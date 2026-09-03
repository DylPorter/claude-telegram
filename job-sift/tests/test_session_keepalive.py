"""The keep-alive, and the two mistakes it exists to make impossible.

MISTAKE 1 — reading a transport failure as a dead session. This suite asserts
that every unhappy path that is not a login bounce comes back UNKNOWN, and that
UNKNOWN neither pulls from a browser nor moves a single session field in the
state file. That is checked as a PROPERTY over every prior state shape, not on
one hand-picked example, because the bug's whole character is that it only
shows up on the run where the network happened to be down.

MISTAKE 2 — clobbering a good cookie with a stale one. The old `sift` wrapper
pulled from firefox/chrome/chromium/brave unconditionally and overwrote the
stored file before anything asked whether it needed replacing. Two assertions
pin the fix: an ALIVE stored cookie causes NO browser pull at all, and a pulled
cookie that fails its own probe is NOT written.

Everything runs against `httpx.MockTransport`, so `conftest.no_network` stays
satisfied and the redirect/status handling under test is the real code.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime

import httpx
import pytest

from job_sift import config, keepalive, session
from job_sift.session import ALIVE, DEAD, UNCONFIGURED, UNKNOWN, UNREACHABLE

PORTAL = "https://web2.cedars.hku.hk/jobs/search.php?sort=postdate&order=desc"

_TABLE_PAGE = """
<html><body><div id="content">
<form action="search.php"></form>
<table class="tablesorter">
  <tr><th>Job ID</th><th>Employer</th><th>Title</th><th>Deadline</th><th>Posted</th></tr>
  <tr><td><a href="job_detail.php?job_id=G0000001">G0000001</a></td>
      <td>Example Co</td><td>Engineer</td><td>2026-10-01</td><td>2026-09-01</td></tr>
</table></div></body></html>
"""

# The portal's own chrome with NO results table. Deliberately kept as its own
# fixture: this is the maintenance-page shape, and the point of the test using
# it is that portal chrome does NOT license a DEAD verdict.
_CHROME_NO_TABLE = """
<html><body><div id="content">
<form action="search.php"></form>
<div id="mega-menu-1"><ul><li>Industries</li></ul></div>
<p>NETjobs is temporarily unavailable for scheduled maintenance.</p>
</div></body></html>
"""

_LOGIN_PAGE = "<html><body><h1>HKU Portal Login</h1></body></html>"

# A placeholder, never a real session value. 26 chars is PHP's default length.
FAKE_SID = "x" * 26


def transport_for(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def serving(status: int, body: str, *, final_path: str | None = None) -> httpx.MockTransport:
    """A transport that answers every request with one canned response.

    `final_path` redirects first, so the bounce guard is exercised through
    httpx's real redirect following rather than by handing the classifier a
    path it never had to derive.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if final_path and request.url.path != final_path:
            return httpx.Response(302, headers={"Location": final_path})
        return httpx.Response(status, text=body)

    return transport_for(handler)


@contextlib.contextmanager
def patched_client(transport: httpx.MockTransport):
    """Force `transport` into every `httpx.Client` the code under test builds.

    For call paths that take no `transport=` argument (`keepalive.run_once`).
    The REAL client is still constructed and still logs, which is the whole
    reason this exists rather than a stub of `ensure_session`.
    """
    real = httpx.Client

    def _make(**kwargs):
        kwargs["transport"] = transport
        return real(**kwargs)

    session.httpx.Client = _make
    try:
        yield
    finally:
        session.httpx.Client = real


def exploding(exc: Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return transport_for(handler)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Point the cookie file and the state dir at tmp_path.

    Both are patched on the config MODULE so every consumer that resolves them
    lazily (`session.write_cookies`, `keepalive._path`) follows, and no test can
    read or clobber a real session.
    """
    monkeypatch.setattr(config, "CEDARS_COOKIES_PATH", tmp_path / "cookies" / "cedars.json")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "CEDARS_PORTAL_URL", PORTAL)
    monkeypatch.setattr(
        session.cedars, "CEDARS_COOKIES_PATH", tmp_path / "cookies" / "cedars.json"
    )
    return tmp_path


@pytest.fixture
def stored_cookie(isolated_paths):
    """A cookie file on disk, and its exact bytes for later comparison."""
    path = config.CEDARS_COOKIES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"PHPSESSID": FAKE_SID}))
    # Already tightened, i.e. the steady state after the first run. Tests about
    # the tightening itself set their own mode.
    path.chmod(0o600)
    return path


@pytest.fixture
def no_browsers(monkeypatch):
    """Record every browser pull and return nothing.

    A recording stub rather than a raising one: several tests need to assert
    that the pull was NEVER attempted, and `pulls == []` says that directly
    while a raise would only say "did not crash".
    """
    pulls: list[str] = []

    def _pull(browser: str) -> dict[str, str]:
        pulls.append(browser)
        return {}

    monkeypatch.setattr(session, "cedars_pull", _pull)
    return pulls


# --------------------------------------------------------------------------
# classify_response — the pure verdict
# --------------------------------------------------------------------------


def test_results_table_is_alive():
    assert session.classify_response(200, "/jobs/search.php", _TABLE_PAGE) == ALIVE


@pytest.mark.parametrize("landing", ["login.php", "main.php"])
def test_bounce_to_login_is_dead(landing):
    assert session.classify_response(200, f"/jobs/{landing}", _LOGIN_PAGE) == DEAD


@pytest.mark.parametrize("status", [301, 403, 429, 500, 502, 503])
def test_non_200_is_unknown_never_dead(status):
    """A 5xx is the server having a bad day, not the cookie being rejected."""
    assert session.classify_response(status, "/jobs/search.php", "") == UNKNOWN


def test_portal_chrome_without_a_table_is_unknown():
    """The maintenance page. It carries CEDARS chrome, so a chrome-based rule
    would call it a portal page — and a table-based rule alone would have to
    call it something. It is UNKNOWN: it says nothing about the cookie."""
    assert session.classify_response(200, "/jobs/search.php", _CHROME_NO_TABLE) == UNKNOWN


def test_unrelated_page_is_unknown():
    """A WAF interstitial or a captive portal is not a verdict on the session."""
    assert session.classify_response(200, "/jobs/search.php", "<html>Access denied</html>") == UNKNOWN


def test_tablesorter_mentioned_only_in_script_is_not_alive():
    """Parsed, not substring-matched — otherwise a stylesheet fakes liveness."""
    html = '<html><head><script>$(".tablesorter").sort()</script></head><body></body></html>'
    assert session.classify_response(200, "/jobs/search.php", html) == UNKNOWN


# --------------------------------------------------------------------------
# check_session — the probe
# --------------------------------------------------------------------------


def test_probe_follows_the_redirect_and_reads_the_terminal_path(stored_cookie):
    """The bounce arrives as a 302; httpx follows it before the guard runs."""
    verdict = session.check_stored_session(
        url=PORTAL, transport=serving(200, _LOGIN_PAGE, final_path="/jobs/login.php")
    )
    assert verdict == DEAD


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("dns"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.RemoteProtocolError("truncated"),
    ],
)
def test_transport_failure_is_unknown_not_dead(stored_cookie, exc):
    """THE headline guarantee. Every one of these used to be indistinguishable
    from an expired cookie, and every one of them would have triggered a
    browser refresh over a healthy stored session."""
    assert session.check_stored_session(url=PORTAL, transport=exploding(exc)) == UNKNOWN


def test_missing_cookie_file_is_dead_not_unknown(isolated_paths):
    """A positive local observation: there is no session to test. Distinct from
    a transport failure, and the correct response (go find one) differs."""
    assert session.check_stored_session(url=PORTAL, transport=serving(200, _TABLE_PAGE)) == DEAD


def test_cookie_file_without_phpsessid_is_dead(isolated_paths):
    path = config.CEDARS_COOKIES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"esd_from_sys": "1"}))
    assert session.check_stored_session(url=PORTAL, transport=serving(200, _TABLE_PAGE)) == DEAD


def test_unset_portal_url_is_unknown(stored_cookie, monkeypatch):
    """Misconfiguration is not a verdict on the cookie either."""
    monkeypatch.setattr(config, "CEDARS_PORTAL_URL", "")
    assert session.check_stored_session(transport=serving(200, _TABLE_PAGE)) == UNKNOWN


# --------------------------------------------------------------------------
# ensure_session — the ordering fix
# --------------------------------------------------------------------------


def test_alive_stored_cookie_never_touches_a_browser(stored_cookie, no_browsers):
    """MISTAKE 2, head on: the old wrapper pulled on every run regardless."""
    before = stored_cookie.read_bytes()
    report = session.ensure_session(url=PORTAL, transport=serving(200, _TABLE_PAGE))
    assert report.state == ALIVE
    assert report.refreshed_from is None
    assert no_browsers == []
    assert stored_cookie.read_bytes() == before


def test_unknown_never_touches_a_browser_or_the_cookie(stored_cookie, no_browsers):
    """MISTAKE 1, head on. A network blip must cost us nothing at all."""
    before = stored_cookie.read_bytes()
    report = session.ensure_session(url=PORTAL, transport=exploding(httpx.ConnectError("dns")))
    assert report.state == UNKNOWN
    assert no_browsers == []
    assert report.wrote_cookie is False
    assert stored_cookie.read_bytes() == before


def test_dead_stored_cookie_walks_the_browsers_and_keeps_a_working_one(
    stored_cookie, monkeypatch
):
    """Recovery: the browser's cookie is different AND it probes ALIVE."""
    fresh = "y" * 26
    monkeypatch.setattr(session, "cedars_pull", lambda b: {"PHPSESSID": fresh} if b == "chrome" else {})

    def handler(request: httpx.Request) -> httpx.Response:
        # The stored cookie bounces through login.php; the chrome one is taken.
        if request.url.path.endswith("login.php"):
            return httpx.Response(200, text=_LOGIN_PAGE)
        if f"PHPSESSID={fresh}" in request.headers.get("cookie", ""):
            return httpx.Response(200, text=_TABLE_PAGE)
        return httpx.Response(302, headers={"Location": "/jobs/login.php"})

    report = session.ensure_session(url=PORTAL, transport=transport_for(handler))
    assert report.state == ALIVE
    assert report.refreshed_from == "chrome"
    assert report.wrote_cookie is True
    assert json.loads(stored_cookie.read_text()) == {"PHPSESSID": fresh}


def test_a_pulled_cookie_that_fails_its_probe_is_not_written(stored_cookie, monkeypatch):
    """The 2026-09-02 failure, inverted. Firefox HAD a cookie; CEDARS did not
    accept it; the old code wrote it anyway and reported success."""
    before = stored_cookie.read_bytes()
    stale = "z" * 26
    monkeypatch.setattr(session, "cedars_pull", lambda b: {"PHPSESSID": stale})
    report = session.ensure_session(
        url=PORTAL, transport=serving(200, _LOGIN_PAGE, final_path="/jobs/login.php")
    )
    assert report.state == DEAD
    assert report.refreshed_from is None
    assert report.wrote_cookie is False
    assert set(report.rejected) == set(session.DEFAULT_BROWSER_ORDER)
    assert stored_cookie.read_bytes() == before


def test_dry_run_recovery_writes_nothing(stored_cookie, monkeypatch):
    """The RECOVERY branch under --dry-run, which is the only place the guard
    exists.

    The earlier version of this test served `_TABLE_PAGE` to every request, so
    the STORED cookie probed alive and `ensure_session` returned at step 1 — the
    recovery branch was never entered, and deleting `if not dry_run:` left the
    suite green. It was a duplicate of `test_alive_stored_cookie_never_touches_a_
    browser` wearing a different name.

    So the transport has to distinguish the two cookies: the stored one bounces,
    the browser's one is accepted. Only then does the walk run, find a working
    cookie, and reach the write it must not perform.
    """
    before = stored_cookie.read_bytes()
    fresh = "y" * 26
    monkeypatch.setattr(session, "cedars_pull", lambda b: {"PHPSESSID": fresh})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("login.php"):
            return httpx.Response(200, text=_LOGIN_PAGE)
        if f"PHPSESSID={fresh}" in request.headers.get("cookie", ""):
            return httpx.Response(200, text=_TABLE_PAGE)
        return httpx.Response(302, headers={"Location": "/jobs/login.php"})

    report = session.ensure_session(
        url=PORTAL, dry_run=True, transport=transport_for(handler)
    )
    # Proof the branch was actually reached — without these, the assertions
    # below are satisfied by the step-1 early return all over again.
    assert report.refreshed_from == "firefox"
    assert report.tried == ["firefox"]
    assert report.state == ALIVE
    assert report.wrote_cookie is False
    assert stored_cookie.read_bytes() == before


def test_browser_read_failure_is_skipped_not_fatal(stored_cookie, monkeypatch):
    """A locked chromium keyring on a machine where the login lives in Firefox
    is noise, not a failure — the walk must continue past it."""
    fresh = "y" * 26

    def _pull(browser: str) -> dict[str, str]:
        if browser == "firefox":
            raise RuntimeError("locked keyring")
        if browser == "chrome":
            return {"PHPSESSID": fresh}
        return {}

    monkeypatch.setattr(session, "cedars_pull", _pull)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("main.php"):
            return httpx.Response(200, text=_LOGIN_PAGE)
        if f"PHPSESSID={fresh}" in request.headers.get("cookie", ""):
            return httpx.Response(200, text=_TABLE_PAGE)
        return httpx.Response(302, headers={"Location": "/jobs/main.php"})

    report = session.ensure_session(url=PORTAL, transport=transport_for(handler))
    assert report.state == ALIVE
    assert report.refreshed_from == "chrome"


def test_browser_order_puts_the_requested_one_first_without_duplicating():
    assert session.browser_order("brave") == ["brave", "firefox", "chrome", "chromium"]
    assert session.browser_order(None) == list(session.DEFAULT_BROWSER_ORDER)
    assert session.browser_order("firefox") == list(session.DEFAULT_BROWSER_ORDER)


# --------------------------------------------------------------------------
# no secrets anywhere
# --------------------------------------------------------------------------


def test_reports_carry_lengths_never_values():
    described = session.describe_cookies({"PHPSESSID": FAKE_SID, "esd_from_sys": "1"})
    assert FAKE_SID not in described
    assert "PHPSESSID (26 chars)" in described


def test_no_cookie_value_reaches_the_log_on_a_rejected_pull(stored_cookie, monkeypatch, caplog):
    """The rejection path is the one that formats a pulled cookie into a
    message, so it is the one worth pinning."""
    stale = "q" * 26
    monkeypatch.setattr(session, "cedars_pull", lambda b: {"PHPSESSID": stale})
    with caplog.at_level("DEBUG"):
        session.ensure_session(
            url=PORTAL, transport=serving(200, _LOGIN_PAGE, final_path="/jobs/login.php")
        )
    assert stale not in caplog.text
    assert "PHPSESSID (26 chars)" in caplog.text


def test_summary_never_carries_a_cookie_value(stored_cookie, monkeypatch):
    fresh = "w" * 26
    monkeypatch.setattr(session, "cedars_pull", lambda b: {"PHPSESSID": fresh})
    report = session.ensure_session(url=PORTAL, transport=serving(200, _TABLE_PAGE))
    assert fresh not in report.summary()


# --------------------------------------------------------------------------
# the cookie file has two writers now
# --------------------------------------------------------------------------


def test_write_cookies_is_atomic_and_leaves_no_tmp_file(isolated_paths):
    session.write_cookies({"PHPSESSID": FAKE_SID})
    path = config.CEDARS_COOKIES_PATH
    assert json.loads(path.read_text()) == {"PHPSESSID": FAKE_SID}
    assert list(path.parent.iterdir()) == [path]


def test_write_cookies_is_owner_only(isolated_paths):
    session.write_cookies({"PHPSESSID": FAKE_SID})
    assert config.CEDARS_COOKIES_PATH.stat().st_mode & 0o777 == 0o600


def test_a_failed_write_leaves_the_old_file_intact(stored_cookie, monkeypatch):
    before = stored_cookie.read_bytes()

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(session.json, "dump", _boom)
    with pytest.raises(OSError):
        session.write_cookies({"PHPSESSID": "new"})
    assert stored_cookie.read_bytes() == before
    assert list(stored_cookie.parent.iterdir()) == [stored_cookie]


# --------------------------------------------------------------------------
# keepalive state — UNKNOWN records no death
# --------------------------------------------------------------------------

NOW = datetime(2026, 9, 3, 15, 40, 0)

_PRIORS = [
    keepalive._blank(),
    {**keepalive._blank(), "state": ALIVE, "last_alive": "2026-09-03T09:00:00"},
    {
        **keepalive._blank(),
        "state": DEAD,
        "last_alive": "2026-09-01T09:00:00",
        "last_dead": "2026-09-03T14:00:00",
        "consecutive_dead": 7,
    },
]


@pytest.mark.parametrize("prior", _PRIORS)
def test_unknown_moves_no_session_field_whatever_the_prior(prior):
    """A PROPERTY, not an example. Whatever the session was, an unreachable
    portal leaves it exactly that — no death recorded, no streak advanced, no
    last_alive rewritten."""
    after = keepalive.next_state(prior, UNKNOWN, now=NOW)
    for field in ("state", "last_alive", "last_dead", "consecutive_dead"):
        assert after[field] == prior[field], field
    assert after["consecutive_unknown"] == prior["consecutive_unknown"] + 1
    assert after["last_unknown"] == NOW.isoformat(timespec="seconds")


def test_dead_advances_the_streak_and_keeps_last_alive():
    prior = {**keepalive._blank(), "state": ALIVE, "last_alive": "2026-09-03T09:00:00"}
    after = keepalive.next_state(prior, DEAD, now=NOW)
    assert after["state"] == DEAD
    assert after["consecutive_dead"] == 1
    assert after["last_alive"] == "2026-09-03T09:00:00"
    assert after["last_dead"] == NOW.isoformat(timespec="seconds")


def test_alive_clears_both_streaks():
    prior = {
        **keepalive._blank(),
        "state": DEAD,
        "consecutive_dead": 9,
        "consecutive_unknown": 4,
    }
    after = keepalive.next_state(prior, ALIVE, now=NOW)
    assert after["state"] == ALIVE
    assert after["consecutive_dead"] == 0
    assert after["consecutive_unknown"] == 0
    assert after["last_alive"] == NOW.isoformat(timespec="seconds")


def test_corrupt_state_file_starts_fresh_rather_than_crashing(isolated_paths):
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (config.STATE_DIR / keepalive.STATE_FILENAME).write_text("{ not json")
    assert keepalive.load_state() == keepalive._blank()


def test_state_round_trips(isolated_paths):
    state = keepalive.next_state(keepalive._blank(), ALIVE, now=NOW)
    keepalive.save_state(state)
    assert keepalive.load_state() == state


def test_state_save_leaves_no_tmp_file(isolated_paths):
    keepalive.save_state(keepalive._blank())
    assert [p.name for p in config.STATE_DIR.iterdir()] == [keepalive.STATE_FILENAME]


# --------------------------------------------------------------------------
# keepalive end to end
# --------------------------------------------------------------------------


@pytest.fixture
def production_logging():
    """Configure logging exactly as a real `keepalive` run does, and restore it.

    THE POINT. `caplog.at_level(...)` FORCES a threshold, so a test using it
    reports what the test asked for rather than what production emits — and the
    previous version of these two tests also stubbed `ensure_session` out, so
    neither `session.py`'s logging nor httpx's ever ran. `caplog.records == []`
    was then a fact about a stub.

    Here the real thresholds are installed instead, on the LOGGERS. A record
    below its logger's level is never CONSTRUCTED, so `caplog.records` ends up
    holding precisely the lines that would reach the journal — including httpx's
    one-per-request line if the silencing regresses.
    """
    root = logging.getLogger()
    saved = {name: logging.getLogger(name).level for name in keepalive._NOISY_LIBRARIES}
    saved_root = root.level
    keepalive.configure_logging(verbose=False)
    yield
    root.setLevel(saved_root)
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_a_steady_live_session_emits_nothing(
    stored_cookie, no_browsers, production_logging, caplog
):
    """THE HAPPY PATH, 144 TIMES A DAY. Not one line.

    Drives the REAL `ensure_session` and `check_session` through a real
    `httpx.Client`; only the socket layer is a mock. That is what makes httpx's
    own INFO-per-request line reachable, which is the regression this catches
    and which the stubbed version could not have seen.
    """
    keepalive.save_state({**keepalive._blank(), "state": ALIVE})
    with patched_client(serving(200, _TABLE_PAGE)):
        verdict, _report, state = keepalive.run_once(now=NOW)
    assert verdict == ALIVE
    assert state["state"] == ALIVE
    assert no_browsers == []
    assert [f"{r.name}: {r.getMessage()}" for r in caplog.records] == []


def test_a_steady_dead_session_with_stale_browser_cookies_emits_nothing(
    stored_cookie, monkeypatch, production_logging, caplog
):
    """THE WORST CASE, and the one that measured 15 lines a run / ~2160 a day.

    A dead stored cookie, four browsers that each hold a CEDARS cookie the
    portal refuses, and a prior state that already says dead. Five probes, five
    httpx request lines, four rejection messages and a summary — all of which
    must stay below the threshold, because NOTHING CHANGED since the last run.
    """
    monkeypatch.setattr(session, "cedars_pull", lambda b: {"PHPSESSID": "z" * 26})
    keepalive.save_state({**keepalive._blank(), "state": DEAD, "consecutive_dead": 5})
    with patched_client(serving(200, _LOGIN_PAGE, final_path="/jobs/login.php")):
        verdict, report, state = keepalive.run_once(now=NOW)
    assert verdict == DEAD
    # The expensive path really was walked — otherwise this asserts nothing.
    assert report.tried == list(session.DEFAULT_BROWSER_ORDER)
    assert report.rejected == list(session.DEFAULT_BROWSER_ORDER)
    assert state["consecutive_dead"] == 6
    assert [f"{r.name}: {r.getMessage()}" for r in caplog.records] == []


def test_a_sustained_outage_emits_nothing_after_the_first_run(
    stored_cookie, no_browsers, production_logging, caplog
):
    """THE THIRD STEADY STATE, and the one the first two miss.

    A portal that stays unreachable is not a state change after the first run,
    so it must go quiet — otherwise a weekend of downtime writes 432 identical
    lines into the journal the daily sift shares. `consecutive_unknown` is what
    distinguishes "just went unreachable" (worth one INFO) from "still
    unreachable" (worth nothing), and this pins the second half.
    """
    keepalive.save_state(
        {
            **keepalive._blank(),
            "state": ALIVE,
            "consecutive_unknown": 3,
            "last_unknown_reason": session.UNREACHABLE,
        }
    )
    with patched_client(exploding(httpx.ConnectError("dns"))):
        verdict, _report, state = keepalive.run_once(now=NOW)
    assert verdict == UNKNOWN
    assert state["consecutive_unknown"] == 4
    assert [f"{r.name}: {r.getMessage()}" for r in caplog.records] == []


def test_the_first_run_of_an_outage_says_so_exactly_once(
    stored_cookie, no_browsers, production_logging, caplog
):
    """The other half: a portal that has JUST become unreachable is a state
    change, and gets one line."""
    keepalive.save_state({**keepalive._blank(), "state": ALIVE})
    with patched_client(exploding(httpx.ConnectError("dns"))):
        keepalive.run_once(now=NOW)
    records = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert len(records) == 1
    assert records[0].name == keepalive.__name__


def test_an_unset_portal_url_says_so_instead_of_blaming_the_network(
    stored_cookie, no_browsers, production_logging, caplog, monkeypatch
):
    """NOTHING WAS REACHED, because nothing was requested.

    Demoting the unset-URL warning to DEBUG (done to keep the journal quiet)
    left the keep-alive's UNKNOWN line as the only surfacing, and it said "could
    not reach the portal" — one message meaning both "the network is down" and
    "you never configured this". Those want opposite things from the reader: one
    is transient and self-healing, the other will never fix itself.
    """
    monkeypatch.setattr(config, "CEDARS_PORTAL_URL", "")
    keepalive.save_state({**keepalive._blank(), "state": ALIVE})
    with patched_client(serving(200, _TABLE_PAGE)):
        verdict, report, state = keepalive.run_once(now=NOW)

    assert verdict == UNKNOWN
    assert report.reason == UNCONFIGURED
    assert state["last_unknown_reason"] == UNCONFIGURED
    # Nothing recorded against the session, exactly as for any other UNKNOWN.
    assert state["state"] == ALIVE
    assert state["consecutive_dead"] == 0

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert len(messages) == 1
    assert "CEDARS_PORTAL_URL" in messages[0]
    # The claim that would be false: nothing was reached, so do not say we tried.
    assert "could not reach" not in messages[0]


def test_the_unconfigured_and_unreachable_messages_differ(
    stored_cookie, no_browsers, production_logging, caplog, monkeypatch
):
    """The point of the split, asserted directly rather than inferred from two
    separate tests that could drift into saying the same thing."""
    def _message_for(portal_url, transport):
        caplog.clear()
        monkeypatch.setattr(config, "CEDARS_PORTAL_URL", portal_url)
        keepalive.save_state({**keepalive._blank(), "state": ALIVE})
        with patched_client(transport):
            keepalive.run_once(now=NOW)
        return [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO][0]

    unconfigured = _message_for("", serving(200, _TABLE_PAGE))
    unreachable = _message_for(PORTAL, exploding(httpx.ConnectError("dns")))
    assert unconfigured != unreachable
    assert "could not reach" in unreachable
    assert "could not reach" not in unconfigured


def test_a_change_of_unknown_reason_is_itself_a_state_change(
    stored_cookie, no_browsers, production_logging, caplog, monkeypatch
):
    """A portal that was unreachable and is now unconfigured (someone cleared
    .env) would otherwise just keep climbing `consecutive_unknown` in silence,
    never saying the one thing that explains it."""
    monkeypatch.setattr(config, "CEDARS_PORTAL_URL", "")
    keepalive.save_state(
        {
            **keepalive._blank(),
            "state": ALIVE,
            "consecutive_unknown": 9,
            "last_unknown_reason": UNREACHABLE,
        }
    )
    with patched_client(serving(200, _TABLE_PAGE)):
        keepalive.run_once(now=NOW)
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert len(messages) == 1
    assert "CEDARS_PORTAL_URL" in messages[0]


def test_a_steady_unconfigured_state_still_goes_quiet(
    stored_cookie, no_browsers, production_logging, caplog, monkeypatch
):
    """Said once, clearly — then silence, like every other steady state."""
    monkeypatch.setattr(config, "CEDARS_PORTAL_URL", "")
    keepalive.save_state(
        {
            **keepalive._blank(),
            "state": ALIVE,
            "consecutive_unknown": 4,
            "last_unknown_reason": UNCONFIGURED,
        }
    )
    with patched_client(serving(200, _TABLE_PAGE)):
        keepalive.run_once(now=NOW)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO] == []


def test_httpx_is_silenced_at_production_level_but_not_under_verbose():
    """Pinned directly as well as through the two tests above, because this is a
    one-line setting that a future edit to `configure_logging` could drop while
    leaving both of those passing for some other reason."""
    root = logging.getLogger()
    saved_root, saved_httpx = root.level, logging.getLogger("httpx").level
    try:
        keepalive.configure_logging(verbose=False)
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        assert logging.getLogger("job_sift.session").isEnabledFor(logging.INFO)
        keepalive.configure_logging(verbose=True)
        assert logging.getLogger("httpx").isEnabledFor(logging.INFO)
    finally:
        root.setLevel(saved_root)
        logging.getLogger("httpx").setLevel(saved_httpx)


def test_configure_logging_wins_over_an_existing_handler():
    """`basicConfig` is a no-op once the root logger has a handler — which is
    true under pytest, and under anything that configured logging first. That
    made `-v` silently do nothing exactly where you would reach for it."""
    root = logging.getLogger()
    saved = root.level
    try:
        root.setLevel(logging.CRITICAL)
        keepalive.configure_logging(verbose=True)
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(saved)


def test_run_once_logs_info_when_the_session_dies(stored_cookie, monkeypatch, caplog):
    keepalive.save_state({**keepalive._blank(), "state": ALIVE})
    monkeypatch.setattr(
        keepalive,
        "ensure_session",
        lambda **kw: session.SessionReport(state=DEAD, tried=["firefox"]),
    )
    with caplog.at_level("INFO"):
        keepalive.run_once(now=NOW)
    # Asserted structurally: exactly one INFO line, from THIS module, naming the
    # browsers it walked. A substring match on the wording would break on a
    # rephrase while proving nothing about the state-change rule.
    infos = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert len(infos) == 1
    assert infos[0].name == keepalive.__name__
    assert "firefox" in infos[0].getMessage()


def test_run_once_logs_info_on_recovery(stored_cookie, monkeypatch, caplog):
    keepalive.save_state({**keepalive._blank(), "state": DEAD, "consecutive_dead": 3})
    monkeypatch.setattr(
        keepalive,
        "ensure_session",
        lambda **kw: session.SessionReport(state=ALIVE, refreshed_from="firefox", wrote_cookie=True),
    )
    with caplog.at_level("INFO"):
        _verdict, _report, state = keepalive.run_once(now=NOW)
    infos = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert len(infos) == 1
    assert infos[0].name == keepalive.__name__
    # The recovery line must name the browser AND the streak it ended — those
    # are the two facts a reader needs, and they are structural, not wording.
    assert "firefox" in infos[0].getMessage() and "3" in infos[0].getMessage()
    assert state["consecutive_dead"] == 0


def test_run_once_unknown_records_no_death_end_to_end(
    stored_cookie, no_browsers, isolated_paths, monkeypatch
):
    """The full path, through the real `ensure_session` and a real (mock)
    transport: an unreachable portal leaves both files untouched in every way
    that matters."""
    keepalive.save_state(
        {**keepalive._blank(), "state": ALIVE, "last_alive": "2026-09-03T09:00:00"}
    )
    # The real `ensure_session` and the real `check_session` run; only the
    # socket layer is replaced, and it is replaced with a DNS failure — the
    # exact event that used to be read as an expired cookie.
    cookie_before = stored_cookie.read_bytes()
    with patched_client(exploding(httpx.ConnectError("dns"))):
        verdict, _report, state = keepalive.run_once(now=NOW)
    assert verdict == UNKNOWN
    assert state["state"] == ALIVE
    assert state["last_alive"] == "2026-09-03T09:00:00"
    assert state["consecutive_dead"] == 0
    assert state["consecutive_unknown"] == 1
    assert no_browsers == []
    assert stored_cookie.read_bytes() == cookie_before


def test_keepalive_dry_run_writes_no_state(stored_cookie, monkeypatch, isolated_paths):
    monkeypatch.setattr(
        keepalive, "ensure_session", lambda **kw: session.SessionReport(state=ALIVE)
    )
    keepalive.run_once(dry_run=True, now=NOW)
    assert not (config.STATE_DIR / keepalive.STATE_FILENAME).exists()


def test_keepalive_dry_run_is_passed_through_to_ensure_session(stored_cookie, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        keepalive,
        "ensure_session",
        lambda **kw: seen.update(kw) or session.SessionReport(state=ALIVE),
    )
    keepalive.run_once(dry_run=True, now=NOW)
    assert seen["dry_run"] is True


@pytest.mark.parametrize(
    "state,code",
    [(ALIVE, 0), (DEAD, 1), (UNKNOWN, 2)],
)
def test_exit_codes_distinguish_dead_from_unreachable(stored_cookie, monkeypatch, state, code):
    """The `sift` wrapper branches on this: a DEAD session earns the loud
    log-back-in banner, an UNKNOWN one earns a quiet note."""
    monkeypatch.setattr(session, "ensure_session", lambda **kw: session.SessionReport(state=state))
    assert session.main(["--dry-run"]) == code


@pytest.mark.parametrize("state,code", [(ALIVE, 0), (DEAD, 1), (UNKNOWN, 2)])
def test_keepalive_exit_codes(stored_cookie, monkeypatch, state, code):
    monkeypatch.setattr(
        keepalive, "ensure_session", lambda **kw: session.SessionReport(state=state)
    )
    assert keepalive.main(["--dry-run"]) == code


# --------------------------------------------------------------------------
# an internal failure is not "session dead"
# --------------------------------------------------------------------------
#
# `sift` answers exit 1 with a banner telling the operator to go log into CEDARS
# in Firefox. So exit 1 must mean ONE thing. `write_cookies` raising after a
# session was verified alive and recovered is not that thing — the session was
# fine, we merely failed to persist it — and reporting it as a dead session
# would be one exit code carrying two incompatible meanings, which is the
# overloading this whole codebase keeps having to remove.


def _recovery_transport(fresh: str) -> httpx.MockTransport:
    """Stored cookie bounces, browser cookie is accepted — i.e. the write path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("login.php"):
            return httpx.Response(200, text=_LOGIN_PAGE)
        if f"PHPSESSID={fresh}" in request.headers.get("cookie", ""):
            return httpx.Response(200, text=_TABLE_PAGE)
        return httpx.Response(302, headers={"Location": "/jobs/login.php"})

    return transport_for(handler)


def test_a_failed_cookie_write_does_not_masquerade_as_a_dead_session(
    stored_cookie, monkeypatch
):
    """Exit 2, not exit 1 — `sift` answers exit 1 with the log-back-in banner."""
    monkeypatch.setattr(
        session, "ensure_session", lambda **kw: (_ for _ in ()).throw(OSError("disk full"))
    )
    assert session.main([]) == session.EXIT_UNKNOWN


def test_the_write_failure_really_does_escape_ensure_session(stored_cookie, monkeypatch):
    """The precondition for the test above: this is a REAL path, not a
    hypothetical. If `ensure_session` ever swallows the write error, the mapping
    in `main` becomes dead code and this test says so."""
    fresh = "y" * 26
    monkeypatch.setattr(session, "cedars_pull", lambda b: {"PHPSESSID": fresh})
    monkeypatch.setattr(
        session, "write_cookies", lambda c: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        session.ensure_session(url=PORTAL, transport=_recovery_transport(fresh))


def test_keepalive_maps_an_internal_failure_to_unknown_too(stored_cookie, monkeypatch):
    monkeypatch.setattr(
        keepalive, "run_once", lambda **kw: (_ for _ in ()).throw(OSError("disk full"))
    )
    assert keepalive.main([]) == keepalive.EXIT_UNKNOWN


def test_an_unparseable_body_is_unknown_not_a_crash(stored_cookie, monkeypatch):
    """`classify_response` parses with BeautifulSoup, which sits outside the
    transport try-block. A parser that chokes has said exactly one thing: it
    could not read the page."""
    def _boom(*a, **k):
        raise ValueError("parser exploded")

    monkeypatch.setattr(session, "classify_response", _boom)
    assert session.check_stored_session(url=PORTAL, transport=serving(200, _TABLE_PAGE)) == UNKNOWN


# --------------------------------------------------------------------------
# the credential's file mode
# --------------------------------------------------------------------------


def test_a_world_readable_cookie_is_tightened_on_read(stored_cookie, no_browsers):
    """write_cookies sets 0600, but it only runs when a cookie is REWRITTEN —
    and the alive path rewrites nothing, so a file that arrived at 0644 (as the
    live one had) would stay world-readable indefinitely. The mode is therefore
    asserted on every read, which happens every run."""
    stored_cookie.chmod(0o644)
    session.check_stored_session(url=PORTAL, transport=serving(200, _TABLE_PAGE))
    assert stored_cookie.stat().st_mode & 0o777 == 0o600


def test_tightening_the_mode_is_announced_once_then_never_again(
    stored_cookie, no_browsers, caplog
):
    """It is a state change, so it earns an INFO line — but only the first time,
    because the second run finds it already correct."""
    stored_cookie.chmod(0o644)
    with caplog.at_level("INFO"):
        session.check_stored_session(url=PORTAL, transport=serving(200, _TABLE_PAGE))
    assert len([r for r in caplog.records if r.levelno >= logging.INFO]) == 1
    caplog.clear()
    with caplog.at_level("INFO"):
        session.check_stored_session(url=PORTAL, transport=serving(200, _TABLE_PAGE))
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


def test_a_missing_cookie_file_does_not_make_hardening_raise(isolated_paths):
    session.harden_cookie_file()  # must not raise
    assert session.check_stored_session(url=PORTAL, transport=serving(200, _TABLE_PAGE)) == DEAD
