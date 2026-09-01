"""Environment + paths. Mirrors signal-brief's config conventions."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT.parent  # claude-telegram/

load_dotenv(BOT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env", override=False)

log = logging.getLogger(__name__)

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

# ---------------------------------------------------------------------------
# Hard wall-clock budget for the whole source-fetch phase, in seconds.
#
# Sources are fetched concurrently (see job_sift/concurrency.py); anything still
# running when this expires is abandoned, recorded as a failed source, and the
# run continues with whatever landed. This is the guard that survived the
# 2026-09-01 DNS outage: an httpx timeout does NOT bound getaddrinfo, so the
# ceiling has to be enforced from outside the fetch call.
#
# 240s leaves ~6 minutes of the unit's TimeoutStartSec=600 for classify, push
# and state-save.
FETCH_BUDGET_ENV = "JOB_SIFT_FETCH_BUDGET_S"
FETCH_BUDGET_DEFAULT_S = 240.0


def fetch_budget_s() -> float:
    """Resolve the fetch budget.

    Read at CALL time, not import time, so a one-off run (or a test) can
    override it with JOB_SIFT_FETCH_BUDGET_S without re-importing the package.
    """
    raw = os.environ.get(FETCH_BUDGET_ENV, "").strip()
    if not raw:
        return FETCH_BUDGET_DEFAULT_S
    try:
        budget = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number — using %.0fs", FETCH_BUDGET_ENV, raw, FETCH_BUDGET_DEFAULT_S)
        return FETCH_BUDGET_DEFAULT_S
    if budget <= 0:
        log.warning("%s=%s must be positive — using %.0fs", FETCH_BUDGET_ENV, raw, FETCH_BUDGET_DEFAULT_S)
        return FETCH_BUDGET_DEFAULT_S
    return budget


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
