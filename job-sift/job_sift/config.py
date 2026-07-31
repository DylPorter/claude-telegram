"""Environment + paths. Mirrors signal-brief's config conventions."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT.parent  # claude-telegram/

load_dotenv(BOT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Vault root for archiving the daily sift digest.
_vault_env = (
    os.environ.get("JOB_SIFT_VAULT_ROOT")
    or os.environ.get("DEFAULT_CWD")
)
VAULT_ROOT = Path(_vault_env).resolve() if _vault_env else None

DATA_DIR = PROJECT_ROOT / ".data"
STATE_DIR = DATA_DIR / "state"
LOG_DIR = DATA_DIR / "logs"
COOKIE_DIR = DATA_DIR / "cookies"
for p in (DATA_DIR, STATE_DIR, LOG_DIR, COOKIE_DIR):
    p.mkdir(parents=True, exist_ok=True)

# Telegram push endpoint (shared with the bot — same /push as signal-brief).
PUSH_HOST = os.environ.get("PUSH_HOST", "127.0.0.1")
PUSH_PORT = int(os.environ.get("PUSH_PORT", "7421"))
PUSH_SECRET = os.environ.get("PUSH_SECRET", "")
PUSH_URL = f"http://{PUSH_HOST}:{PUSH_PORT}/push"

# Claude CLI for classification. Same conventions as signal-brief.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# Haiku is plenty for per-listing prestige+scope binary classification.
# Override via env if you want to splurge on opus for ambiguous edge cases.
JOB_SIFT_MODEL = os.environ.get("JOB_SIFT_MODEL", "haiku")

# CEDARS portal — set in .env once you know the exact URL.
CEDARS_PORTAL_URL = os.environ.get("CEDARS_PORTAL_URL", "")
# Path to a JSON file containing the cookie jar exported from logged-in Chrome.
# See README for the manual cookie-export flow.
CEDARS_COOKIES_PATH = COOKIE_DIR / "cedars.json"

# Vault layout
def _vault_path(env_key: str, default_rel: str) -> Path | None:
    if VAULT_ROOT is None:
        return None
    return Path(os.environ.get(env_key, str(VAULT_ROOT / default_rel)))


JOB_SIFT_ARCHIVE_DIR = _vault_path("JOB_SIFT_ARCHIVE_DIR", "Inbox/Job Sift")
# Rolling "still open" register — one note, rewritten every run (see open_roles.py).
OPEN_ROLES_PATH = _vault_path("JOB_SIFT_OPEN_ROLES_PATH", "Areas/Work/Open Roles.md")


def assert_required() -> None:
    """Fail fast on critical config."""
    if not PUSH_SECRET:
        raise SystemExit("PUSH_SECRET missing — set in claude-telegram/.env")
    if not CEDARS_PORTAL_URL:
        raise SystemExit(
            "CEDARS_PORTAL_URL missing — set in job-sift/.env (the filtered "
            "NETJobs listings URL you actually browse)"
        )
    if not CEDARS_COOKIES_PATH.exists():
        raise SystemExit(
            f"CEDARS cookies not found at {CEDARS_COOKIES_PATH}. "
            "Export from Chrome — see README."
        )
    if VAULT_ROOT and not VAULT_ROOT.exists():
        raise SystemExit(f"VAULT_ROOT does not exist: {VAULT_ROOT}")
