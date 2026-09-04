"""Build the two-tab HTML board out of the event register (+ job-sift's feed).

Mirror of `job_sift/board.py`. `board_html.py` owns the rendering and knows
nothing about events; this module owns the mapping from this project's records
onto its `Section` shape, and the file handoff that lets two independent
services share one page.

Each side writes a small JSON feed of its own rows and reads the other's if it
is there. If it is not, the tab says so — `Section(available=False)` — rather
than rendering an empty table, because "job-sift is tracking no roles" and
"hk-events could not read job-sift's feed" are different facts and must not
look identical.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from pathlib import Path

from hk_events.board_html import Column, Facet, Section, Sort, render_board
from hk_events.open_events import OpenEvent

log = logging.getLogger(__name__)

FEED_VERSION = 1


def event_row(record: OpenEvent) -> dict:
    """One register row as board data. Every value is a string or None."""
    return {
        "title": record.title or None,
        "url": record.url or None,
        "starts": record.starts,
        "starts_at": record.starts_at,
        "location": record.location,
        "organizer": record.organizer,
        "room": record.room,
        "source": record.source or None,
        "status": record.status,
        "reason": record.reason,
        "first_seen": record.first_seen or None,
        "last_seen": record.last_seen or None,
    }


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
    Facet("status", "Status"),
]
_EVENT_SORTS = [
    Sort("starts", "Soonest first", kind="date", ascending=True),
    Sort("first_seen", "Recently added", kind="date", ascending=False),
    Sort("title", "Title"),
]
_EVENT_SEARCH = ["title", "location", "organizer", "room", "reason"]


def events_section(records: list[OpenEvent]) -> Section:
    """The Events tab.

    Every captured event is here, including the ones the relevance classifier
    tagged `drop` — that tag is a facet now, not a delete. Precision-biasing a
    filter is a reasonable thing to do to a push notification and an
    unreasonable thing to do to an archive.
    """
    return Section(
        key="events",
        label="Events",
        rows=[event_row(r) for r in records],
        columns=_EVENT_COLUMNS,
        facets=_EVENT_FACETS,
        sorts=_EVENT_SORTS,
        search_keys=_EVENT_SEARCH,
        empty_text="The event register is empty.",
    )


# --------------------------------------------------------------------------
# The jobs feed handoff
# --------------------------------------------------------------------------

_JOB_COLUMNS = [
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
]
_JOB_FACETS = [
    Facet("role_type", "Role type"),
    Facet("industry", "Industry"),
    Facet("function", "Function"),
    Facet("technical", "Technical"),
    Facet("source", "Source"),
    Facet("status", "Status"),
]
_JOB_SORTS = [
    Sort("first_seen", "Recently added", kind="date", ascending=False),
    Sort("deadline", "Deadline", kind="date", ascending=True),
    Sort("employer", "Employer"),
]
_JOB_SEARCH = [
    "employer", "title", "industry", "location", "reason", "role_type", "function",
]


def read_jobs_feed(path: Path | None) -> tuple[list[dict] | None, str | None]:
    """Read job-sift's feed. Returns `(rows, note)`; `rows is None` means UNREAD."""
    if path is None:
        return None, "No jobs feed is configured, so this tab has no data."
    if not path.exists():
        return None, (
            f"No jobs feed at {path}. job-sift writes it on its own run — this "
            "tab will fill in after job-sift next runs."
        )
    try:
        payload = json.loads(path.read_text())
        rows = payload["jobs"]
        if not isinstance(rows, list):
            raise TypeError("'jobs' is not a list")
    except Exception as exc:  # noqa: BLE001 — a bad feed must not kill the board
        log.warning("jobs feed at %s could not be read: %s", path, exc)
        return None, f"The jobs feed at {path} could not be read ({exc})."
    generated = payload.get("generated")
    note = f"Roles supplied by job-sift, generated {generated}." if generated else None
    return [r for r in rows if isinstance(r, dict)], note


def jobs_section(rows: list[dict] | None, note: str | None) -> Section:
    return Section(
        key="jobs",
        label="Jobs",
        rows=rows or [],
        columns=_JOB_COLUMNS,
        facets=_JOB_FACETS,
        sorts=_JOB_SORTS,
        search_keys=_JOB_SEARCH,
        empty_text="job-sift is tracking no roles.",
        note=note,
        available=rows is not None,
    )


def build_board(
    records: list[OpenEvent],
    today: date,
    *,
    jobs_feed_path: Path | None = None,
    title: str = "Roles & Events Board",
) -> str:
    rows, note = read_jobs_feed(jobs_feed_path)
    return render_board(
        [jobs_section(rows, note), events_section(records)],
        generated_on=today,
        title=title,
        footer=(
            "Capture is broad on purpose: everything inside the horizon is here, "
            "and the dropdowns above are the filter. Nothing is dropped for "
            "being untagged — untagged rows appear under the “—” option."
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


def write_feed(path: Path, records: list[OpenEvent], today: date) -> Path:
    """Write the events feed job-sift reads for its own Events tab.

    Atomic, because the reader is a different process on a different timer and
    a half-written feed would read as a corrupt one — which its reader reports
    as "could not be read", correctly but pointlessly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": FEED_VERSION,
            "generated": today.isoformat(),
            "events": [event_row(r) for r in records],
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
    log.info("events feed written to %s", path)
    return path
