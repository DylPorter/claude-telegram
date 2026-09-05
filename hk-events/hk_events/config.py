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
PUSH_DOCUMENT_URL = f"http://{PUSH_HOST}:{PUSH_PORT}/push-document"

# ---------------------------------------------------------------------------
# Board attachment — send the HTML board to Telegram as a document, so it is
# readable on a phone instead of only on the machine that wrote it.
#
# UNSET = OFF, and that is the default on purpose: the sibling project and
# anyone else running this fleet must be unaffected by the feature existing, and
# the operator must be able to turn it off without editing code.
#
# The value is the board KEY the bot serves out of its own PUSH_DOCUMENTS
# allowlist — NOT a path. This process never tells the bot which file to read;
# it names a key the bot has already been configured to map to a file. That is
# what stops the delivery endpoint being an arbitrary-file-read primitive, so
# do not "helpfully" turn this into a path.
BOARD_ATTACH_ENV = "HK_EVENTS_BOARD_ATTACH"


def board_attach_key() -> str | None:
    """The bot-side board key to attach to the digest, or None when off.

    Read at CALL time, not import time, so a one-off run (or a test) can toggle
    it with the env var without re-importing the package — same convention as
    HK_EVENTS_BOARD_PATH.
    """
    key = os.environ.get(BOARD_ATTACH_ENV, "").strip()
    return key or None

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
# 240s leaves ~11 minutes of the unit's TimeoutStartSec=900 for classify, push
# and state-save. Raise this only if you raise TimeoutStartSec with it.
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

# ---------------------------------------------------------------------------
# The board — ONE self-contained HTML file, rewritten every run.
#
# Defaults into the vault next to job-sift's, but it is deliberately a plain
# path: the file has no dependencies at all (no CDN, no build step, no server),
# so it is meant to be copyable to someone else's machine and opened from disk.
BOARD_PATH = _vault_path("HK_EVENTS_BOARD_PATH", "Areas/Work/Events Board.html")


def board_path() -> Path | None:
    """Resolved at CALL time, so a one-off run can redirect it with an env var."""
    override = os.environ.get("HK_EVENTS_BOARD_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return BOARD_PATH


def events_feed_path() -> Path:
    """Where hk-events PUBLISHES its rows for job-sift's Events tab."""
    override = os.environ.get("HK_EVENTS_EVENTS_FEED", "").strip()
    if override:
        return Path(override).expanduser()
    return STATE_DIR / "events_feed.json"


def jobs_feed_path() -> Path:
    """Where hk-events READS job-sift's rows for its own Jobs tab. Missing →
    the tab says the feed is missing; it never renders a fake zero."""
    override = os.environ.get("HK_EVENTS_JOBS_FEED", "").strip()
    if override:
        return Path(override).expanduser()
    return BOT_ROOT / "job-sift" / ".data" / "state" / "jobs_feed.json"


# ---------------------------------------------------------------------------
# The purge (see open_events.purge). Three clocks, all overridable.
def _positive_int_env(key: str, default: int) -> int:
    """Read at CALL time so a one-off run can override without re-importing."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer — using %d", key, raw, default)
        return default
    if value < 0:
        log.warning("%s=%s must not be negative — using %d", key, raw, default)
        return default
    return value


def purge_past_after_days() -> int:
    from hk_events.open_events import PURGE_PAST_AFTER_DAYS

    return _positive_int_env("HK_EVENTS_PURGE_PAST_DAYS", PURGE_PAST_AFTER_DAYS)


def purge_unseen_after_days() -> int:
    from hk_events.open_events import PURGE_UNSEEN_AFTER_DAYS

    return _positive_int_env("HK_EVENTS_PURGE_UNSEEN_DAYS", PURGE_UNSEEN_AFTER_DAYS)


def purge_max_age_days() -> int:
    from hk_events.open_events import PURGE_MAX_AGE_DAYS

    return _positive_int_env("HK_EVENTS_PURGE_MAX_AGE_DAYS", PURGE_MAX_AGE_DAYS)


def assert_required() -> None:
    """Fail fast on critical config."""
    if not PUSH_SECRET:
        raise SystemExit("PUSH_SECRET missing — set in claude-telegram/.env")
    if VAULT_ROOT and not VAULT_ROOT.exists():
        raise SystemExit(f"VAULT_ROOT does not exist: {VAULT_ROOT}")
