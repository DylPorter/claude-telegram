"""Rolling register of surfaced roles that survives across runs.

The daily digest is event-shaped ("what is new since last run"), but job hunting
is state-shaped ("what is open and still applicable right now"). A role surfaced
on day N never reappeared on day N+1, so a missed morning digest silently lost an
opportunity whose deadline was still weeks away (a PwC internship and a CLSA role
both aged out unread in July 2026).

This module keeps a deduplicated, deadline-sorted register in
`.data/state/open_roles.json`, rendered to `Areas/Work/Open Roles.md`. All the
decision logic here is pure and unit-tested; the only I/O is load/save at the
bottom.

Status is the operator's, not ours: once he marks a role `applied` or `dismissed`, no
later run may resurrect it to `open`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date

from job_sift.config import STATE_DIR
from job_sift.schema import JobListing

log = logging.getLogger(__name__)

# Statuses that encode a decision the operator made by hand. Nothing automatic may
# overwrite them — not an upsert, not the ager, not the pruner.
STICKY_STATUSES = frozenset({"applied", "dismissed"})

VALID_STATUSES = frozenset({"open", "expired", "stale", "applied", "dismissed"})


@dataclass
class OpenRole:
    """One surfaced role, tracked across runs. `dedup_key` is the primary key."""

    dedup_key: str
    source: str
    employer: str
    title: str
    apply_url: str
    deadline: str | None  # ISO date
    first_seen: str  # ISO date — never overwritten once set
    last_seen: str  # ISO date — bumped every run the listing is still present
    reason: str
    status: str = "open"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> OpenRole:
        """Tolerant of missing/extra keys so a hand-edited state file still loads."""
        status = d.get("status", "open")
        return cls(
            dedup_key=d["dedup_key"],
            source=d.get("source", ""),
            employer=d.get("employer", ""),
            title=d.get("title", ""),
            apply_url=d.get("apply_url", ""),
            deadline=d.get("deadline"),
            first_seen=d.get("first_seen", ""),
            last_seen=d.get("last_seen", ""),
            reason=d.get("reason", ""),
            status=status if status in VALID_STATUSES else "open",
        )

    @property
    def deadline_date(self) -> date | None:
        if not self.deadline:
            return None
        try:
            return date.fromisoformat(self.deadline)
        except ValueError:
            return None

    def days_left(self, today: date) -> int | None:
        d = self.deadline_date
        return None if d is None else (d - today).days


def _state_path():
    return STATE_DIR / "open_roles.json"


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------


def upsert_roles(
    existing: list[OpenRole],
    newly_surfaced: list[tuple[JobListing, str]],
    today: date,
) -> list[OpenRole]:
    """Merge this run's surfaced listings into the register.

    Known key → bump `last_seen`, refresh the mutable listing fields, keep
    `first_seen`. A sticky status (`applied`/`dismissed`) is never reset to
    `open`; a re-sighting only re-opens something the ager had closed.

    New key → append as `open` with `first_seen == last_seen == today`.
    Returns a NEW list; `existing` is not mutated.
    """
    iso = today.isoformat()
    merged = [
        OpenRole(**r.to_dict()) if isinstance(r, OpenRole) else r for r in existing
    ]
    by_key = {r.dedup_key: r for r in merged}

    for listing, reason in newly_surfaced:
        key = listing.dedup_key
        deadline = listing.deadline.isoformat() if listing.deadline else None
        current = by_key.get(key)
        if current is None:
            role = OpenRole(
                dedup_key=key,
                source=str(listing.source),
                employer=listing.employer,
                title=listing.title,
                apply_url=listing.apply_url,
                deadline=deadline,
                first_seen=iso,
                last_seen=iso,
                reason=reason,
                status="open",
            )
            merged.append(role)
            by_key[key] = role
            continue

        current.last_seen = iso
        current.employer = listing.employer or current.employer
        current.title = listing.title or current.title
        current.apply_url = listing.apply_url or current.apply_url
        current.deadline = deadline if deadline is not None else current.deadline
        current.reason = reason or current.reason
        if current.status not in STICKY_STATUSES:
            # Seen again today → it is live, whatever the ager decided earlier.
            current.status = "open"

    return merged


def age_roles(
    roles: list[OpenRole], today: date, stale_after_days: int = 30
) -> list[OpenRole]:
    """Close out roles that time has overtaken.

    - deadline in the past → `expired`
    - no deadline at all and not seen for > `stale_after_days` → `stale`
      (nothing left to judge it by, and the source stopped listing it)

    `applied`/`dismissed` are the operator's marks and are left alone.
    """
    aged = [OpenRole(**r.to_dict()) for r in roles]
    for role in aged:
        if role.status in STICKY_STATUSES:
            continue
        deadline = role.deadline_date
        if deadline is not None:
            if deadline < today:
                role.status = "expired"
            continue
        last_seen = _parse_date(role.last_seen)
        if last_seen is not None and (today - last_seen).days > stale_after_days:
            role.status = "stale"
    return aged


def _sort_key(role: OpenRole) -> tuple[int, date, str]:
    """Deadline ascending, None-deadline LAST, then employer.

    The leading flag is what puts undated roles at the bottom — a role with no
    deadline is never more urgent than one with a real date. `date.min` is only
    a filler so the tuple stays comparable; the flag already separated them.
    """
    deadline = role.deadline_date
    if deadline is None:
        return (1, date.min, role.employer.lower())
    return (0, deadline, role.employer.lower())


def active_roles(roles: list[OpenRole]) -> list[OpenRole]:
    """Only `open` roles, deadline-ascending with undated ones last."""
    return sorted((r for r in roles if r.status == "open"), key=_sort_key)


def closing_within(roles: list[OpenRole], today: date, days: int = 7) -> list[OpenRole]:
    """Open roles whose deadline lands within `days` (inclusive) of today.

    Already-past deadlines are excluded — those are `expired`'s job, and a
    negative days-left in a "closing soon" list is noise.
    """
    out = []
    for role in active_roles(roles):
        left = role.days_left(today)
        if left is not None and 0 <= left <= days:
            out.append(role)
    return out


def prune(roles: list[OpenRole], today: date, keep_days: int = 60) -> list[OpenRole]:
    """Drop closed-out records older than `keep_days` past `last_seen`.

    `applied` records are kept forever — they are the operator's application history,
    not clutter. Anything still `open` is kept by definition.
    """
    kept = []
    for role in roles:
        if role.status in ("open", "applied"):
            kept.append(role)
            continue
        last_seen = _parse_date(role.last_seen)
        if last_seen is None or (today - last_seen).days <= keep_days:
            kept.append(role)
    return kept


def parse_status_overrides(md: str) -> dict[str, str]:
    """Extract `<!-- status:applied cedars:12345 -->` markers from the note.

    We emit one marker per entry so the operator can flip a role to applied/dismissed
    by editing the markdown directly rather than the JSON state file. Only the
    sticky statuses are honoured — letting the markdown set `open`/`expired`
    would just fight the ager.
    """
    overrides: dict[str, str] = {}
    for line in md.splitlines():
        stripped = line.strip()
        start = stripped.find("<!-- status:")
        if start == -1:
            continue
        end = stripped.find("-->", start)
        if end == -1:
            continue
        body = stripped[start + len("<!-- status:") : end].strip()
        parts = body.split(None, 1)
        if len(parts) != 2:
            continue
        status, key = parts[0].strip(), parts[1].strip()
        if status in STICKY_STATUSES and key:
            overrides[key] = status
    return overrides


def apply_status_overrides(
    roles: list[OpenRole], overrides: dict[str, str]
) -> list[OpenRole]:
    """Stamp markdown-sourced statuses onto the loaded register."""
    out = [OpenRole(**r.to_dict()) for r in roles]
    for role in out:
        override = overrides.get(role.dedup_key)
        if override in STICKY_STATUSES:
            role.status = override
    return out


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------


def load_open_roles() -> list[OpenRole]:
    """Load the register, tolerating a missing or corrupt file (mirrors dedupe.load_seen)."""
    p = _state_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
        return [OpenRole.from_dict(d) for d in raw]
    except Exception as exc:
        log.warning("failed to load open-roles register: %s — starting fresh", exc)
        return []


def save_open_roles(roles: list[OpenRole]) -> None:
    _state_path().write_text(
        json.dumps([r.to_dict() for r in roles], indent=2, sort_keys=True)
    )
