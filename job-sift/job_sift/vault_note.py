"""Write the daily job-sift archive to the vault."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from job_sift.config import JOB_SIFT_ARCHIVE_DIR, OPEN_ROLES_PATH, VAULT_ROOT

log = logging.getLogger(__name__)


def write_archive(today: date, content: str) -> Path | None:
    """Write the rendered archive note to the vault. Returns the path or None if vault not configured."""
    if VAULT_ROOT is None or JOB_SIFT_ARCHIVE_DIR is None:
        log.info("vault not configured — skipping archive")
        return None

    JOB_SIFT_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = JOB_SIFT_ARCHIVE_DIR / f"{today.isoformat()}.md"
    path.write_text(content)
    log.info("archive written to %s", path)
    return path


def read_open_roles_note() -> str:
    """Return the current Open Roles note body, or "" if absent/unconfigured.

    Read before every write so hand-edited `<!-- status:applied ... -->` markers
    survive the rewrite.
    """
    if VAULT_ROOT is None or OPEN_ROLES_PATH is None:
        return ""
    if not OPEN_ROLES_PATH.exists():
        return ""
    try:
        return OPEN_ROLES_PATH.read_text()
    except Exception as exc:
        log.warning("failed to read %s: %s", OPEN_ROLES_PATH, exc)
        return ""


def write_open_roles(content: str) -> Path | None:
    """Write the rolling Open Roles register note. None if vault not configured."""
    if VAULT_ROOT is None or OPEN_ROLES_PATH is None:
        log.info("vault not configured — skipping open-roles note")
        return None

    OPEN_ROLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPEN_ROLES_PATH.write_text(content)
    log.info("open-roles note written to %s", OPEN_ROLES_PATH)
    return OPEN_ROLES_PATH
