"""Diff incoming listings against a persisted seen-set.

Persists `seen_ids` per source as JSON files under .data/state/. Two purposes:
1. Only surface NEW listings each daily run (suppress noise).
2. Build the long-term log used to derive a prestige whitelist in ~30 days.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from job_sift.config import STATE_DIR
from job_sift.schema import JobListing

log = logging.getLogger(__name__)


def _seen_path(source: str) -> Path:
    return STATE_DIR / f"seen_{source}.json"


def _log_path() -> Path:
    return STATE_DIR / "classifier_log.jsonl"


def load_seen(source: str) -> set[str]:
    p = _seen_path(source)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except Exception as exc:
        log.warning("failed to load seen-set for %s: %s — starting fresh", source, exc)
        return set()


def save_seen(source: str, seen: set[str]) -> None:
    _seen_path(source).write_text(json.dumps(sorted(seen), indent=2))


def filter_new(listings: list[JobListing]) -> tuple[list[JobListing], dict[str, set[str]]]:
    """Return (only-new listings, per-source-updated-seen-sets).

    Caller is expected to persist seen sets AFTER classification + push so that
    a failed push doesn't permanently mark listings as seen.
    """
    seen_by_source: dict[str, set[str]] = {}
    new_listings: list[JobListing] = []
    for listing in listings:
        seen = seen_by_source.setdefault(listing.source, load_seen(listing.source))
        if listing.external_id in seen:
            continue
        new_listings.append(listing)
        seen.add(listing.external_id)
    return new_listings, seen_by_source


def log_classification(listing: JobListing, result) -> None:
    """Append one classification decision to the rolling JSONL log.

    Used after ~30 days to derive a prestige whitelist organically (see
    feedback_internship_strategy memory).
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": listing.source,
        "external_id": listing.external_id,
        "employer": listing.employer,
        "title": listing.title,
        "prestige": result.prestige,
        "scope": result.scope,
        "reason": result.reason,
    }
    with _log_path().open("a") as f:
        f.write(json.dumps(entry) + "\n")
