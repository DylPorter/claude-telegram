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

      * A tableless PAGE 1 raises `SourceFetchError`. Page 1 is the only page a
        healthy portal must always render with a table (even with zero open
        listings the header row is there), so no table on page 1 means we never
        reached the listings at all. Escalating it is what stops a
        maintenance/interstitial page from being scored a success and reported
        as "Surfaced: none today".
      * A tableless PAGE 2+ stops pagination and keeps what pages 1..N-1
        returned. By then we hold POSITIVE evidence the source is alive and the
        run reports real listings, so this is a partial degrade, not a silent
        zero — and paginating past the last page is a legitimate way to get a
        page the parser does not recognise. Escalating it would turn a normal
        end-of-results into a failed source.
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
            # No results table — we did not read a listings page.
            if page == 1:
                raise SourceFetchError(
                    "cedars",
                    "the CEDARS listings page carried no results table "
                    f"(no `table.tablesorter` at {CEDARS_PORTAL_URL}). Either the "
                    "portal served a maintenance/interstitial page, the session "
                    "bounced somewhere other than login.php/main.php, or the "
                    "results-table markup changed. Open the URL in a browser to "
                    "tell those apart — this is NOT 'no jobs today'.",
                )
            log.warning(
                "cedars: page %d carried no results table — stopping after %d listing(s) "
                "from the earlier page(s)",
                page,
                len(all_listings),
            )
            break

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
