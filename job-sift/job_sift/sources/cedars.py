"""HKU CEDARS NETJobs scraper.

Live scraper: fetches the CEDARS listings page with `httpx` using a stored
session cookie (`CEDARS_COOKIES_PATH`, refreshed daily by `refresh_cookie.py`
— see README's "Cookie refresh" section) and parses the `table.tablesorter`
results table with BeautifulSoup. Paginates greedily — see
`fetch_cedars_listings` for the stop conditions and the `JOB_SIFT_CEDARS_*`
env overrides.

`_stub_listings()` returns hardcoded sample data instead of scraping, for
end-to-end pipeline testing without live CEDARS access. It is reachable only
when `JOB_SIFT_STUB=1` is set — every other invocation hits the real portal.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from job_sift.config import CEDARS_COOKIES_PATH, CEDARS_PORTAL_URL
from job_sift.errors import SourceAuthError, SourceFetchError
from job_sift.schema import JobListing

log = logging.getLogger(__name__)


def _load_cookies() -> dict[str, str]:
    """Load the stored CEDARS session cookie(s) from `CEDARS_COOKIES_PATH`.

    Normal path: `refresh_cookie.py` writes a JSON object of name → value
    (e.g. `{"PHPSESSID": "...", "esd_from_sys": "..."}`), pulled from a
    locally logged-in browser — see README. Also accepts the legacy
    list-of-objects shape from a manual EditThisCookie-style export:
        [
          {"name": "...", "value": "...", "domain": "...", ...},
          ...
        ]

    Returns dict of name → value for the relevant domain. Domain filtering is
    skipped at this layer — httpx + the URL host take care of cookie scope.
    """
    if not CEDARS_COOKIES_PATH.exists():
        return {}
    try:
        raw = json.loads(CEDARS_COOKIES_PATH.read_text())
        if isinstance(raw, dict):
            return raw  # already name → value
        return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
    except Exception as exc:
        log.warning("failed to load cedars cookies: %s", exc)
        return {}


def _fetch_listings_page(page: int = 1) -> str:
    """GET one CEDARS listings page (?page=N) using stored cookies. Returns HTML."""
    cookies = _load_cookies()
    if not cookies:
        raise RuntimeError(
            f"no cedars cookies at {CEDARS_COOKIES_PATH} — log into "
            "https://web2.cedars.hku.hk/jobs/ in Firefox, then run ./sift "
            "(it refreshes the cookie automatically)."
        )
    url = CEDARS_PORTAL_URL
    if page > 1:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}page={page}"
    with httpx.Client(cookies=cookies, follow_redirects=True, timeout=30.0) as client:
        resp = client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"cedars fetch failed: HTTP {resp.status_code} for page {page}")
        # Detect the logged-out bounce: an expired PHPSESSID makes the search
        # page 302 → login.php → main.php, landing on a 200 landing page with
        # no results table. This guard names the cause; without it the tableless
        # page still escalates (see _parse_listings_html), but as a generic
        # "structure change" that does not tell the operator to log back in.
        # The whitelist is deliberately NOT the only defence — it is two
        # filenames, and a third bounce target must not go quiet.
        final_page = resp.url.path.rsplit("/", 1)[-1]
        if final_page in {"login.php", "main.php"}:
            raise SourceAuthError(
                "cedars",
                "CEDARS session cookie expired — request was redirected to "
                f"{final_page}. Log into https://web2.cedars.hku.hk/jobs/ in "
                "Firefox (a successful cookie refresh does not mean a valid "
                "session) and re-run ./sift.",
            )
        return resp.text


_DETAIL_BASE = "https://web2.cedars.hku.hk/jobs/"

# The NETjobs search endpoint. Every search/filter/sort control on the portal
# posts or links here, so a form pointing at it is the portal's own search
# surface — not a page that merely mentions CEDARS.
_SEARCH_ENDPOINT = "search.php"

# The portal's industry / job-type navigation.
_MEGA_MENU_SELECTOR = "#mega-menu-1"


def _is_portal_page(html: str) -> bool:
    """Did this HTML come from the CEDARS NETjobs portal?

    DIAGNOSIS ONLY. This chooses the wording of an error, never whether to
    raise one. A missing `table.tablesorter` is a failure on every page number
    whatever this returns — see `fetch_cedars_listings`. The distinction it
    draws is still worth drawing, because the two causes need opposite
    responses from the operator: "your session is bouncing you off NETjobs" vs
    "you are on NETjobs and the results table is not where it used to be".

    An earlier cut of this function decided the raise, on the theory that portal
    chrome plus no table meant we had walked off the end of the pagination. That
    was wrong, and wrong in the direction that matters: a CEDARS-served
    maintenance page carries CEDARS chrome, so it satisfied every anchor below,
    took the quiet arm, and returned `[]` — reopening the fifty-day silent zero
    on page 1 for two of the three causes the error message itself named. The
    refutation is in this module's own reasoning: the template emits the table
    shell even at zero rows, so end-of-results is a table with NO ROWS, which
    `if not page_listings: break` already handles. "Chrome but no table at all"
    therefore has no legitimate producer.

    THE ANCHORS, read off the captured live page (see
    tests/fixtures/cedars_listings_page.html):

      * a `<form>` whose action is `search.php` — the NETjobs search surface.
        The live page emits two: the header keyword box, and the filter form
        inside `#content` directly above the results table.
      * `#mega-menu-1` — the portal's industry / job-type mega menu.

    NOT INDEPENDENT, despite serving as alternatives. In the real document
    `form#search_form` sits under `div#search < div#box < div#container` and
    `div#mega-menu-1` under `div.nav < div#container` — adjacent siblings from
    one shared header include, so a header redesign takes both at once. The only
    structurally separate instance is the `#content` filter form, and it is
    emitted by the same results template as the table, so it is the LEAST likely
    of the three to outlive a page that has lost its table. Treat these as one
    signal with redundant spellings, not as two witnesses; the claim of
    independence was overstated and is retracted here. It costs little now that
    the return value only picks a sentence.

    EVENT-INDEPENDENT, which still matters: the anchors say nothing about
    whether any listing is present, so the diagnosis reads the same on a busy
    day and a dead-quiet one.
    """
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        if _SEARCH_ENDPOINT in (form.get("action") or ""):
            return True
    return soup.select_one(_MEGA_MENU_SELECTOR) is not None


def _safe_date(s: str) -> date | None:
    s = s.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_listings_html(html: str) -> list[JobListing] | None:
    """Parse the CEDARS listings table into JobListing objects.

    Table layout (inferred from live page):
      - <table class="tablesorter"> with 1 header row + 20 data rows per page
      - 5 cells: Job ID, Employer, Title (with optional STEM badge img), Deadline, Posted
      - Every cell wraps the same <a href="job_detail.php?job_id=GXXXXXXX">

    RETURNS None vs `[]`, and the difference is the whole point:

      * `None` — there is NO results table on this page at all. We did not read
        a listings page; we read *something else*. A maintenance notice, a WAF
        interstitial, a bounce to a landing page whose filename is not in
        `_fetch_listings_page`'s two-name redirect whitelist, or a renamed
        `tablesorter` class (the layout above is inferred, not contracted).
        This is "I could not look", and the caller escalates it.
      * `[]` — the table IS there and holds zero data rows. That is a real,
        observed "nothing open today", and the caller scores it a success.

    Returning `[]` for both is the sixth variant of the bug that killed CEDARS
    for fifty days: `fetch_cedars_listings` read the empty list as "0 listings,
    stop", the orchestrator saw a clean return and put `cedars` in `succeeded`,
    and `source_health` zeroed the failure streak and stamped `last_success`.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.tablesorter")
    if table is None:
        log.warning(
            "no .tablesorter found — this is not a CEDARS listings page "
            "(maintenance/interstitial page, or the table markup changed)"
        )
        return None

    listings: list[JobListing] = []
    for row in table.select("tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue  # header row or malformed

        job_id = cells[0].get_text(strip=True)
        employer = cells[1].get_text(strip=True)
        # Title cell can contain a <img> for STEM badge — get_text strips it
        title = cells[2].get_text(strip=True)
        deadline = _safe_date(cells[3].get_text(strip=True))
        posted = _safe_date(cells[4].get_text(strip=True))

        detail_a = cells[0].find("a")
        if detail_a and detail_a.get("href"):
            apply_url = _DETAIL_BASE + detail_a["href"]
        else:
            apply_url = f"{_DETAIL_BASE}job_detail.php?job_id={job_id}"

        if not job_id:
            continue

        listings.append(
            JobListing(
                source="cedars",
                external_id=job_id,
                employer=employer,
                title=title,
                apply_url=apply_url,
                posting_date=posted,
                deadline=deadline,
                location="Hong Kong",
            )
        )

    log.info("parsed %d listings from CEDARS page", len(listings))
    return listings


def _stub_listings() -> list[JobListing]:
    """Sample data for end-to-end pipeline testing before the real scraper lands."""
    return [
        JobListing(
            source="cedars",
            external_id="G2503022",
            employer="Nvidia Singapore Pte Ltd",
            title="Machine Learning Intern - AI Agents Conversational AI",
            apply_url="https://nvidia.example/apply/G2503022",
            posting_date=date(2026, 5, 11),
            deadline=date(2026, 6, 12),
            location="Hong Kong",
        ),
        JobListing(
            source="cedars",
            external_id="G2503079",
            employer="WealthRyse (Hong Kong) Co., Limited",
            title="Tech Support Specialists",
            apply_url="https://wealthryse.example/apply/G2503079",
            posting_date=date(2026, 5, 18),
            deadline=date(2026, 6, 12),
            location="Hong Kong",
        ),
        JobListing(
            source="cedars",
            external_id="G2503066",
            employer="Andrew Lee King Fun & Associates Architects Limited",
            title="Software Engineer",
            apply_url="https://alkfa.example/apply/G2503066",
            posting_date=date(2026, 5, 14),
            deadline=date(2026, 6, 13),
            location="Hong Kong",
        ),
    ]


def fetch_cedars_listings(
    *,
    seen_ids: set[str] | None = None,
    max_pages: int | None = None,
) -> list[JobListing]:
    """Public entry point.

    Greedy pagination: fetch page 1, then page 2, etc., stopping when a full
    page contains zero new (unseen) listings OR when `max_pages` is reached.
    This keeps daily runs cheap when nothing new has posted, while still
    catching up automatically if the bot missed a few days.

    If `seen_ids` is None, fetches only the first page (no greedy logic) —
    used by stub mode and by initial-bootstrap runs.

    Env overrides (for a one-time deep catch-up after an outage, without
    changing the cheap daily defaults):
      - JOB_SIFT_CEDARS_MAX_PAGES : raise the page cap (default 5; ~11 pages
        cover all ~215 open listings).
      - JOB_SIFT_CEDARS_FULL=1    : disable the greedy 0-new stop, so it scans
        every page up to the cap even when the newest pages are all-seen. This
        is REQUIRED to reach the older backlog (pages 6+) once a normal run has
        already marked pages 1-5 as seen — otherwise greedy stops at page 1.

    PARTIAL degrade, TOTAL escalation — the same rule `_ical_common
    .fetch_feed_group` follows in the sibling bot:

      * NO `table.tablesorter` AT ALL raises `SourceFetchError`, on ANY page
        number, with no exceptions. A maintenance notice, a WAF interstitial, a
        bounce the redirect whitelist missed, or a renamed table class all mean
        we did not read the listings — on page 4 exactly as much as on page 1,
        and on page 4 it also means pages 5..N went unread. Escalating is what
        stops that being scored a success and reported as "Surfaced: none
        today". `_is_portal_page` picks the wording; it does not get a vote on
        whether to raise.
      * A TABLE WITH ZERO DATA ROWS returns `[]` and stops the walk. This is
        the ONLY legitimate end-of-results, and it is why the page-number rule
        was unnecessary rather than merely crude: the portal template emits the
        table shell even at zero rows, so walking off the end yields an empty
        table, which `if not page_listings: break` below already handles. There
        is no page shape left for which "no table at all" is a normal outcome.

    THE PAGE NUMBER IS NOT EVIDENCE and is no longer consulted. Neither is
    portal chrome: an earlier cut let chrome-plus-no-table stop quietly, which
    reopened the fifty-day silent zero on page 1 for a CEDARS-served maintenance
    page (which carries CEDARS chrome). See `_is_portal_page`.
    """
    if os.environ.get("JOB_SIFT_STUB") == "1":
        log.info("cedars: STUB mode — returning sample listings")
        return _stub_listings()

    if max_pages is None:
        max_pages = int(os.environ.get("JOB_SIFT_CEDARS_MAX_PAGES", "5"))
    full_scan = os.environ.get("JOB_SIFT_CEDARS_FULL") == "1"

    all_listings: list[JobListing] = []
    for page in range(1, max_pages + 1):
        log.info("cedars: fetching page %d", page)
        html = _fetch_listings_page(page)
        page_listings = _parse_listings_html(html)

        if page_listings is None:
            # NO RESULTS TABLE. Always a failure, on every page — the only
            # legitimate end-of-results is an EMPTY table, handled below.
            # `_is_portal_page` chooses which of the two causes to name; the
            # operator's next move differs, the raise does not.
            if _is_portal_page(html):
                detail = (
                    "we reached the NETjobs portal — its search form and menu are "
                    "on the page — but the results table was not there. Either the "
                    "portal served a maintenance/interstitial notice inside its own "
                    "chrome, or the results-table markup changed and "
                    "`table.tablesorter` no longer selects it."
                )
            else:
                detail = (
                    "the response carries none of the NETjobs chrome either, so we "
                    "were not on the portal at all. Either the session bounced "
                    "somewhere other than login.php/main.php, or something upstream "
                    "(WAF, captive portal, proxy error page) answered instead."
                )
            raise SourceFetchError(
                "cedars",
                f"page {page}: no results table (no `table.tablesorter` at "
                f"{CEDARS_PORTAL_URL}). {detail} Open the URL in a browser to "
                f"confirm — this is NOT 'no jobs today', and pages {page + 1}+ "
                "went unread.",
            )

        if not page_listings:
            log.info("cedars: page %d returned 0 listings — stopping", page)
            break

        all_listings.extend(page_listings)

        if full_scan:
            # Deep catch-up: keep going regardless of the seen-set frontier.
            log.info("cedars: page %d (full-scan) — %d listings", page, len(page_listings))
            continue

        # Greedy stop: if we know the seen-set and every listing on this page
        # is already in it, we've gone past the diff frontier — no point
        # fetching more pages.
        if seen_ids is not None:
            new_on_page = sum(1 for L in page_listings if L.external_id not in seen_ids)
            if new_on_page == 0:
                log.info("cedars: page %d had 0 new listings — stopping", page)
                break
            log.info("cedars: page %d had %d new listings", page, new_on_page)
        elif page >= 1:
            # No seen-set passed → only fetch first page
            break

    log.info("cedars: fetched %d total listings across %d page(s)", len(all_listings), page)
    return all_listings
