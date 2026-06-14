"""Read/write the daily note for the vault audit trail."""

from __future__ import annotations

import logging
from datetime import date as _date
from pathlib import Path

from signal_brief.config import DAILY_NOTES_DIR

log = logging.getLogger(__name__)

SIGNAL_SECTION_MARKER = "## 🌅 Morning Signal Brief"
SIGNAL_SECTION_END_MARKER = "<!-- signal-brief:end -->"

THREADS_SECTION_MARKER = "## 🧵 Thread Reconciliation"
THREADS_SECTION_END_MARKER = "<!-- threads:end -->"


def daily_note_path(date_str: str | None = None) -> Path:
    """Return the path to the daily note for the given date (defaults to today)."""
    if date_str is None:
        date_str = _date.today().isoformat()
    return DAILY_NOTES_DIR / f"{date_str}.md"


def _upsert_section(
    date_str: str, section_md: str, *, start_marker: str, end_marker: str
) -> Path:
    """Insert or replace a bounded section in the daily note, idempotently.

    The section is bounded by `start_marker` and `end_marker`. Re-running
    replaces it in place rather than appending. Creates the note if absent.
    """
    path = daily_note_path(date_str)
    bounded = f"{section_md}\n\n{end_marker}\n"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {date_str}\n\n{bounded}")
        log.info("created daily note %s with section %s", path, start_marker)
        return path

    content = path.read_text()
    start = content.find(start_marker)
    end_idx = content.find(end_marker, start) if start >= 0 else -1

    if start >= 0 and end_idx >= 0:
        end = end_idx + len(end_marker)
        new_content = content[:start] + bounded + content[end:].lstrip("\n")
        new_content = new_content.replace(bounded + "\n", bounded)
        path.write_text(new_content)
        log.info("replaced section %s in %s", start_marker, path)
    else:
        sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
        path.write_text(content + sep + bounded)
        log.info("appended section %s to %s", start_marker, path)

    return path


def upsert_signal_section(date_str: str, section_md: str) -> Path:
    """Insert or replace the Morning Signal Brief section in the daily note."""
    return _upsert_section(
        date_str, section_md,
        start_marker=SIGNAL_SECTION_MARKER,
        end_marker=SIGNAL_SECTION_END_MARKER,
    )


def upsert_threads_section(date_str: str, section_md: str) -> Path:
    """Insert or replace the Thread Reconciliation section in the daily note."""
    return _upsert_section(
        date_str, section_md,
        start_marker=THREADS_SECTION_MARKER,
        end_marker=THREADS_SECTION_END_MARKER,
    )
