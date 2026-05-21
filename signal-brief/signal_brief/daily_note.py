"""Read/write the daily note for the vault audit trail."""

from __future__ import annotations

import logging
from datetime import date as _date
from pathlib import Path

from signal_brief.config import DAILY_NOTES_DIR

log = logging.getLogger(__name__)

SIGNAL_SECTION_MARKER = "## 🌅 Morning Signal Brief"
SIGNAL_SECTION_END_MARKER = "<!-- signal-brief:end -->"


def daily_note_path(date_str: str | None = None) -> Path:
    """Return the path to the daily note for the given date (defaults to today)."""
    if date_str is None:
        date_str = _date.today().isoformat()
    return DAILY_NOTES_DIR / f"{date_str}.md"


def upsert_signal_section(date_str: str, section_md: str) -> Path:
    """Insert or replace the Morning Signal Brief section in the daily note.

    The section is bounded by `## 🌅 Morning Signal Brief` and
    `<!-- signal-brief:end -->`. Re-running the morning brief replaces it
    in place rather than appending.

    If the daily note doesn't exist yet, creates it with the section inside.
    """
    path = daily_note_path(date_str)
    bounded = f"{section_md}\n\n{SIGNAL_SECTION_END_MARKER}\n"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {date_str}\n\n{bounded}")
        log.info("created daily note %s with signal section", path)
        return path

    content = path.read_text()
    start = content.find(SIGNAL_SECTION_MARKER)
    end_marker = content.find(SIGNAL_SECTION_END_MARKER, start) if start >= 0 else -1

    if start >= 0 and end_marker >= 0:
        # Replace existing block.
        end = end_marker + len(SIGNAL_SECTION_END_MARKER)
        new_content = content[:start] + bounded + content[end:].lstrip("\n")
        # Ensure single blank line separation.
        new_content = new_content.replace(bounded + "\n", bounded)
        path.write_text(new_content)
        log.info("replaced signal section in %s", path)
    else:
        # Append to end with blank line separation.
        sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
        path.write_text(content + sep + bounded)
        log.info("appended signal section to %s", path)

    return path
