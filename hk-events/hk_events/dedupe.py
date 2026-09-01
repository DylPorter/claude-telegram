"""Diff incoming events against a persisted seen-set.

Mirrors job-sift/dedupe.py, with one deliberate divergence: a job posting has ONE
relevant moment (you saw it, you evaluated it), but an event has TWO — when you
discover it, and when it's about to happen. The original implementation only
modelled the first, so an event seen once in June and forgotten was never shown
again. That made this a *new-events* digest wearing an *upcoming-events* costume.

State shape (per source, JSON at .data/state/seen_<source>.json):

    {"<external_id>": {"stages": ["new", "soon"], "tag": "founder_ai"}}

`stages` records which notifications have already fired for that event, so each
one fires at most once. `tag` caches the classifier verdict from the discovery
run so the T-minus reminder doesn't pay for a second LLM call — and, more
importantly, so an event the filter already rejected is never resurfaced.

Legacy state (a bare JSON list of ids, the pre-2026-08-09 format) is migrated on
read into {"stages": ["new"], "tag": None}. A null tag means "verdict unknown",
which the orchestrator handles by re-classifying at reminder time.

NOTE: the seen-set suppresses re-SURFACING to Telegram. Calendar-write
idempotency is a separate concern handled in calendar_sync.py (keyed on
Event.stable_hash) so that re-runs never duplicate calendar entries even if the
seen-set is wiped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hk_events.config import HK_EVENTS_REMINDER_DAYS, STATE_DIR
from hk_events.schema import Event

log = logging.getLogger(__name__)

STAGE_NEW = "new"
STAGE_SOON = "soon"


def _seen_path(source: str) -> Path:
    return STATE_DIR / f"seen_{source}.json"


def _log_path() -> Path:
    return STATE_DIR / "relevance_log.jsonl"


def _migrate(raw) -> dict[str, dict]:
    """Accept either the legacy list-of-ids format or the current dict format."""
    if isinstance(raw, list):
        log.info("migrating %d ids from legacy list-format seen-set", len(raw))
        return {str(i): {"stages": [STAGE_NEW], "tag": None} for i in raw}
    if isinstance(raw, dict):
        # Guard against hand-edited/partial records.
        out: dict[str, dict] = {}
        for key, rec in raw.items():
            if isinstance(rec, dict):
                out[key] = {
                    "stages": list(rec.get("stages") or [STAGE_NEW]),
                    "tag": rec.get("tag"),
                }
            else:
                out[key] = {"stages": [STAGE_NEW], "tag": None}
        return out
    return {}


def load_seen(source: str) -> dict[str, dict]:
    p = _seen_path(source)
    if not p.exists():
        return {}
    try:
        return _migrate(json.loads(p.read_text()))
    except Exception as exc:
        log.warning("failed to load seen-set for %s: %s — starting fresh", source, exc)
        return {}


def save_seen(source: str, seen: dict[str, dict]) -> None:
    _seen_path(source).write_text(json.dumps(seen, indent=2, sort_keys=True))


# Which source wins when two of them carry the same real-world event and NEITHER
# has been seen before. Earlier = preferred. `luma` outranks `luma_discover`
# because the .ics carries a description and an organizer, which the city-page
# listing card does not, and the classifier reads both.
#
# This ordering is only ever the TIE-BREAK. Continuity outranks it — see
# collapse_cross_source.
_SOURCE_PRECEDENCE = ["meetup", "luma", "aitinkerers", "luma_discover", "cyberport", "startmeuphk"]


def _precedence(event: Event) -> tuple[int, str]:
    try:
        rank = _SOURCE_PRECEDENCE.index(event.source)
    except ValueError:
        rank = len(_SOURCE_PRECEDENCE)
    return (rank, event.source)


def collapse_cross_source(
    events: list[Event], *, seen_lookup=load_seen
) -> tuple[list[Event], list[tuple[Event, Event]]]:
    """Collapse events that two sources both reported, BEFORE the seen-set diff.

    Returns `(kept, collapsed)`, where each `collapsed` pair is `(kept, dropped)`
    so the caller can log what it merged.

    WHY THIS EXISTS. Nothing in this pipeline used to compare events across
    sources. `dedup_key` is source-prefixed, `stable_hash` mixes the source into
    the digest, and `filter_due` loads a SEPARATE seen-set per source — so two
    adapters holding one event produced two "new event" notifications, two
    classifier calls, and two Google Calendar inserts. That was latent until
    `luma_discover` landed: the city page and the calendar .ics feeds genuinely
    overlap, and a standalone event getting attached to a followed calendar puts
    it in both. Verified in live data 2026-09-01 — `evt-cuDFACZOa8zGKRu`
    ("Paperclip-maxxing Capitalism") was on the startupshk .ics and on
    lu.ma/hong-kong simultaneously.

    The collision key is `Event.identity_key`; for the Luma pair that resolves to
    `luma-evt:<api_id>`, which both adapters derive independently.

    CONTINUITY BEATS PRECEDENCE, and that is the subtle half. Picking a winner by
    a fixed source ranking alone would trade a same-run double-report for a
    across-run one: an event first found by `luma_discover` is recorded in
    `seen_luma_discover.json`, so if a later run switched the winner to `luma`,
    that run would look the event up in `seen_luma.json`, miss, and notify it a
    second time — plus write a second calendar entry, since `stable_hash` mixes
    the source in. So a candidate whose OWN source has already seen it wins. The
    fixed ranking only decides a genuinely first sighting, where no seen-set has
    an opinion and either choice is equally new.

    `seen_lookup` is injected so tests can drive this without touching
    `.data/state/`.
    """
    by_identity: dict[str, list[Event]] = {}
    order: list[str] = []
    for event in events:
        key = event.identity_key
        if key not in by_identity:
            by_identity[key] = []
            order.append(key)
        by_identity[key].append(event)

    seen_cache: dict[str, dict[str, dict]] = {}

    def _already_seen(event: Event) -> bool:
        if event.source not in seen_cache:
            seen_cache[event.source] = seen_lookup(event.source)
        return event.external_id in seen_cache[event.source]

    kept: list[Event] = []
    collapsed: list[tuple[Event, Event]] = []
    for key in order:
        group = by_identity[key]
        if len(group) == 1:
            kept.append(group[0])
            continue
        # sorted() is stable, so equal keys preserve fetch order — the result is
        # deterministic even when two candidates tie on both criteria.
        winner = sorted(group, key=lambda e: (not _already_seen(e), _precedence(e)))[0]
        kept.append(winner)
        for other in group:
            if other is not winner:
                collapsed.append((winner, other))
                log.info(
                    "collapsed duplicate %s: keeping %s/%s, dropping %s/%s (%s)",
                    key, winner.source, winner.external_id,
                    other.source, other.external_id, winner.title[:60],
                )
    return kept, collapsed


def _is_soon(event: Event, *, now: datetime | None = None) -> bool:
    """True if the event starts within the reminder window and hasn't started yet.

    Events with no start time can never trigger a reminder — there's nothing to
    count down to, and guessing would spam the digest.
    """
    if event.start is None:
        return False
    now = now or datetime.now(timezone.utc)
    start = event.start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return now <= start <= now + timedelta(days=HK_EVENTS_REMINDER_DAYS)


def filter_due(
    events: list[Event], *, now: datetime | None = None
) -> tuple[list[tuple[Event, str, str | None]], dict[str, dict[str, dict]]]:
    """Return (events due for a notification, per-source-updated-seen-sets).

    Each due entry is (event, stage, cached_tag):
      - stage STAGE_NEW  — first time we've seen it. cached_tag is always None.
      - stage STAGE_SOON — starts within HK_EVENTS_REMINDER_DAYS and we haven't
        reminded yet. cached_tag is the stored verdict, or None if unknown
        (legacy state), in which case the caller should re-classify.

    Events whose cached verdict was "drop" are never resurfaced — the filter
    already rejected them once and a countdown doesn't make them relevant.

    Caller persists seen sets AFTER classification + push so that a failed push
    doesn't permanently mark events as notified.
    """
    seen_by_source: dict[str, dict[str, dict]] = {}
    due: list[tuple[Event, str, str | None]] = []

    for event in events:
        # NOT setdefault: its default arg is evaluated eagerly, so load_seen()
        # would re-read and re-migrate the whole state file once per event.
        if event.source not in seen_by_source:
            seen_by_source[event.source] = load_seen(event.source)
        seen = seen_by_source[event.source]
        rec = seen.get(event.external_id)

        if rec is None:
            seen[event.external_id] = {"stages": [STAGE_NEW], "tag": None}
            due.append((event, STAGE_NEW, None))
            continue

        stages = rec.setdefault("stages", [STAGE_NEW])
        if STAGE_SOON in stages:
            continue
        if not _is_soon(event, now=now):
            continue
        if rec.get("tag") == "drop":
            continue

        stages.append(STAGE_SOON)
        due.append((event, STAGE_SOON, rec.get("tag")))

    return due, seen_by_source


def record_verdict(seen_by_source: dict[str, dict[str, dict]], event: Event, tag: str) -> None:
    """Cache the classifier verdict so the reminder pass can reuse it."""
    rec = seen_by_source.get(event.source, {}).get(event.external_id)
    if rec is not None:
        rec["tag"] = tag


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
