"""Lever careers-page poller.

API: https://api.lever.co/v0/postings/{slug}?mode=json
Public, no auth, similar shape to Greenhouse but the response is a flat
array (no wrapping object).

Response example (each posting):
  {
    "id": "abc-123",
    "text": "Software Engineer",
    "categories": {"location": "San Francisco", "team": "Engineering", ...},
    "hostedUrl": "https://jobs.lever.co/mistral/abc-123",
    "applyUrl": "https://jobs.lever.co/mistral/abc-123/apply",
    "createdAt": 1716000000000  # epoch ms
  }
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

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

_API = "https://api.lever.co/v0/postings"
_TIMEOUT = 20.0


def _epoch_ms_to_date(ms: int | None) -> date | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def _fetch_company(slug: str, *, client: httpx.Client) -> list[dict]:
    url = f"{_API}/{slug}"
    try:
        resp = client.get(url, params={"mode": "json"})
    except httpx.HTTPError as exc:
        log.warning("lever: %s — network error: %s", slug, exc)
        return []
    if resp.status_code == 404:
        log.warning("lever: %s — 404 (invalid slug?)", slug)
        return []
    if resp.status_code != 200:
        log.warning("lever: %s — HTTP %d", slug, resp.status_code)
        return []
    try:
        data = resp.json()
    except ValueError:
        log.warning("lever: %s — non-JSON response", slug)
        return []
    return data if isinstance(data, list) else []


def fetch_lever_listings() -> list[JobListing]:
    slugs = load_slugs("lever")
    if not slugs:
        return []
    log.info("lever: polling %d companies", len(slugs))

    listings: list[JobListing] = []
    with httpx.Client(timeout=_TIMEOUT) as client:
        for slug in slugs:
            postings = _fetch_company(slug, client=client)
            kept = 0
            for p in postings:
                location_name = (p.get("categories") or {}).get("location")
                if not location_matches(location_name):
                    continue
                title = (p.get("text") or "").strip()
                ext_id = str(p.get("id", "")).strip()
                if not ext_id or not title:
                    continue
                # Prefer plain-text version; fall back to additionalPlain;
                # last resort = raw "additional" (we don't bother stripping HTML
                # since Lever provides plain variants).
                desc = _trim(p.get("descriptionPlain") or p.get("additionalPlain"))
                listings.append(
                    JobListing(
                        source="lever",
                        external_id=f"{slug}/{ext_id}",
                        employer=slug.replace("_", " ").replace("-", " ").title(),
                        title=title,
                        apply_url=p.get("hostedUrl") or p.get("applyUrl") or f"{_API}/{slug}/{ext_id}",
                        posting_date=_epoch_ms_to_date(p.get("createdAt")),
                        deadline=None,
                        location=location_name,
                        description=desc,
                        raw={"slug": slug, "team": (p.get("categories") or {}).get("team")},
                    )
                )
                kept += 1
            log.info("lever: %s — %d postings, %d after location filter", slug, len(postings), kept)

    log.info("lever: %d total listings after filtering", len(listings))
    return listings
