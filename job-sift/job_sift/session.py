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
        # Unconfigured is not a verdict on the cookie.
        log.warning("cedars session: CEDARS_PORTAL_URL is not set — cannot probe")
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

    verdict = classify_response(resp.status_code, resp.url.path, resp.text)
    log.debug("cedars session: probe -> %s (HTTP %s)", verdict, resp.status_code)
    return verdict


def check_stored_session(
    *,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """`check_session` against whatever is in `CEDARS_COOKIES_PATH`."""
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
        if self.state == UNKNOWN:
            return "CEDARS session state UNKNOWN — could not reach the portal; stored cookie left alone"
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

    Never raises: a caller's next move is the same whether this failed or the
    session is simply gone, and the orchestrator must still run the other
    sources either way.
    """
    state = check_stored_session(url=url, transport=transport)

    if state == ALIVE:
        log.debug("cedars session: stored cookie is alive — not touching it")
        return SessionReport(state=ALIVE)

    if state == UNKNOWN:
        # THE LOAD-BEARING BRANCH. No refresh, no write, no recorded death.
        log.info(
            "cedars session: could not determine liveness (transport/portal problem) "
            "— keeping the stored cookie and proceeding"
        )
        return SessionReport(state=UNKNOWN)

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
            log.info(
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
        log.info(
            "cedars session: recovered from %s (%s)%s",
            browser,
            describe_cookies(pulled),
            "" if report.wrote_cookie else " [dry-run: not written]",
        )
        report.state = ALIVE
        report.refreshed_from = browser
        return report

    log.warning("cedars session: no live session in any of: %s", ", ".join(report.tried))
    return report


def cedars_pull(browser: str) -> dict[str, str]:
    """Indirection point so tests can patch the browser layer in one place."""
    from job_sift import refresh_cookie

    return refresh_cookie.pull_cookies(browser)


EXIT_ALIVE = 0
EXIT_DEAD = 1
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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    report = ensure_session(first_browser=args.browser, dry_run=args.dry_run)
    print(report.summary())
    return {ALIVE: EXIT_ALIVE, DEAD: EXIT_DEAD}.get(report.state, EXIT_UNKNOWN)


if __name__ == "__main__":
    sys.exit(main())
