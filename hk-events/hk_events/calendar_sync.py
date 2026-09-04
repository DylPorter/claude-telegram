"""Idempotent Google Calendar writes via the `gws` CLI.

Uses the `gws calendar +insert` helper (same `gws` binary signal-brief uses for
Gmail). Verified syntax (gws 0.22.5):

    gws calendar +insert \
        --calendar <CALENDAR_ID> \
        --summary <TITLE> \
        --start <RFC3339> \
        --end <RFC3339> \
        --location <TEXT> \
        --description <TEXT> \
        [--dry-run] [--format json]

`--dry-run` validates + prints the request body WITHOUT hitting the API
(confirmed: it returns {"dry_run": true, ...} and creates nothing). The helper
emits JSON; a successful real insert returns the created event object with an
"id" field.

IDEMPOTENCY
-----------
The `+insert` helper does not expose iCalUID / privateExtendedProperty, so we
de-dup ourselves: a JSON map at .data/cache/calendar_synced.json keyed on
Event.stable_hash → {created Google event id, summary, ts}. Before inserting we
check the map; if the hash is present we SKIP. This makes re-runs (and a wiped
seen-set) safe — no duplicate calendar entries.

GATING
------
Calendar writes are OFF unless HK_EVENTS_CALENDAR_ENABLED=1 in the env. When
disabled (the default) this module logs what it WOULD insert and records nothing
— so a fresh checkout never silently mutates the operator's real calendar.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from hk_events import config
from hk_events.config import GWS_BIN, HK_EVENTS_CALENDAR_ENABLED, HK_EVENTS_CALENDAR_ID
from hk_events.schema import Event

log = logging.getLogger(__name__)

GWS_TIMEOUT = 30.0

# Default event duration when a feed gives a start but no end (lots of scraped
# events omit it). 2h is a reasonable HK meetup default.
_DEFAULT_DURATION_HOURS = 2


# Resolved through the `config` MODULE on every call, never bound at import
# time — same reasoning as dedupe._seen_path. A test that points
# `config.CACHE_DIR` at a tmp_path must actually redirect the idempotency map,
# or it silently reads/writes the live deployment's calendar_synced.json.
def _synced_path() -> Path:
    return config.CACHE_DIR / "calendar_synced.json"


def _load_synced() -> dict[str, dict]:
    p = _synced_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        log.warning("failed to load calendar idempotency map: %s — starting fresh", exc)
        return {}


def _save_synced(data: dict[str, dict]) -> None:
    _synced_path().write_text(json.dumps(data, indent=2, sort_keys=True))


def _rfc3339(dt: datetime) -> str:
    """RFC3339 / ISO-8601 with offset, as `gws +insert --start/--end` expect."""
    return dt.isoformat()


def _build_description(event: Event) -> str:
    """Calendar body: registration/source URL first (the task requirement), then
    any event blurb."""
    parts = [f"Register / source: {event.url}"]
    if event.description:
        parts.append("")
        parts.append(event.description[:1500])
    parts.append("")
    parts.append(f"[auto-added by hk-events · source: {event.source}]")
    return "\n".join(parts)


def _insert_cmd(event: Event, *, dry_run: bool) -> list[str]:
    assert event.start is not None  # caller guarantees this
    end = event.end or event.start.replace(
        hour=min(event.start.hour + _DEFAULT_DURATION_HOURS, 23)
    )
    cmd = [
        GWS_BIN, "calendar", "+insert",
        "--calendar", HK_EVENTS_CALENDAR_ID,
        "--summary", event.title,
        "--start", _rfc3339(event.start),
        "--end", _rfc3339(end),
        "--description", _build_description(event),
        "--format", "json",
    ]
    if event.location:
        cmd += ["--location", event.location]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def _run_gws_insert(cmd: list[str]) -> dict | None:
    """Run the insert and return the parsed JSON response (or None on failure)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GWS_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        log.warning("gws calendar insert timed out")
        return None
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        if "invalid_grant" in err or "Token has been expired" in err:
            log.error("gws auth expired — run `gws auth login` to refresh")
        else:
            log.warning("gws calendar insert failed (rc=%d): %s", proc.returncode, err[:300])
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.warning("gws calendar insert returned non-JSON: %s", proc.stdout[:200])
        return None


def sync_events(events: list[Event], *, dry_run: bool = False) -> dict[str, int]:
    """Create calendar events for `events`, idempotently.

    Returns a small stats dict: {created, skipped_existing, skipped_no_start, errors}.

    - dry_run=True OR HK_EVENTS_CALENDAR_ENABLED=False → no real writes, no map
      mutation; logs what it would do. (dry_run still calls gws with --dry-run so
      the request body is validated against the live helper.)
    """
    stats = {"created": 0, "skipped_existing": 0, "skipped_no_start": 0, "errors": 0}
    synced = _load_synced()
    writes_enabled = HK_EVENTS_CALENDAR_ENABLED and not dry_run

    if not writes_enabled:
        log.info(
            "calendar writes DISABLED (enabled=%s, dry_run=%s) — validating only",
            HK_EVENTS_CALENDAR_ENABLED, dry_run,
        )

    for event in events:
        if event.start is None:
            log.info("calendar: skipping %r — no start time", event.title[:40])
            stats["skipped_no_start"] += 1
            continue

        key = event.stable_hash
        if key in synced:
            log.info("calendar: already synced %r (hash %s) — skipping", event.title[:40], key)
            stats["skipped_existing"] += 1
            continue

        cmd = _insert_cmd(event, dry_run=not writes_enabled)
        resp = _run_gws_insert(cmd)

        if resp is None:
            stats["errors"] += 1
            continue

        if not writes_enabled:
            # --dry-run path: gws returns {"dry_run": true, ...}. Validate shape,
            # do NOT record to the idempotency map (nothing was created).
            log.info("calendar: [dry-run] would create %r at %s", event.title[:40], event.start.isoformat())
            stats["created"] += 1  # counted as "would-create" for reporting
            continue

        gcal_id = resp.get("id") or resp.get("body", {}).get("id", "")
        synced[key] = {
            "gcal_id": gcal_id,
            "summary": event.title,
            "start": event.start.isoformat(),
            "source": event.source,
            "ts": datetime.now().isoformat(),
        }
        stats["created"] += 1
        log.info("calendar: created %r (gcal id %s, hash %s)", event.title[:40], gcal_id, key)

    if writes_enabled:
        _save_synced(synced)

    return stats
