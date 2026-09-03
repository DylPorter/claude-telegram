"""Is the stored CEDARS session still accepted? And if not, can we replace it?

Two things live here, and the split between them is the whole point.

`check_session` answers ONE question — does CEDARS still accept this cookie? —
with THREE answers, because two is what broke this codebase before:

    ALIVE    200, we were not bounced, and the results table is on the page
    DEAD     the request was redirected to login.php / main.php
    UNKNOWN  anything else at all

UNKNOWN IS THE DEFAULT FOR EVERY UNHAPPY PATH: a transport error, a DNS
failure, a timeout, a 5xx, a 403, a WAF interstitial, a maintenance notice, a
200 whose body carries no results table. None of those is evidence about the
COOKIE. They are evidence about the NETWORK, and the two get confused in
exactly one direction: "I could not reach the server" read as "the session is
dead", which then triggers a browser refresh that overwrites a perfectly good
stored cookie with whatever Firefox happens to be holding. The sibling module
`liveness.py` fails safe the same way and for the same reason; so does
`source_health`'s insistence that success be positive rather than inferred.

A NOTE ON WHAT IS *NOT* UNKNOWN. A missing cookie file, or a cookie file with
no PHPSESSID, is DEAD. That is not a transport failure being reinterpreted —
it is a positive local observation that there is no session to test, and the
right response (go find one in a browser) is the same as for an expired one.

`ensure_session` is the ordering fix. The old `sift` wrapper pulled a cookie
from firefox -> chrome -> chromium -> brave on EVERY run and overwrote the
stored file unconditionally, before anything had asked whether the stored file
was fine. On 2026-09-02 that printed "cookie refreshed from firefox" and handed
the scraper an expired session, because Firefox's copy was older than the one
already on disk. So the order inverts:

    1. stored cookie ALIVE   -> touch nothing
    2. stored cookie UNKNOWN -> touch nothing, and say so. A network blip must
                                never cost us the stored session.
    3. stored cookie DEAD    -> walk the browsers; keep a pulled cookie ONLY if
                                it then tests ALIVE

Point 3's second clause matters as much as the ordering: a browser pull that
succeeds proves a cookie EXISTS, not that CEDARS still honours it. Writing an
untested pull to disk is how a dead session gets laundered into a fresh
timestamp.

NO COOKIE VALUE IS EVER LOGGED, PRINTED, OR RETURNED from this module. Reports
carry cookie names and character counts only.

EVERYTHING HERE LOGS AT DEBUG, deliberately. This module has two callers with
opposite noise budgets: `sift` runs it once a day and wants to be told things,
while `keepalive` runs it 144 times a day and must be silent unless something
CHANGED. A library that decides its own log levels serves the first caller and
drowns the second — and the drowning is not hypothetical: at INFO, a dead
session whose browsers hold a stale cookie emitted fifteen lines a run, about
2160 a day, on top of httpx's own request line per probe.

So the verdict is DATA (`SessionReport`), and announcing it is the CALLER's job:
`keepalive._log_verdict` owns the INFO-on-state-change rule, and `main` prints
one summary line to stdout for the interactive path. The single exception is
`harden_cookie_file`, which logs INFO when it actually changes a file mode —
that is a state change, and it can only fire once.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from job_sift import config
from job_sift.sources import cedars

log = logging.getLogger(__name__)

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"

# WHY a verdict came back UNKNOWN. The state is what decides the ACTION (and for
# UNKNOWN the action is always "do nothing"), but it is not enough to decide the
# DIAGNOSIS, and the diagnoses want opposite things from the reader:
#
#   UNREACHABLE  transient. The network, the VPN, or the portal. Wait.
#   UNCONFIGURED permanent. CEDARS_PORTAL_URL is empty. Nothing will ever fix
#                itself, and "could not reach the portal" is actively wrong —
#                nothing was reached because nothing was attempted.
#
# Collapsing these into one sentence is the same overloading this module spends
# its whole length avoiding, moved from the action into the diagnosis.
UNREACHABLE = "unreachable"
UNCONFIGURED = "unconfigured"

#: An expired PHPSESSID makes the listings request redirect through login.php
#: and land on one of these with a 200. This is the ONLY positive evidence of
#: death we accept from the network; everything else routes to UNKNOWN.
BOUNCE_TARGETS = frozenset({"login.php", "main.php"})

#: The results table. Its presence is what says "CEDARS served us the logged-in
#: page", and it is checked by parsing rather than by substring so a stray
#: mention of the class name in a script or stylesheet cannot fake it.
_RESULTS_TABLE_SELECTOR = "table.tablesorter"

#: Deliberately shorter than the scraper's 30s. This runs 144 times a day and
#: has nothing to salvage from a slow response — a probe that gives up is
#: UNKNOWN, which costs nothing. Kept well under the unit's TimeoutStartSec.
PROBE_TIMEOUT_S = 20.0

#: httpx's default ceiling is 20 hops. The portal needs at most a couple, and a
#: long chain is a bounce we should not be following anyway.
_MAX_REDIRECTS = 5

#: The browsers a dead session is hunted for in, in order. Firefox first is a
#: technical requirement, not a preference: chromium-family cookie stores are
#: encrypted against the OS keyring and cannot be read from a headless unit.
DEFAULT_BROWSER_ORDER = ("firefox", "chrome", "chromium", "brave")


def browser_order(first: str | None = None) -> list[str]:
    """`first` (if given) followed by the rest, no duplicates."""
    order = [first] if first else []
    order.extend(b for b in DEFAULT_BROWSER_ORDER if b != first)
    return order


def classify_response(status: int, final_path: str, html: str) -> str:
    """Read one probe response. PURE — no I/O, so it is directly testable.

    `final_path` is the path AFTER redirects; httpx has already followed the
    302 by the time this runs, so the requested path tells us nothing.

    ORDER MATTERS. The bounce check runs before the table check because a login
    page has no results table and would otherwise fall through to UNKNOWN,
    losing the one signal that genuinely identifies an expired session.
    """
    if status != 200:
        return UNKNOWN
    if final_path.rsplit("/", 1)[-1] in BOUNCE_TARGETS:
        return DEAD
    if BeautifulSoup(html, "lxml").select_one(_RESULTS_TABLE_SELECTOR) is not None:
        return ALIVE
    # Portal chrome is deliberately NOT consulted here. A CEDARS-served
    # maintenance page carries CEDARS chrome, and treating that as a verdict on
    # the cookie is the same mistake in a nicer outfit. `sources/cedars.py` uses
    # the chrome check to WORD an error; nothing uses it to decide one.
    return UNKNOWN


def check_session(
    cookies: Mapping[str, str] | None,
    *,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Probe CEDARS with `cookies`. Returns ALIVE / DEAD / UNKNOWN. Never raises.

    `transport` is injected so tests drive the real function against
    `httpx.MockTransport` rather than monkeypatching it away — the redirect and
    status handling is precisely the part worth exercising.
    """
    if not cookies or "PHPSESSID" not in cookies:
        # A positive local observation, not a transport failure. See the module
        # docstring: there is no session to test, so there is nothing to lose by
        # going to look for one.
        log.debug("cedars session: no stored PHPSESSID")
        return DEAD

    target = url or config.CEDARS_PORTAL_URL
    if not target:
        # Unconfigured is not a verdict on the cookie. DEBUG rather than
        # WARNING for the same reason as everything else in this module: a
        # static misconfiguration would otherwise emit 144 identical warnings a
        # day, which is precisely the drowning this design is trying to avoid.
        # `SessionReport.summary()` names this cause on the interactive path.
        log.debug("cedars session: CEDARS_PORTAL_URL is not set — cannot probe")
        return UNKNOWN

    try:
        with httpx.Client(
            cookies=dict(cookies),
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            timeout=PROBE_TIMEOUT_S,
            transport=transport,
        ) as client:
            resp = client.get(target)
    except Exception as exc:  # noqa: BLE001 — every transport failure is UNKNOWN
        log.debug("cedars session: probe could not reach the portal (%s)", type(exc).__name__)
        return UNKNOWN

    try:
        verdict = classify_response(resp.status_code, resp.url.path, resp.text)
    except Exception as exc:  # noqa: BLE001 — an unparseable body is UNKNOWN
        # `classify_response` parses with BeautifulSoup, and a parser that
        # chokes on a malformed body has told us exactly one thing: it could not
        # read the page. That is the definition of UNKNOWN. Letting it escape
        # would instead crash the process, and a crashed process is an exit code
        # the caller has to interpret — which is how "could not look" got
        # confused with "session dead" in the first place.
        log.debug("cedars session: could not parse the probe response (%s)", type(exc).__name__)
        return UNKNOWN
    log.debug("cedars session: probe -> %s (HTTP %s)", verdict, resp.status_code)
    return verdict


def harden_cookie_file() -> None:
    """Force the cookie file to 0600 if it is not already. Never raises.

    `write_cookies` sets the mode, but it only runs when a cookie is REWRITTEN —
    and the whole point of this branch is that the alive path rewrites nothing.
    A file created before this change (or by hand, or by an editor's save) stays
    at whatever mode it had, indefinitely, while holding a live credential. The
    live file was found at 0644 — world-readable on a shared machine.

    So the mode is asserted on every READ instead, which is the one event that
    happens on every single run. Cheap: an fstat and, almost always, nothing.
    """
    path = config.CEDARS_COOKIES_PATH
    try:
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            path.chmod(0o600)
            log.info(
                "cedars session: tightened %s from %o to 600 — it holds a live credential",
                path.name,
                mode,
            )
    except OSError:
        # A missing or unreadable file is the caller's problem, not this
        # function's; it resolves to a DEAD verdict a moment later.
        pass


def check_stored_session(
    *,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """`check_session` against whatever is in `CEDARS_COOKIES_PATH`."""
    harden_cookie_file()
    return check_session(cedars.load_stored_cookies(), url=url, transport=transport)


def describe_cookies(cookies: Mapping[str, str]) -> str:
    """Names and character counts. NEVER a value — see the module docstring."""
    if not cookies:
        return "none"
    return ", ".join(f"{name} ({len(value)} chars)" for name, value in sorted(cookies.items()))


def write_cookies(cookies: Mapping[str, str]) -> None:
    """Persist the cookie file ATOMICALLY (tmp file + `os.replace`).

    Two processes write this file now — the daily `sift` run and the 10-minute
    keep-alive — so a plain `write_text` has a real window in which the other
    one reads a truncated or empty JSON object and concludes it has no session.
    That would be a self-inflicted DEAD verdict, and a DEAD verdict is the one
    that reaches for a browser and overwrites things.

    Same construction as `source_health.save_health`, for the same reason: a
    reader sees the old file or the new one, never a half one.
    """
    path = config.CEDARS_COOKIES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(dict(cookies), f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        # Owner-only: this file is a live credential for as long as the session
        # lasts. chmod BEFORE the rename so there is no window where it is
        # readable at the final path.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        # Never leave a stray tmp file behind; the old file stays intact.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class SessionReport:
    """What `ensure_session` did. Carries no cookie values."""

    state: str
    #: For UNKNOWN: which of the causes above. None for ALIVE / DEAD.
    reason: str | None = None
    #: The browser a working cookie was pulled from, when one was.
    refreshed_from: str | None = None
    #: Browsers actually consulted. Empty unless the stored cookie was DEAD.
    tried: list[str] = field(default_factory=list)
    #: Browsers that yielded a PHPSESSID which then FAILED its probe. These are
    #: the pulls the old code would have written to disk.
    rejected: list[str] = field(default_factory=list)
    #: True when nothing was written (dry-run, or nothing needed writing).
    wrote_cookie: bool = False

    @property
    def ok(self) -> bool:
        return self.state == ALIVE

    def summary(self) -> str:
        if self.state == ALIVE and self.refreshed_from:
            return f"CEDARS session recovered from {self.refreshed_from}"
        if self.state == ALIVE:
            return "CEDARS session alive (stored cookie still accepted)"
        if self.reason == UNCONFIGURED:
            return (
                "CEDARS session NOT CHECKED — CEDARS_PORTAL_URL is unset, so nothing "
                "was requested. Set it in job-sift/.env; the stored cookie is untouched."
            )
        if self.state == UNKNOWN:
            # All THREE causes, because the reader's next move differs and an
            # earlier version named only two. The third — a body we could not
            # parse — is the one `sift`'s own comment block calls out.
            return (
                "CEDARS session state UNKNOWN — could not verify it (portal unreachable, "
                "or it answered with something unreadable); stored cookie left alone"
            )
        detail = f"; tried {', '.join(self.tried)}" if self.tried else ""
        if self.rejected:
            detail += f"; pulled a cookie from {', '.join(self.rejected)} but it was not accepted"
        return f"CEDARS session DEAD{detail}"


def ensure_session(
    *,
    first_browser: str | None = None,
    dry_run: bool = False,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> SessionReport:
    """Test the stored cookie FIRST; reach for a browser only if it is truly dead.

    NO FAILURE TO *DETERMINE* THE VERDICT ESCAPES. A transport error, a timeout,
    a 5xx, an unparseable body — all resolve to UNKNOWN, because the caller's
    next move is the same whether the probe failed or the session is gone, and
    the orchestrator must still run the other sources either way.

    WHAT CAN STILL RAISE, and why it is not collapsed into a verdict: writing
    the recovered cookie. If `write_cookies` fails (disk full, read-only mount,
    a permissions change), we have verified a live session and then failed to
    keep it. Returning ALIVE would be a lie about what the next fetch will do —
    the stored cookie is still the dead one — and returning DEAD would be a lie
    about what we observed. Neither verdict is true, so the exception is left to
    propagate and `main` maps it to its own exit code. This is the one case
    where "I could not tell you" is genuinely a third thing.
    """
    state = check_stored_session(url=url, transport=transport)

    if state == ALIVE:
        log.debug("cedars session: stored cookie is alive — not touching it")
        return SessionReport(state=ALIVE)

    if state == UNKNOWN:
        # THE LOAD-BEARING BRANCH. No refresh, no write, no recorded death.
        #
        # Whether the portal was CONFIGURED is a local fact, knowable without
        # asking the network, so it is settled here rather than threaded back
        # out of the probe. That keeps `check_session`'s return type a plain
        # verdict while still letting the caller say something true.
        reason = UNREACHABLE if (url or config.CEDARS_PORTAL_URL) else UNCONFIGURED
        log.debug("cedars session: could not determine liveness (%s)", reason)
        return SessionReport(state=UNKNOWN, reason=reason)

    # DEAD — and only now do we go looking.
    report = SessionReport(state=DEAD)
    for browser in browser_order(first_browser):
        report.tried.append(browser)
        try:
            pulled = cedars_pull(browser)
        except Exception as exc:  # noqa: BLE001 — a locked keyring is not fatal
            log.debug("cedars session: could not read cookies from %s (%s)", browser, exc)
            continue
        if "PHPSESSID" not in pulled:
            log.debug("cedars session: no PHPSESSID in %s", browser)
            continue

        # A pull proves a cookie EXISTS. Only a probe proves CEDARS takes it.
        verdict = check_session(pulled, url=url, transport=transport)
        if verdict != ALIVE:
            log.debug(
                "cedars session: %s had a session cookie (%s) but CEDARS answered %s "
                "— NOT writing it over the stored one",
                browser,
                describe_cookies(pulled),
                verdict,
            )
            report.rejected.append(browser)
            continue

        if not dry_run:
            write_cookies(pulled)
            report.wrote_cookie = True
        log.debug(
            "cedars session: recovered from %s (%s)%s",
            browser,
            describe_cookies(pulled),
            "" if report.wrote_cookie else " [dry-run: not written]",
        )
        report.state = ALIVE
        report.refreshed_from = browser
        return report

    log.debug("cedars session: no live session in any of: %s", ", ".join(report.tried) or "no browser")
    return report


def cedars_pull(browser: str) -> dict[str, str]:
    """Indirection point so tests can patch the browser layer in one place."""
    from job_sift import refresh_cookie

    return refresh_cookie.pull_cookies(browser)


EXIT_ALIVE = 0
EXIT_DEAD = 1
#: Also the code for an unexpected internal failure — see `main`.
EXIT_UNKNOWN = 2


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m job_sift.session` — used by the `sift` wrapper.

    Exit code IS the contract, and UNKNOWN gets its own so the caller can tell a
    dead session (show the operator the log-back-in banner) from an unreachable
    portal (say so quietly and carry on with what we have).
    """
    parser = argparse.ArgumentParser(
        prog="job_sift.session",
        description="Test the stored CEDARS session; refresh from a browser only if it is dead.",
    )
    parser.add_argument("--browser", help="browser to try first when the stored cookie is dead")
    parser.add_argument("--dry-run", action="store_true", help="never write the cookie file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    args = parser.parse_args(argv)

    from job_sift.keepalive import configure_logging

    configure_logging(verbose=args.verbose)

    try:
        report = ensure_session(first_browser=args.browser, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        # NOT exit 1. Exit 1 means "CEDARS rejected the session", and `sift`
        # answers it with a banner telling the operator to go log in again. An
        # OSError from `write_cookies` would then print "SESSION DEAD" about a
        # session we had just verified was ALIVE and merely failed to persist —
        # one exit code carrying two incompatible meanings, which is the exact
        # overloading this branch exists to remove, reappearing at the process
        # boundary. Exit 2 already means "we cannot vouch for the session";
        # an internal failure belongs there, and `sift` proceeds rather than
        # shouting.
        # `exception`, not `error`: unlike the disciplined happy path this is
        # genuinely unexpected, and the traceback is the whole value. Same
        # reasoning as `keepalive.main`.
        log.exception("cedars session: unexpected failure")
        print(f"CEDARS session check failed unexpectedly: {type(exc).__name__}", file=sys.stderr)
        return EXIT_UNKNOWN
    print(report.summary())
    return {ALIVE: EXIT_ALIVE, DEAD: EXIT_DEAD}.get(report.state, EXIT_UNKNOWN)


if __name__ == "__main__":
    sys.exit(main())
