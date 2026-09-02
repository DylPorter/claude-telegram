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
# 240s leaves ~11 minutes of the unit's TimeoutStartSec=900 for classify, push
# and state-save. Raise this only if you raise TimeoutStartSec with it.
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


# ---------------------------------------------------------------------------
# LinkedIn liveness re-check (see job_sift/liveness.py and issue #1c).
#
# Bounded on purpose. The pass costs one HTTP request per row it checks, so the
# cap is what keeps a 200-row register from turning the daily run into a crawl,
# and the cooldown is what keeps the same row from being asked every morning.
# At 10 rows a run with a 7-day cooldown the whole open register is swept in
# well under a fortnight, which is far quicker than the 30-day stale rule this
# supplements.
LIVENESS_MAX_ENV = "JOB_SIFT_LIVENESS_MAX"
LIVENESS_MAX_DEFAULT = 10
LIVENESS_INTERVAL_ENV = "JOB_SIFT_LIVENESS_INTERVAL_DAYS"
LIVENESS_INTERVAL_DEFAULT = 7


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


def liveness_max_per_run() -> int:
    """0 disables the pass entirely — the documented kill switch."""
    return _positive_int_env(LIVENESS_MAX_ENV, LIVENESS_MAX_DEFAULT)


def liveness_interval_days() -> int:
    return _positive_int_env(LIVENESS_INTERVAL_ENV, LIVENESS_INTERVAL_DEFAULT)


# Hard wall-clock budget for the WHOLE liveness pass, in seconds.
#
# Needed for the same reason FETCH_BUDGET_S is, and it is the same class of bug:
# httpx's `timeout` is per socket OPERATION, not per request, so it bounds
# neither a redirect chain nor a slow-drip body. Measured against a local
# server: a 6-hop chain at 2s/hop took 14.0s and a drip-fed body 24.0s, both
# under a configured 10s timeout. `liveness._MAX_REDIRECTS` caps the first of
# those; only a wall-clock ceiling outside the request caps the second.
#
# 60s against the unit's TimeoutStartSec=900, on top of a fetch phase already
# allowed 240s. Real probes measure ~1.4s each, so the happy path never comes
# near this.
LIVENESS_BUDGET_ENV = "JOB_SIFT_LIVENESS_BUDGET_S"
LIVENESS_BUDGET_DEFAULT_S = 60.0


def liveness_budget_s() -> float:
    raw = os.environ.get(LIVENESS_BUDGET_ENV, "").strip()
    if not raw:
        return LIVENESS_BUDGET_DEFAULT_S
    try:
        budget = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number — using %.0fs", LIVENESS_BUDGET_ENV, raw, LIVENESS_BUDGET_DEFAULT_S)
        return LIVENESS_BUDGET_DEFAULT_S
    if budget <= 0:
        log.warning("%s=%s must be positive — using %.0fs", LIVENESS_BUDGET_ENV, raw, LIVENESS_BUDGET_DEFAULT_S)
        return LIVENESS_BUDGET_DEFAULT_S
    return budget


# CEDARS portal — set in .env once you know the exact URL.
CEDARS_PORTAL_URL = os.environ.get("CEDARS_PORTAL_URL", "")
# Path to a JSON file containing the CEDARS session cookie(s). Written
# automatically by `refresh_cookie.py` (pulled from Firefox's cookies.sqlite by
# default; see README's "Cookie refresh" section) before each scheduled run.
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
            "Log into https://web2.cedars.hku.hk/jobs/ in Firefox, then run "
            "./sift to refresh."
        )
    if VAULT_ROOT and not VAULT_ROOT.exists():
        raise SystemExit(f"VAULT_ROOT does not exist: {VAULT_ROOT}")
