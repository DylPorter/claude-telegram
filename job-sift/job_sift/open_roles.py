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

from job_sift import liveness
from job_sift.config import STATE_DIR
from job_sift.schema import JobListing, normalise

log = logging.getLogger(__name__)

# Statuses that encode a decision the operator made by hand. Nothing automatic may
# overwrite them — not an upsert, not the ager, not the pruner.
STICKY_STATUSES = frozenset({"applied", "dismissed"})

VALID_STATUSES = frozenset({"open", "expired", "stale", "applied", "dismissed"})

# Which classifier lane admitted the role. Stored so the register can render the
# two lanes under separate headings without re-running the classifier, and so a
# role keeps its heading on the days its source does not re-list it.
VALID_LANES = frozenset({"prestige", "floor"})


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
    # Defaults to "prestige" so a register written before the floor lane
    # existed loads with every role under the heading it was surfaced under.
    lane: str = "prestige"
    # Part of `identity_key`. A register written before this field existed loads
    # with None, which simply means those rows key on employer+title alone and
    # will not collapse against a freshly-written row carrying a location — a
    # missed collapse, which is the direction we want to fail in.
    location: str | None = None
    # ISO date of the last liveness re-check, or None for "never asked". Only
    # ever stamped when a check actually came back with an answer.
    last_checked: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> OpenRole:
        """Tolerant of missing/extra keys so a hand-edited state file still loads."""
        status = d.get("status", "open")
        lane = d.get("lane", "prestige")
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
            lane=lane if lane in VALID_LANES else "prestige",
            location=d.get("location"),
            last_checked=d.get("last_checked"),
        )

    @property
    def identity_key(self) -> str:
        """Same identity as `JobListing.identity_key`, computed off the register.

        Kept deliberately in lockstep with the listing property (same fields,
        same `normalise`) so a stored row and the listing that produced it agree
        — otherwise `collapse_register` would split a posting the fetch-time
        collapse had already merged. Read that docstring for why the key is
        source-scoped and why it is exact rather than fuzzy.
        """
        employer = normalise(self.employer)
        title = normalise(self.title)
        if not employer or not title:
            return self.dedup_key
        return f"{self.source}|{employer}|{title}|{normalise(self.location)}"

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
    newly_surfaced: list[tuple[JobListing, str]] | list[tuple[JobListing, str, str]],
    today: date,
) -> list[OpenRole]:
    """Merge this run's surfaced listings into the register.

    Known key → bump `last_seen`, refresh the mutable listing fields, keep
    `first_seen`. A sticky status (`applied`/`dismissed`) is never reset to
    `open`; a re-sighting only re-opens something the ager had closed.

    New key → append as `open` with `first_seen == last_seen == today`.
    Returns a NEW list; `existing` is not mutated.

    Each entry is `(listing, reason)` or `(listing, reason, lane)`. The lane is
    optional because it is not the register's business to know how many lanes
    the classifier has — a caller that does not supply one gets "prestige",
    which is what every caller meant before there was a second lane.

    THE LANE IS NOT PART OF THE KEY. `dedup_key` is, and it does not mention
    the lane, so a role that moves between lanes — the classifier gets better,
    an employer gets added to the boost list — is still the SAME record. Its
    `first_seen` survives, and so does an `applied`/`dismissed` mark the
    operator made under the old heading. A lane change re-files a role; it must
    never resurrect a decision he already took.
    """
    iso = today.isoformat()
    merged = [
        OpenRole(**r.to_dict()) if isinstance(r, OpenRole) else r for r in existing
    ]
    by_key = {r.dedup_key: r for r in merged}

    for entry in newly_surfaced:
        listing, reason, *rest = entry
        lane = rest[0] if rest and rest[0] in VALID_LANES else "prestige"
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
                lane=lane,
                location=listing.location,
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
        current.lane = lane
        current.location = listing.location or current.location
        if current.status not in STICKY_STATUSES:
            # Seen again today → it is live, whatever the ager decided earlier.
            current.status = "open"

    return merged


# Sources whose rows can be liveness-checked. LinkedIn is the only one today,
# and the only one that needs it: every other source re-lists a role for as long
# as it is open, so `last_seen` is already a live signal there, and CEDARS
# carries a real deadline on top.
LIVENESS_SOURCES = frozenset({"linkedin"})


def _sticky_rank(role: OpenRole) -> int:
    """`applied` outranks `dismissed` outranks everything else, for a merge.

    An application is a fact about what the operator did; a dismissal is a
    preference. If one posting somehow carries both marks under two ids, the
    fact survives.
    """
    if role.status == "applied":
        return 0
    if role.status == "dismissed":
        return 1
    return 2


def _merge_key(role: OpenRole) -> tuple:
    """Order within a duplicate group; the first element is the survivor.

    Sticky first (see `_sticky_rank`), then the most recently seen row — that is
    the id the source is still listing, so it is the one whose `apply_url` still
    points somewhere useful. `sorted` is stable, so an exact tie keeps register
    order.
    """
    return (_sticky_rank(role), _invert_iso(role.last_seen))


def _invert_iso(value: str) -> str:
    """Sort ISO dates DESCENDING inside an otherwise-ascending tuple."""
    # Complementing each digit turns "2026-08-20" into a string that sorts
    # before "2026-08-01" — cheaper and clearer than splitting the sort.
    return "".join(chr(0x7E - ord(c)) if c.isdigit() else c for c in value or "")


def collapse_register(roles: list[OpenRole]) -> list[OpenRole]:
    """Fold register rows that are the same posting into one. Returns a NEW list.

    `dedupe.collapse_duplicates` handles duplicates that arrive in the SAME run.
    This handles the ones that do not, which is the reported case: the two IMC
    rows in issue #1b came from alert emails days apart, so no single fetch ever
    held both and nothing upstream could have seen them together. The register
    is the only place they ever coexist.

    Merging rules, in the order they matter:

    1. A HAND-SET STATUS SURVIVES. If any row in the group is `applied` or
       `dismissed`, that row wins outright, so collapsing can never resurrect a
       decision the operator already took. This is the one rule that is not
       about tidiness.
    2. Otherwise the most recently seen row wins — it is the id the source is
       still listing.
    3. History is unioned, not taken from the winner: earliest `first_seen`,
       latest `last_seen`. Collapsing must not make a role look newer than it is.
    4. A deadline is never lost. If the winner has none and a dropped row does,
       the later of the known deadlines is carried over — later, so a merge can
       never make the ager expire a role sooner than the evidence supports.

    The dropped row's `<!-- status:... -->` marker in the note becomes an
    orphan; `apply_status_overrides` simply stops matching it, and rule 1 has
    already moved anything it was carrying onto the survivor.
    """
    groups: dict[str, list[OpenRole]] = {}
    order: list[str] = []
    for role in roles:
        key = role.identity_key
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(role)

    out: list[OpenRole] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            out.append(OpenRole(**group[0].to_dict()))
            continue
        ranked = sorted(group, key=_merge_key)
        winner = OpenRole(**ranked[0].to_dict())
        first_seens = [r.first_seen for r in group if r.first_seen]
        last_seens = [r.last_seen for r in group if r.last_seen]
        if first_seens:
            winner.first_seen = min(first_seens)
        if last_seens:
            winner.last_seen = max(last_seens)
        if winner.deadline is None:
            deadlines = [r.deadline for r in group if r.deadline]
            if deadlines:
                winner.deadline = max(deadlines)
        for other in ranked[1:]:
            log.info(
                "collapsed duplicate register row: keeping %s, dropping %s (%s — %s)",
                winner.dedup_key, other.dedup_key, winner.employer[:40], winner.title[:60],
            )
        out.append(winner)
    return out


def roles_due_liveness_check(
    roles: list[OpenRole],
    today: date,
    *,
    interval_days: int = 7,
    limit: int = 10,
) -> list[OpenRole]:
    """Pick the rows worth re-checking this run, cheapest-value-first.

    Deliberately narrow. Only `open` rows from `LIVENESS_SOURCES` that have NO
    deadline are eligible — a row with a real deadline already has an ageing
    mechanism, and a row the operator marked or the ager closed is none of this
    function's business.

    `limit` is a hard cap and `interval_days` a per-row cooldown, so the whole
    pass costs a bounded, small number of requests per run rather than one per
    open role. Never-checked rows sort first, then least-recently-checked.
    """
    due: list[OpenRole] = []
    for role in roles:
        if role.status != "open":
            continue
        if role.source not in LIVENESS_SOURCES:
            continue
        if role.deadline_date is not None:
            continue
        last = _parse_date(role.last_checked or "")
        if last is not None and (today - last).days < interval_days:
            continue
        due.append(role)
    due.sort(key=lambda r: (r.last_checked or "", r.first_seen))
    return due[:limit]


def apply_liveness(
    roles: list[OpenRole], verdicts: dict[str, str], today: date
) -> list[OpenRole]:
    """Fold liveness verdicts (keyed by `dedup_key`) into the register.

    Returns a NEW list. Three things this must never do, all of them the same
    hazard wearing different clothes:

    * `UNKNOWN` changes NOTHING — not the status, and not `last_checked`
      either. Stamping the date on a failed check would buy a week of silence
      with no evidence behind it, so a source that is blocking us would look
      exactly like a source that keeps saying "still open".
    * A confirmed-closed row becomes `expired`, an existing terminal status the
      renderer and the pruner already understand. No new status word, so nothing
      downstream has to learn one.
    * `applied` / `dismissed` are untouchable, as everywhere else. A posting
      closing does not un-apply the operator's application.
    """
    out = [OpenRole(**r.to_dict()) for r in roles]
    iso = today.isoformat()
    for role in out:
        verdict = verdicts.get(role.dedup_key)
        if verdict is None or verdict == liveness.UNKNOWN:
            continue
        if role.status in STICKY_STATUSES:
            continue
        role.last_checked = iso
        if verdict == liveness.CLOSED:
            log.info("liveness: retiring %s — the posting is closed", role.dedup_key)
            role.status = "expired"
    return out


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


def in_lane(roles: list[OpenRole], lane: str) -> list[OpenRole]:
    """Roles belonging to one lane, order preserved.

    A filter rather than a grouping so callers keep whatever sort they already
    applied — the register is deadline-sorted, and re-grouping would lose that.
    """
    return [r for r in roles if r.lane == lane]


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
