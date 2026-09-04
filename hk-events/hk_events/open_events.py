"""Rolling register of captured events that survives across runs.

WHY THIS EXISTS AT ALL. hk-events kept only seen-SETS — ids and a cached
verdict, no titles, no dates, no urls — because the deliverable was a push and
a push is event-shaped ("what is new since last run"). A board is state-shaped
("what is on, right now"), so there has to be somewhere the rows actually live.
This is job-sift's `open_roles.py` with the field names changed, deliberately:
two registers that behave differently under ageing would be two things to
reason about.

CAPTURE IS BROAD HERE TOO. Every event fetched inside the horizon is written,
including ones the relevance classifier tagged `drop`. The tag is kept as a
FACET (`room`), not applied as a gate — the precision-biased filter is a taste
decision, and taste decisions belong in the reader's dropdown now rather than
in a delete nobody can review.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date

from hk_events import config
from hk_events.schema import Event

log = logging.getLogger(__name__)

VALID_STATUSES = frozenset({"open", "past"})

# Days after an event has started before it leaves the register. Small: an
# event that happened yesterday is still worth seeing on the board (you may be
# writing it up), one from last month is not.
PURGE_PAST_AFTER_DAYS = 3
# The two clocks job-sift's purge uses, same meanings.
PURGE_UNSEEN_AFTER_DAYS = 30
PURGE_MAX_AGE_DAYS = 60


@dataclass
class OpenEvent:
    """One captured event, tracked across runs. `dedup_key` is the primary key."""

    dedup_key: str
    source: str
    title: str
    url: str
    starts: str | None  # ISO date (HKT) of the start, or None if the feed gave none
    starts_at: str | None  # ISO datetime, for display; None when unknown
    location: str | None
    organizer: str | None
    first_seen: str
    last_seen: str
    # ADVISORY TAGS. None means untagged — the classifier has not judged this
    # event (or judged it in a run whose verdict was not cached). Never "drop".
    room: str | None = None
    reason: str | None = None
    status: str = "open"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> OpenEvent:
        """Tolerant of missing/extra keys so a hand-edited state file loads."""
        status = d.get("status", "open")
        return cls(
            dedup_key=d["dedup_key"],
            source=d.get("source", ""),
            title=d.get("title", ""),
            url=d.get("url", ""),
            starts=_opt_str(d.get("starts")),
            starts_at=_opt_str(d.get("starts_at")),
            location=_opt_str(d.get("location")),
            organizer=_opt_str(d.get("organizer")),
            first_seen=d.get("first_seen", ""),
            last_seen=d.get("last_seen", ""),
            room=_opt_str(d.get("room")),
            reason=_opt_str(d.get("reason")),
            status=status if status in VALID_STATUSES else "open",
        )

    @property
    def start_date(self) -> date | None:
        """The parsed start, or None for BOTH "no start" and "unreadable".

        Fine for sorting and display. Anything that DELETES must use
        `start_state` — see its docstring.
        """
        if not self.starts:
            return None
        try:
            return date.fromisoformat(self.starts)
        except ValueError:
            return None

    @property
    def start_state(self) -> tuple[str, date | None]:
        """`("none"|"unreadable"|"known", date_or_None)`.

        Mirrors `job_sift.open_roles.OpenRole.deadline_state`, and exists for
        the same reason: `start_date` collapses "there is no start" and "there
        is a start and I cannot parse it", so the future-start veto that stops
        the purge deleting a live event silently did not apply to any row whose
        date we failed to read. That is the one-value-two-meanings failure this
        codebase keeps deleting, rebuilt inside the fix for it.
        """
        if not self.starts:
            return ("none", None)
        try:
            return ("known", date.fromisoformat(self.starts))
        except ValueError:
            return ("unreadable", None)


def _opt_str(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _state_path():
    # Through the module, not a bound name — tests redirect STATE_DIR.
    return config.STATE_DIR / "open_events.json"


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------


def upsert_events(
    existing: list[OpenEvent],
    seen_now: list[tuple[Event, str | None, str | None]],
    today: date,
) -> list[OpenEvent]:
    """Merge this run's fetched events into the register. Returns a NEW list.

    Each entry is `(event, room, reason)`. A `room` of None LEAVES THE STORED
    TAG ALONE rather than clearing it: the reminder path reuses a cached
    verdict and does not always have one to hand, and "nobody classified it
    this run" must not erase the run that did.

    Known key → bump `last_seen`, refresh the mutable fields, keep `first_seen`.
    New key → append with `first_seen == last_seen == today`.
    """
    iso = today.isoformat()
    merged = [OpenEvent(**e.to_dict()) for e in existing]
    by_key = {e.dedup_key: e for e in merged}

    for event, room, reason in seen_now:
        key = event.dedup_key
        start_date = event.start_date
        current = by_key.get(key)
        if current is None:
            record = OpenEvent(
                dedup_key=key,
                source=str(event.source),
                title=event.title,
                url=event.url,
                starts=start_date.isoformat() if start_date else None,
                starts_at=event.start.isoformat() if event.start else None,
                location=event.location,
                organizer=event.organizer,
                first_seen=iso,
                last_seen=iso,
                room=_opt_str(room),
                reason=_opt_str(reason),
            )
            merged.append(record)
            by_key[key] = record
            continue

        current.last_seen = iso
        current.title = event.title or current.title
        current.url = event.url or current.url
        if start_date:
            current.starts = start_date.isoformat()
            current.starts_at = event.start.isoformat() if event.start else current.starts_at
        current.location = event.location or current.location
        current.organizer = event.organizer or current.organizer
        if _opt_str(room):
            current.room = _opt_str(room)
        if _opt_str(reason):
            current.reason = _opt_str(reason)

    return merged


def age_events(events: list[OpenEvent], today: date) -> list[OpenEvent]:
    """Mark events whose start has passed. Returns a NEW list.

    An event with NO start stays `open` forever as far as this is concerned —
    there is nothing to compare against, and guessing that an undated event has
    already happened would retire a live one. The unseen clock in `purge`
    is what eventually removes those.
    """
    aged = [OpenEvent(**e.to_dict()) for e in events]
    for record in aged:
        start = record.start_date
        if start is not None and start < today:
            record.status = "past"
    return aged


def purge(
    events: list[OpenEvent],
    today: date,
    *,
    past_after_days: int = PURGE_PAST_AFTER_DAYS,
    unseen_after_days: int = PURGE_UNSEEN_AFTER_DAYS,
    max_age_days: int = PURGE_MAX_AGE_DAYS,
) -> tuple[list[OpenEvent], list[tuple[OpenEvent, str]]]:
    """Drop rows that have aged out. Returns `(kept, dropped)` with reasons.

    Same shape as `job_sift.open_roles.purge`, and the same rule that a drop is
    reported rather than performed silently — a register that shrank and said
    nothing is indistinguishable from a capture that failed.

    ⚠️ A START STILL IN THE FUTURE VETOES BOTH CLOCKS, exactly as a future
    deadline does for a job. An event announced two months out and no longer
    carried by whichever feed first mentioned it is still happening; `last_seen`
    is a fact about our crawl, and the start date is a fact about the world.
    A `luma_discover` row is the concrete case: the city page shows about a
    dozen events at a time, so anything further out silently stops being
    re-sighted long before it occurs.

    TWO DIVERGENCES FROM THE JOB REGISTER, both because an event is not a
    posting:

    * There is a THIRD rule here — an event that already happened, by more than
      a few days, leaves. A job posting has to be inferred dead; an event has a
      date on which it stops existing.
    * There is NO sticky-status exemption, because hk-events has no operator
      marks: nothing in this project writes `applied`/`dismissed`, so there is
      nothing here that a purge could destroy. If a hand-set mark is ever added,
      it must be exempted here FIRST — that is the one thing in the job
      register's purge that is not a heuristic.

    A row whose dates are missing or unparseable is KEPT — `starts`,
    `last_seen` and `first_seen` alike (see `start_state`). Not being able to
    tell is not evidence, and the safe direction for a delete is not to delete.
    A sighting today likewise vetoes the max-age clock.

    ⚠️ THIS DELETE IS IRREVERSIBLE. The seen-set has no TTL, so a purged row is
    not re-captured the next time a feed carries it. Every drop is reported for
    that reason.
    """
    kept: list[OpenEvent] = []
    dropped: list[tuple[OpenEvent, str]] = []
    for record in events:
        state, start = record.start_state
        if state == "unreadable":
            log.warning(
                "purge: %s has an unreadable start %r — KEEPING it. A date we "
                "cannot parse is not evidence the event is over.",
                record.dedup_key, record.starts,
            )
            kept.append(record)
            continue
        if start is not None:
            if start >= today:
                kept.append(record)
                continue
            days_past = (today - start).days
            if days_past > past_after_days:
                dropped.append((record, f"started {days_past} days ago (> {past_after_days})"))
                continue
            kept.append(record)
            continue
        last_seen = _parse_date(record.last_seen)
        first_seen = _parse_date(record.first_seen)
        if last_seen is not None and (today - last_seen).days > unseen_after_days:
            dropped.append(
                (record, f"undated, and not listed by {record.source} for "
                         f"{(today - last_seen).days} days (> {unseen_after_days})")
            )
            continue
        if (
            first_seen is not None
            and (today - first_seen).days > max_age_days
            # A sighting in THIS RUN vetoes the age clock: the source listing
            # it today is the strongest evidence available that it is real.
            # Same rule as job_sift.open_roles.purge.
            and last_seen != today
        ):
            dropped.append(
                (record, f"undated, first seen {(today - first_seen).days} days ago "
                         f"(> {max_age_days})")
            )
            continue
        kept.append(record)
    return kept, dropped


def upcoming(events: list[OpenEvent]) -> list[OpenEvent]:
    """Only `open` events, soonest first, undated ones last."""
    return sorted(
        (e for e in events if e.status == "open"),
        key=lambda e: (1, "") if e.starts is None else (0, e.starts),
    )


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------


class RegisterUnreadableError(RuntimeError):
    """`open_events.json` exists but could not be read.

    A THIRD OUTCOME. "No file yet" is an empty register; "a file I cannot
    parse" is the register still being there and this process being unable to
    see it. Returning `[]` for both would let the next `save_events` truncate
    it to nothing — see the same class in job_sift/open_roles.py, where that
    wipe was reproduced against a register holding a hand-set `applied`.
    """


def load_events() -> list[OpenEvent]:
    """Load the register. Missing file → empty; UNREADABLE file → raise."""
    path = _state_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
        return [OpenEvent.from_dict(d) for d in raw]
    except Exception as exc:
        log.error(
            "event register at %s could not be read (%s) — REFUSING to continue, "
            "because the next write would replace it with an empty one",
            path, exc,
        )
        raise RegisterUnreadableError(
            f"{path} could not be read ({exc}); the register was NOT overwritten"
        ) from exc


def save_events(events: list[OpenEvent]) -> None:
    """Write the register ATOMICALLY (tmp file + os.replace)."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([e.to_dict() for e in events], indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
