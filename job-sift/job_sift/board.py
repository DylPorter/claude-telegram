"""Build the two-tab HTML board out of the register (+ the hk-events feed).

`board_html.py` owns the rendering and knows nothing about jobs; this module
owns the mapping from this project's records onto its `Section` shape.

WHY THERE IS A FILE HANDOFF FOR THE EVENTS TAB. job-sift and hk-events are two
separate services on two separate timers with two separate state directories,
and the board is one file with two tabs. Rather than have either import the
other, each writes a small JSON feed of its own rows and reads the other's if
it is there. If it is NOT there, the tab says so — `Section(available=False)` —
instead of rendering an empty table. Those are different facts: "hk-events
found no upcoming events" and "job-sift could not read hk-events' feed" would
otherwise look identical on the page, which is the exact ambiguity this
codebase exists to delete.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from pathlib import Path

from job_sift.board_html import Column, Facet, Section, Sort, render_board
from job_sift.open_roles import OpenRole
from job_sift.classifier import negative_title
from job_sift.tags import clean_function, derive_role_type

log = logging.getLogger(__name__)

FEED_VERSION = 1


def _tri(value: bool | None) -> str | None:
    """A tri-state bool as a facet-friendly string. None stays None (untagged).

    NOT `str(value)`: `str(None)` is `"None"`, which would render as a real tag
    called "None" and give the reader a dropdown option that looks like an
    answer. Absent has to stay absent all the way to the cell.
    """
    if value is None:
        return None
    return "yes" if value else "no"


def job_row(role: OpenRole, today: date) -> dict:
    """One register row as board data. Every value is a string, a bool or None.

    `role_type` and `function` fall back to deriving from the TITLE when the
    stored tag is absent. Neither is a guess: both are the same pure keyword
    functions the capture path runs, on the same input, so they compute the
    identical answer capture would have stored. (Capture derives `role_type`
    from the title only for exactly this reason — an earlier cut also scanned
    the description, which both diverged from this fallback and mislabelled a
    permanent role whose body mentioned an internship programme.) The fallback
    exists because the register predates both fields; without it every row
    written before today reads "untagged" for a tag that costs nothing to
    compute. When a derivation finds nothing the value stays None, exactly as
    at capture.

    `industry` and `is_technical` get NO such fallback, because there is no
    pure function that produces them — they come from a model, and inventing
    them here would be exactly the fabrication this file's neighbours forbid.
    """
    days_left = role.days_left(today)
    return {
        "employer": role.employer or None,
        "title": role.title or None,
        "apply_url": role.apply_url or None,
        "role_type": role.role_type or derive_role_type(role.title),
        "industry": role.industry,
        "technical": _tri(role.is_technical),
        "function": role.function or clean_function(negative_title(role.title)),
        "lane": role.lane,
        "prestige": role.prestige,
        "source": role.source or None,
        "location": role.location,
        "status": role.status,
        "deadline": role.deadline,
        "days_left": None if days_left is None else str(days_left),
        "first_seen": role.first_seen or None,
        "last_seen": role.last_seen or None,
        "reason": role.reason or None,
    }


def jobs_section(roles: list[OpenRole], today: date) -> Section:
    """The Jobs tab.

    Every row in the register is here, including `expired`, `stale`, `applied`
    and `dismissed` ones, with `status` offered as a facet. Filtering them out
    by default would be another taste decision taken away from the reader —
    and a board that quietly holds back rows is a board whose counts cannot be
    checked against the register.
    """
    return Section(
        key="jobs",
        label="Jobs",
        rows=[job_row(r, today) for r in roles],
        columns=[
            Column("employer", "Employer"),
            Column("title", "Title", kind="link", href_key="apply_url"),
            Column("role_type", "Role type", kind="tags"),
            Column("industry", "Industry", kind="tags"),
            Column("function", "Function", kind="tags"),
            Column("technical", "Technical"),
            Column("source", "Source"),
            Column("location", "Location"),
            Column("deadline", "Deadline"),
            Column("status", "Status"),
            Column("first_seen", "First seen"),
        ],
        facets=[
            Facet("role_type", "Role type"),
            Facet("industry", "Industry"),
            Facet("function", "Function"),
            Facet("technical", "Technical"),
            Facet("source", "Source"),
            Facet("lane", "Lane"),
            Facet("prestige", "Prestige"),
            Facet("status", "Status"),
        ],
        sorts=[
            Sort("first_seen", "Recently added", kind="date", ascending=False),
            Sort("deadline", "Deadline", kind="date", ascending=True),
            Sort("last_seen", "Last seen", kind="date", ascending=False),
            Sort("employer", "Employer"),
        ],
        search_keys=[
            "employer", "title", "industry", "location", "reason", "role_type", "function",
        ],
        empty_text="The register is empty.",
    )


# --------------------------------------------------------------------------
# The events feed handoff
# --------------------------------------------------------------------------

_EVENT_COLUMNS = [
    Column("title", "Event", kind="link", href_key="url"),
    Column("starts", "Starts"),
    Column("location", "Location"),
    Column("room", "Room", kind="tags"),
    Column("organizer", "Organiser"),
    Column("source", "Source"),
    Column("first_seen", "First seen"),
]
_EVENT_FACETS = [
    Facet("room", "Room"),
    Facet("source", "Source"),
    Facet("location", "Location"),
]
_EVENT_SORTS = [
    Sort("starts", "Soonest first", kind="date", ascending=True),
    Sort("first_seen", "Recently added", kind="date", ascending=False),
    Sort("title", "Title"),
]
_EVENT_SEARCH = ["title", "location", "organizer", "room", "reason"]


def read_events_feed(path: Path | None) -> tuple[list[dict] | None, str | None]:
    """Read hk-events' feed. Returns `(rows, note)`; `rows is None` means UNREAD.

    The three outcomes are kept apart deliberately:
      * `([], note)`     — the feed was read and hk-events had no events.
      * `(rows, None)`   — the feed was read and had rows.
      * `(None, note)`   — the feed could not be read, for any reason.
    Only the first two may render a table. The third renders the note.
    """
    if path is None:
        return None, "No events feed is configured, so this tab has no data."
    if not path.exists():
        return None, (
            f"No events feed at {path}. hk-events writes it on its own run — "
            "this tab will fill in after hk-events next runs."
        )
    try:
        payload = json.loads(path.read_text())
        rows = payload["events"]
        if not isinstance(rows, list):
            raise TypeError("'events' is not a list")
    except Exception as exc:  # noqa: BLE001 — a bad feed must not kill the board
        log.warning("events feed at %s could not be read: %s", path, exc)
        return None, f"The events feed at {path} could not be read ({exc})."
    generated = payload.get("generated")
    note = f"Events supplied by hk-events, generated {generated}." if generated else None
    return [r for r in rows if isinstance(r, dict)], note


def events_section(rows: list[dict] | None, note: str | None) -> Section:
    return Section(
        key="events",
        label="Events",
        rows=rows or [],
        columns=_EVENT_COLUMNS,
        facets=_EVENT_FACETS,
        sorts=_EVENT_SORTS,
        search_keys=_EVENT_SEARCH,
        empty_text="hk-events is tracking no upcoming events.",
        note=note,
        available=rows is not None,
    )


def build_board(
    roles: list[OpenRole],
    today: date,
    *,
    events_feed_path: Path | None = None,
    title: str = "Roles & Events Board",
) -> str:
    rows, note = read_events_feed(events_feed_path)
    return render_board(
        [jobs_section(roles, today), events_section(rows, note)],
        generated_on=today,
        title=title,
        footer=(
            "Capture is broad on purpose: everything in scope is here, and the "
            "dropdowns above are the filter. Nothing is dropped for being "
            "untagged — untagged rows appear under the “—” option of every facet."
        ),
    )


def write_board(path: Path, html: str) -> Path:
    """Write the board ATOMICALLY, the same way `write_feed` writes the feed.

    The feed was atomic and this was not, which is backwards: a half-written
    FEED is refused by its reader and reported, while a half-written PAGE still
    opens, showing whatever rows made it before the cut with nothing to say the
    rest is missing. A silent partial result is the one output shape this
    codebase refuses to produce.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(html)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log.info("board written to %s", path)
    return path


def write_feed(path: Path, roles: list[OpenRole], today: date) -> Path:
    """Write the jobs feed hk-events reads for its own Jobs tab.

    Atomic, because the reader is a different process on a different timer and
    a half-written feed would read as a corrupt one — which its reader reports
    as "could not be read", correctly but pointlessly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": FEED_VERSION,
            "generated": today.isoformat(),
            "jobs": [job_row(r, today) for r in roles],
        },
        indent=2,
        sort_keys=True,
    )
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
    log.info("jobs feed written to %s", path)
    return path
