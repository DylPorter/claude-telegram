"""Write the daily hk-events archive to the vault (audit trail)."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from hk_events.config import HK_EVENTS_ARCHIVE_DIR, VAULT_ROOT

log = logging.getLogger(__name__)


def write_archive(today: date, content: str) -> Path | None:
    """Write the rendered archive note to the vault. Returns path or None if vault not configured."""
    if VAULT_ROOT is None or HK_EVENTS_ARCHIVE_DIR is None:
        log.info("vault not configured — skipping archive")
        return None

    HK_EVENTS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = HK_EVENTS_ARCHIVE_DIR / f"{today.isoformat()}.md"
    path.write_text(content)
    log.info("archive written to %s", path)
    return path
