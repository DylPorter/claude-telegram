"""Shared scaffolding for standardized-ATS adapters (Greenhouse, Lever, Ashby).

These vendors all expose public job-board JSON APIs with a similar pattern:
  1. We curate a per-vendor list of company slugs in companies.yaml
  2. For each slug, GET a documented public endpoint
  3. Filter by location against a shared allowlist
  4. Convert to JobListing objects

This module hosts the parts that DON'T vary across vendors: config loading
and location matching. Vendor-specific URL / response-parsing lives in
greenhouse.py, lever.py, ashby.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from job_sift.config import PROJECT_ROOT
from job_sift.errors import SourceNotConfiguredError

log = logging.getLogger(__name__)


_CFG_CACHE: dict | None = None


def _load_companies_yaml() -> dict:
    """Memoized read of config/companies.yaml."""
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    cfg_path = PROJECT_ROOT / "config" / "companies.yaml"
    if not cfg_path.exists():
        log.warning("no companies.yaml at %s — ATS sources return 0 listings", cfg_path)
        _CFG_CACHE = {}
        return _CFG_CACHE
    with cfg_path.open() as f:
        _CFG_CACHE = yaml.safe_load(f) or {}
    return _CFG_CACHE


def load_slugs(vendor: str) -> list[str]:
    """Return the list of slugs configured for a given ATS vendor."""
    cfg = _load_companies_yaml()
    return list(cfg.get(vendor, []) or [])


def load_location_allowlist() -> list[str]:
    """Return the shared location allowlist (lowercased substrings)."""
    cfg = _load_companies_yaml()
    return [s.lower() for s in (cfg.get("location_allowlist", []) or [])]


def require_location_allowlist(vendor: str) -> list[str]:
    """The allowlist, or `SourceNotConfiguredError` if there isn't one.

    Sibling of each adapter's `if not slugs` check, and here for the same
    reason. An empty allowlist is NOT a filter that happens to match nothing:
    `location_matches` asks `any(term in location for term in allow)`, and
    `any()` over an empty sequence is False, so every listing that HAS a
    location is discarded. Verified against the real defect: with the allowlist
    emptied, `HK listing matches? False`.

    The adapter then polls every board successfully, keeps nothing, raises
    nothing, and returns `[]` — which the orchestrator puts in `succeeded` and
    `source_health` scores a SUCCESS, zeroing the failure streak and stamping a
    `last_success`. That is the same `[]`-means-two-things overload as the
    missing-slugs case one level up, and it gets the same answer: a config
    defect is neither "I looked and found nothing" nor "I could not look", so it
    goes in the third bucket and `update_health` prunes the source.

    Called BEFORE the first request, deliberately: there is nothing to learn
    from polling ten boards whose every result we are about to throw away.

    THE OPPOSITE ERROR IS EQUALLY BAD and is not committed here. A POPULATED
    allowlist that matches zero listings today is a real fetch of a real answer
    — pinned by `test_a_populated_allowlist_matching_nothing_today_is_still_a
    _success`. Only the empty/missing list raises.
    """
    allow = load_location_allowlist()
    if not allow:
        raise SourceNotConfiguredError(
            vendor,
            "companies.yaml has no `location_allowlist:` entries — with nothing "
            "to match on, every listing that has a location is filtered out and "
            "the source reports a fabricated zero. Nothing was polled.",
        )
    return allow


def location_matches(location_str: str | None, *, is_remote: bool = False) -> bool:
    """A listing passes the location filter if:
      - it has no location at all (let the classifier / human decide), OR
      - it's flagged as remote (is_remote=True passed from a vendor-specific
        signal like Ashby's `isRemote` field — but we still require the
        location string to indicate global remote, not country-restricted), OR
      - any allowlist substring appears in the location name (case-insensitive)

    The `is_remote` boolean alone isn't sufficient because vendors mark
    country-restricted-remote roles as `isRemote=True` (e.g., "Remote, US").
    We still need the string check to confirm global accessibility.

    Raises `SourceNotConfiguredError` on an empty/missing allowlist rather than
    silently answering False for everything — see `require_location_allowlist`.
    Every adapter calls that up front, so this guard is unreachable in a normal
    run; it is here so the check cannot be lost by a future adapter that forgets
    to make the up-front call. The vendor name is unknown at this depth, and the
    orchestrator keys its buckets on the TASK name rather than `exc.source`, so
    "ats" is a label for the log line, not a routing decision.
    """
    if not location_str:
        return True
    s = location_str.lower()
    allow = require_location_allowlist("ats")
    return any(allow_term in s for allow_term in allow)
