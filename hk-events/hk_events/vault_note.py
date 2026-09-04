"""Write the daily hk-events archive to the vault (audit trail)."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from hk_events import config

log = logging.getLogger(__name__)


# Resolved through the `config` MODULE on every call, never bound at import
# time — same reasoning as dedupe._seen_path. A test that points
# `config.HK_EVENTS_ARCHIVE_DIR` at a tmp_path must actually redirect the
# write, or it silently lands in the live vault instead.
def write_archive(today: date, content: str) -> Path | None:
    """Write the rendered archive note to the vault. Returns path or None if vault not configured."""
    archive_dir = config.HK_EVENTS_ARCHIVE_DIR
    if config.VAULT_ROOT is None or archive_dir is None:
        log.info("vault not configured — skipping archive")
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{today.isoformat()}.md"
    path.write_text(content)
    log.info("archive written to %s", path)
    return path
