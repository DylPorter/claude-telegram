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
    """
    if not location_str:
        return True
    s = location_str.lower()
    allow = load_location_allowlist()
    return any(allow_term in s for allow_term in allow)
