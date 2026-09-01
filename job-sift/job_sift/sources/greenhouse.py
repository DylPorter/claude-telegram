"""Greenhouse careers-page poller.

For each company-slug in config/companies.yaml, hits the public Greenhouse
boards API and emits JobListing objects. Listings from this source are
treated as "curated prestige" by the orchestrator — they bypass the prestige
classifier and only get scope-checked.

API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false
Public, no auth, rate-limit-friendly (we hit each slug once per run).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

from job_sift.errors import SourceFetchError
from job_sift.schema import JobListing
from job_sift.sources._ats_common import load_slugs, location_matches

log = logging.getLogger(__name__)


_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_TIMEOUT = 20.0
_DESC_CHAR_CAP = 4000  # truncate descriptions before classifier — full text bloats LLM context with no upside


def _strip_html(html: str | None) -> str | None:
    if not html:
        return None
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_DESC_CHAR_CAP] if text else None


def _parse_updated_at(s: str | None) -> date | None:
    if not s:
        return None
    try:
        # Greenhouse uses ISO 8601 with timezone, e.g. "2026-05-20T11:42:00-04:00"
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _fetch_company(slug: str, *, client: httpx.Client) -> list[dict] | None:
    """Jobs for one board, or None if the board could not be reached/read.

    None and [] are deliberately different: None is "I could not look", [] is
    "this board has no jobs". The caller counts the Nones.
    """
    url = f"{_API_BASE}/{slug}/jobs"
    try:
        # content=true returns each job's HTML description in the same call.
        # Lets the classifier read the body for ambiguous titles without
        # an extra HTTP round-trip per listing.
        resp = client.get(url, params={"content": "true"})
    except httpx.HTTPError as exc:
        log.warning("greenhouse: %s — network error: %s", slug, exc)
        return None

    if resp.status_code == 404:
        log.warning("greenhouse: %s — 404 (invalid slug?)", slug)
        return None
    if resp.status_code != 200:
        log.warning("greenhouse: %s — HTTP %d", slug, resp.status_code)
        return None

    try:
        return resp.json().get("jobs", []) or []
    except ValueError:
        log.warning("greenhouse: %s — non-JSON response", slug)
        return None


def fetch_greenhouse_listings() -> list[JobListing]:
    """Public entry point. Polls every configured Greenhouse company and
    returns location-filtered listings.

    PARTIAL degrade, TOTAL escalation. A single dead slug is logged, skipped,
    and the other boards still report. But if EVERY configured slug failed we
    raise `SourceFetchError` rather than returning `[]`: `httpx` wraps
    `socket.gaierror` in `ConnectError` (an `HTTPError`), so a total network
    outage otherwise comes back as a clean empty list, stays out of the
    orchestrator's error map, and is scored by `source_health` as a SUCCESS —
    zeroing the failure streak and writing a fabricated `last_success`.
    Returning zero must mean we looked.
    """
    slugs = load_slugs("greenhouse")
    if not slugs:
        return []

    log.info("greenhouse: polling %d companies", len(slugs))

    listings: list[JobListing] = []
    failed = 0
    with httpx.Client(timeout=_TIMEOUT) as client:
        for slug in slugs:
            jobs = _fetch_company(slug, client=client)
            if jobs is None:
                failed += 1
                continue
            kept = 0
            for j in jobs:
                location_name = (j.get("location") or {}).get("name")
                if not location_matches(location_name):
                    continue
                title = j.get("title", "").strip()
                ext_id = str(j.get("id", "")).strip()
                if not ext_id or not title:
                    continue
                listings.append(
                    JobListing(
                        source="greenhouse",
                        external_id=f"{slug}/{ext_id}",
                        # Use company_name from the API if present, else titleize the slug.
                        employer=j.get("company_name") or slug.replace("_", " ").title(),
                        title=title,
                        apply_url=j.get("absolute_url", f"{_API_BASE}/{slug}/jobs/{ext_id}"),
                        posting_date=_parse_updated_at(j.get("updated_at")),
                        deadline=None,  # Greenhouse rarely surfaces this
                        location=location_name,
                        description=_strip_html(j.get("content")),
                        raw={"slug": slug, "departments": j.get("departments"), "offices": j.get("offices")},
                    )
                )
                kept += 1
            log.info("greenhouse: %s — %d jobs total, %d after location filter", slug, len(jobs), kept)

    if failed == len(slugs):
        raise SourceFetchError(
            "greenhouse",
            f"all {len(slugs)} configured board(s) failed to fetch — "
            "network outage or the Greenhouse API is unreachable (see log for per-slug detail)",
        )

    log.info("greenhouse: %d total listings after filtering", len(listings))
    return listings
