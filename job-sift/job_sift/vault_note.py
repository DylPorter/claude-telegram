"""Write the daily job-sift archive to the vault."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from job_sift import config

log = logging.getLogger(__name__)


# Every path below is resolved through the `config` MODULE on each call, never
# bound at import time. Same reasoning as hk-events' vault_note (commit
# 3a19ac0) and `dedupe._seen_path`: a test that points `config.VAULT_ROOT` /
# `config.JOB_SIFT_ARCHIVE_DIR` / `config.OPEN_ROLES_PATH` at a tmp_path must
# actually redirect the write. With `from job_sift.config import VAULT_ROOT`
# the patch silently did nothing and the write landed in the REAL vault —
# which for `write_open_roles` means overwriting the operator's rolling Open
# Roles register, and it was stubbed in exactly one test out of the suite.
def write_archive(today: date, content: str) -> Path | None:
    """Write the rendered archive note to the vault. Returns the path or None if vault not configured."""
    archive_dir = config.JOB_SIFT_ARCHIVE_DIR
    if config.VAULT_ROOT is None or archive_dir is None:
        log.info("vault not configured — skipping archive")
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{today.isoformat()}.md"
    path.write_text(content)
    log.info("archive written to %s", path)
    return path


def read_open_roles_note() -> str:
    """Return the current Open Roles note body, or "" if absent/unconfigured.

    Read before every write so hand-edited `<!-- status:applied ... -->` markers
    survive the rewrite.
    """
    path = config.OPEN_ROLES_PATH
    if config.VAULT_ROOT is None or path is None:
        return ""
    if not path.exists():
        return ""
    try:
        return path.read_text()
    except Exception as exc:
        log.warning("failed to read %s: %s", path, exc)
        return ""


def write_open_roles(content: str) -> Path | None:
    """Write the rolling Open Roles register note. None if vault not configured."""
    path = config.OPEN_ROLES_PATH
    if config.VAULT_ROOT is None or path is None:
        log.info("vault not configured — skipping open-roles note")
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    log.info("open-roles note written to %s", path)
    return path
