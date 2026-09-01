"""Ashby careers-page poller.

API: https://api.ashbyhq.com/posting-api/job-board/{slug}
Public, no auth. Response shape:
  {
    "apiVersion": "1",
    "jobs": [
      {
        "id": "043d6a58-87a1-4e3c-bf47-4dc351b94cf4",
        "title": "Software Engineer",
        "location": "San Francisco",
        "employmentType": "FullTime" | "Intern" | "Contract" | ...,
        "isRemote": false,
        "workplaceType": "Onsite" | "Remote" | "Hybrid",
        "jobUrl": "https://jobs.ashbyhq.com/...",
        "publishedAt": "2026-04-23T21:08:09.241+00:00",
        ...
      }
    ]
  }

The structured `employmentType` field is unusually clean — we can use it
directly as a hint for the scope classifier.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import httpx

from job_sift.errors import SourceFetchError, SourceNotConfiguredError
from job_sift.schema import JobListing
from job_sift.sources._ats_common import load_slugs, location_matches

_DESC_CHAR_CAP = 4000


def _trim(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    return s[:_DESC_CHAR_CAP] if s else None

log = logging.getLogger(__name__)

_API = "https://api.ashbyhq.com/posting-api/job-board"
_TIMEOUT = 20.0


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _fetch_company(slug: str, *, client: httpx.Client) -> list[dict] | None:
    """Jobs for one board, or None if the board could not be reached/read.

    None and [] are deliberately different: None is "I could not look", [] is
    "this board has no jobs". The caller counts the Nones.
    """
    url = f"{_API}/{slug}"
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        log.warning("ashby: %s — network error: %s", slug, exc)
        return None
    if resp.status_code == 404:
        log.warning("ashby: %s — 404 (invalid slug?)", slug)
        return None
    if resp.status_code != 200:
        log.warning("ashby: %s — HTTP %d", slug, resp.status_code)
        return None
    try:
        return resp.json().get("jobs", []) or []
    except ValueError:
        log.warning("ashby: %s — non-JSON response", slug)
        return None


def fetch_ashby_listings() -> list[JobListing]:
    """Public entry point. Polls every configured Ashby board.

    PARTIAL degrade, TOTAL escalation. A single dead slug is logged, skipped,
    and the other boards still report. But if EVERY configured slug failed we
    raise `SourceFetchError` rather than returning `[]`: `httpx` wraps
    `socket.gaierror` in `ConnectError` (an `HTTPError`), so a total network
    outage otherwise comes back as a clean empty list, stays out of the
    orchestrator's error map, and is scored by `source_health` as a SUCCESS —
    zeroing the failure streak and writing a fabricated `last_success`.
    Returning zero must mean we looked.

    A THIRD outcome sits alongside those two: with no slugs configured there is
    nothing to poll, so the run learnt nothing about this source either way.
    That raises `SourceNotConfiguredError`, which the orchestrator scores as
    neither a success nor a failure — see errors.py.
    """
    slugs = load_slugs("ashby")
    if not slugs:
        # NOT `return []`. An empty return is scored a SUCCESS by source_health
        # — it resets the streak and stamps today as `last_success` — and with
        # no slugs we polled nothing and learnt nothing. "No config" is neither
        # success nor failure, so escalate and let the orchestrator prune it.
        raise SourceNotConfiguredError("ashby", "companies.yaml has no `ashby:` slugs — nothing to poll")
    log.info("ashby: polling %d companies", len(slugs))

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
                # Skip listings the company has hidden
                if j.get("isListed") is False:
                    continue
                location_name = j.get("location") or j.get("locationName")
                if not location_matches(location_name, is_remote=bool(j.get("isRemote"))):
                    continue
                title = (j.get("title") or "").strip()
                ext_id = str(j.get("id", "")).strip()
                if not ext_id or not title:
                    continue
                listings.append(
                    JobListing(
                        source="ashby",
                        external_id=f"{slug}/{ext_id}",
                        employer=slug.replace("_", " ").replace("-", " ").title(),
                        title=title,
                        apply_url=j.get("applyUrl") or j.get("jobUrl") or f"{_API}/{slug}/{ext_id}",
                        posting_date=_parse_iso_date(j.get("publishedAt")),
                        deadline=None,
                        location=location_name,
                        description=_trim(j.get("descriptionPlain")),
                        raw={
                            "slug": slug,
                            "employmentType": j.get("employmentType"),
                            "workplaceType": j.get("workplaceType"),
                            "isRemote": j.get("isRemote"),
                        },
                    )
                )
                kept += 1
            log.info("ashby: %s — %d jobs, %d after filter", slug, len(jobs), kept)

    if failed == len(slugs):
        raise SourceFetchError(
            "ashby",
            f"all {len(slugs)} configured board(s) failed to fetch — "
            "network outage or the Ashby API is unreachable (see log for per-slug detail)",
        )

    log.info("ashby: %d total listings after filtering", len(listings))
    return listings
