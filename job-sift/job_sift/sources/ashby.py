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


def _fetch_company(slug: str, *, client: httpx.Client) -> list[dict]:
    url = f"{_API}/{slug}"
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        log.warning("ashby: %s — network error: %s", slug, exc)
        return []
    if resp.status_code == 404:
        log.warning("ashby: %s — 404 (invalid slug?)", slug)
        return []
    if resp.status_code != 200:
        log.warning("ashby: %s — HTTP %d", slug, resp.status_code)
        return []
    try:
        return resp.json().get("jobs", []) or []
    except ValueError:
        log.warning("ashby: %s — non-JSON response", slug)
        return []


def fetch_ashby_listings() -> list[JobListing]:
    slugs = load_slugs("ashby")
    if not slugs:
        return []
    log.info("ashby: polling %d companies", len(slugs))

    listings: list[JobListing] = []
    with httpx.Client(timeout=_TIMEOUT) as client:
        for slug in slugs:
            jobs = _fetch_company(slug, client=client)
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

    log.info("ashby: %d total listings after filtering", len(listings))
    return listings
