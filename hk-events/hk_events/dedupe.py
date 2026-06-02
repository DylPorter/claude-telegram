"""Diff incoming events against a persisted seen-set.

Mirrors job-sift/dedupe.py. Persists `seen_ids` per source as JSON files under
.data/state/. Two purposes:
1. Only surface NEW events each daily run (suppress noise).
2. Build the long-term log used to tune the relevance filter over time.

NOTE: the seen-set suppresses re-SURFACING to Telegram. Calendar-write
idempotency is a separate concern handled in calendar_sync.py (keyed on
Event.stable_hash) so that re-runs never duplicate calendar entries even if the
seen-set is wiped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from hk_events.config import STATE_DIR
from hk_events.schema import Event

log = logging.getLogger(__name__)


def _seen_path(source: str) -> Path:
    return STATE_DIR / f"seen_{source}.json"


def _log_path() -> Path:
    return STATE_DIR / "relevance_log.jsonl"


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


def filter_new(events: list[Event]) -> tuple[list[Event], dict[str, set[str]]]:
    """Return (only-new events, per-source-updated-seen-sets).

    Caller persists seen sets AFTER classification + push so that a failed push
    doesn't permanently mark events as seen.
    """
    seen_by_source: dict[str, set[str]] = {}
    new_events: list[Event] = []
    for event in events:
        seen = seen_by_source.setdefault(event.source, load_seen(event.source))
        if event.external_id in seen:
            continue
        new_events.append(event)
        seen.add(event.external_id)
    return new_events, seen_by_source


def log_classification(event: Event, result) -> None:
    """Append one relevance decision to the rolling JSONL log (filter tuning)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": event.source,
        "external_id": event.external_id,
        "title": event.title,
        "start": event.start.isoformat() if event.start else None,
        "tag": result.tag,
        "reason": result.reason,
    }
    with _log_path().open("a") as f:
        f.write(json.dumps(entry) + "\n")
