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

import json
from datetime import datetime

import httpx
import pytest

from job_sift import config, keepalive, session
from job_sift.session import ALIVE, DEAD, UNKNOWN

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


def _client_factory(transport: httpx.MockTransport):
    """An `httpx.Client` replacement that forces `transport` in.

    Used only where the code path under test is reached through a caller that
    takes no `transport=` argument (`keepalive.run_once`). Everywhere else the
    transport is injected properly, because the point of injecting it is that
    the real client code still runs.
    """
    real = httpx.Client

    def _make(**kwargs):
        kwargs["transport"] = transport
        return real(**kwargs)

    return _make


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
    before = stored_cookie.read_bytes()
    fresh = "y" * 26
    monkeypatch.setattr(session, "cedars_pull", lambda b: {"PHPSESSID": fresh})
    report = session.ensure_session(
        url=PORTAL, dry_run=True, transport=serving(200, _TABLE_PAGE)
    )
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


def test_run_once_on_a_live_session_is_silent(stored_cookie, no_browsers, monkeypatch, caplog):
    """The happy path, 144 times a day. It must not print an INFO line."""
    keepalive.save_state({**keepalive._blank(), "state": ALIVE})
    monkeypatch.setattr(
        keepalive, "ensure_session", lambda **kw: session.SessionReport(state=ALIVE)
    )
    with caplog.at_level("INFO"):
        verdict, _report, state = keepalive.run_once(now=NOW)
    assert verdict == ALIVE
    assert caplog.records == []
    assert state["state"] == ALIVE


def test_run_once_logs_info_when_the_session_dies(stored_cookie, monkeypatch, caplog):
    keepalive.save_state({**keepalive._blank(), "state": ALIVE})
    monkeypatch.setattr(
        keepalive,
        "ensure_session",
        lambda **kw: session.SessionReport(state=DEAD, tried=["firefox"]),
    )
    with caplog.at_level("INFO"):
        keepalive.run_once(now=NOW)
    assert any(r.levelname == "INFO" and "DEAD" in r.message for r in caplog.records)


def test_run_once_logs_info_on_recovery(stored_cookie, monkeypatch, caplog):
    keepalive.save_state({**keepalive._blank(), "state": DEAD, "consecutive_dead": 3})
    monkeypatch.setattr(
        keepalive,
        "ensure_session",
        lambda **kw: session.SessionReport(state=ALIVE, refreshed_from="firefox", wrote_cookie=True),
    )
    with caplog.at_level("INFO"):
        _verdict, _report, state = keepalive.run_once(now=NOW)
    assert any("recovered" in r.message for r in caplog.records)
    assert state["consecutive_dead"] == 0


def test_run_once_is_quiet_while_the_session_stays_dead(stored_cookie, monkeypatch, caplog):
    """A dead session must not shout 144 times a day either — the daily run's
    banner is the loud surface."""
    keepalive.save_state({**keepalive._blank(), "state": DEAD, "consecutive_dead": 5})
    monkeypatch.setattr(
        keepalive, "ensure_session", lambda **kw: session.SessionReport(state=DEAD, tried=["firefox"])
    )
    with caplog.at_level("INFO"):
        _verdict, _report, state = keepalive.run_once(now=NOW)
    assert caplog.records == []
    assert state["consecutive_dead"] == 6


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
    monkeypatch.setattr(
        session.httpx, "Client", _client_factory(exploding(httpx.ConnectError("dns")))
    )
    cookie_before = stored_cookie.read_bytes()
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
