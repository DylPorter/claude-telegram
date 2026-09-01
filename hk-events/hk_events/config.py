"""Environment + paths. Mirrors job-sift/config.py (which mirrors signal-brief)."""

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

# Vault root for archiving the daily events digest.
_vault_env = (
    os.environ.get("HK_EVENTS_VAULT_ROOT")
    or os.environ.get("DEFAULT_CWD")
)
VAULT_ROOT = Path(_vault_env).resolve() if _vault_env else None

DATA_DIR = PROJECT_ROOT / ".data"
STATE_DIR = DATA_DIR / "state"
CACHE_DIR = DATA_DIR / "cache"  # calendar idempotency map lives here (per signal-brief convention)
LOG_DIR = DATA_DIR / "logs"
CONFIG_DIR = PROJECT_ROOT / "config"
for p in (DATA_DIR, STATE_DIR, CACHE_DIR, LOG_DIR):
    p.mkdir(parents=True, exist_ok=True)

# Telegram push endpoint (shared with the bot — same /push as signal-brief + job-sift).
PUSH_HOST = os.environ.get("PUSH_HOST", "127.0.0.1")
PUSH_PORT = int(os.environ.get("PUSH_PORT", "7421"))
PUSH_SECRET = os.environ.get("PUSH_SECRET", "")
PUSH_URL = f"http://{PUSH_HOST}:{PUSH_PORT}/push"

# Claude CLI for relevance classification. Same convention as job-sift.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# Haiku is plenty for per-event binary room classification.
HK_EVENTS_MODEL = os.environ.get("HK_EVENTS_MODEL", "haiku")

# ---------------------------------------------------------------------------
# Hard wall-clock budget for the whole source-fetch phase, in seconds.
#
# Sources are fetched concurrently (see hk_events/concurrency.py); anything still
# running when this expires is abandoned, recorded as a failed source, and the
# run continues with whatever landed. This is the guard that survived the
# 2026-09-01 DNS outage: an httpx timeout does NOT bound getaddrinfo, so the
# ceiling has to be enforced from outside the fetch call.
#
# 240s leaves ~6 minutes of the unit's TimeoutStartSec=600 for classify, push
# and state-save.
FETCH_BUDGET_ENV = "HK_EVENTS_FETCH_BUDGET_S"
FETCH_BUDGET_DEFAULT_S = 240.0


def fetch_budget_s() -> float:
    """Resolve the fetch budget.

    Read at CALL time, not import time, so a one-off run (or a test) can
    override it with HK_EVENTS_FETCH_BUDGET_S without re-importing the package.
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


# gws (Google Workspace CLI) binary — used for calendar writes. Same tool
# signal-brief uses for Gmail. On PATH by default; override if needed.
GWS_BIN = os.environ.get("GWS_BIN", "gws")

# Target calendar for auto-created events. "primary" = the operator's main calendar.
# Set to a dedicated secondary calendar ID to keep events separate.
HK_EVENTS_CALENDAR_ID = os.environ.get("HK_EVENTS_CALENDAR_ID", "primary")
# Master switch for calendar writes. Default OFF so a first run (or a forgotten
# --dry-run) never silently writes to a real calendar. Set to "1" to enable.
HK_EVENTS_CALENDAR_ENABLED = os.environ.get("HK_EVENTS_CALENDAR_ENABLED", "0") == "1"

# Only look at events starting within this many days (rolling horizon).
HK_EVENTS_HORIZON_DAYS = int(os.environ.get("HK_EVENTS_HORIZON_DAYS", "45"))

# Push a "nothing today" heartbeat to Telegram on empty runs. Default OFF:
# 56 of the first 64 runs surfaced nothing, and a daily no-op message is what
# trains you to stop opening the digest. Silence is the signal; the vault
# archive + journalctl remain the audit trail proving the run happened.
HK_EVENTS_PUSH_EMPTY = os.environ.get("HK_EVENTS_PUSH_EMPTY", "0") == "1"

# Re-surface an already-seen event this many days before it starts. Discovery
# alone isn't enough: an event spotted 6 weeks out gets forgotten long before
# it happens, so each event fires twice — once on discovery, once on approach.
HK_EVENTS_REMINDER_DAYS = int(os.environ.get("HK_EVENTS_REMINDER_DAYS", "3"))


# Vault layout
def _vault_path(env_key: str, default_rel: str) -> Path | None:
    if VAULT_ROOT is None:
        return None
    return Path(os.environ.get(env_key, str(VAULT_ROOT / default_rel)))


HK_EVENTS_ARCHIVE_DIR = _vault_path("HK_EVENTS_ARCHIVE_DIR", "Inbox/HK Events")


def assert_required() -> None:
    """Fail fast on critical config."""
    if not PUSH_SECRET:
        raise SystemExit("PUSH_SECRET missing — set in claude-telegram/.env")
    if VAULT_ROOT and not VAULT_ROOT.exists():
        raise SystemExit(f"VAULT_ROOT does not exist: {VAULT_ROOT}")
