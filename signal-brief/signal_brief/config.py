"""Environment + paths. One module that everything else imports from."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT.parent  # claude-telegram/

# Load the bot's .env — we share it so PUSH_SECRET etc. aren't duplicated.
load_dotenv(BOT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env", override=False)  # local override if present

# Vault root must be configured via DEFAULT_CWD (shared with the bot) or
# SIGNAL_BRIEF_VAULT_ROOT (this project's own override). No fallback —
# missing config should fail loudly, not silently point at someone's home dir.
_vault_env = (
    os.environ.get("SIGNAL_BRIEF_VAULT_ROOT")
    or os.environ.get("DEFAULT_CWD")
)
VAULT_ROOT = Path(_vault_env).resolve() if _vault_env else None

DATA_DIR = PROJECT_ROOT / ".data"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = DATA_DIR / "logs"
CONFIG_DIR = PROJECT_ROOT / "config"

for p in (DATA_DIR, CACHE_DIR, LOG_DIR):
    p.mkdir(parents=True, exist_ok=True)

# Push endpoint (the bot's outbound HTTP server)
PUSH_HOST = os.environ.get("PUSH_HOST", "127.0.0.1")
PUSH_PORT = int(os.environ.get("PUSH_PORT", "7421"))
PUSH_SECRET = os.environ.get("PUSH_SECRET", "")
PUSH_URL = f"http://{PUSH_HOST}:{PUSH_PORT}/push"

# Claude CLI for the LLM filter pass.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# Use opus for the filter — this is intelligence work, not chat-from-phone.
SIGNAL_BRIEF_MODEL = os.environ.get("SIGNAL_BRIEF_MODEL", "opus")
SIGNAL_BRIEF_EFFORT = os.environ.get("SIGNAL_BRIEF_EFFORT", "high")

# Vault layout — these are conventional paths for an Obsidian-style vault.
# Override via env if your folders are named differently.
def _vault_path(env_key: str, default_rel: str) -> Path | None:
    if VAULT_ROOT is None:
        return None
    return Path(os.environ.get(env_key, str(VAULT_ROOT / default_rel)))


MEMORY_DIR = _vault_path("SIGNAL_BRIEF_MEMORY_DIR", ".claude-memory")
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md" if MEMORY_DIR else None
DAILY_NOTES_DIR = _vault_path("SIGNAL_BRIEF_DAILY_NOTES_DIR", "Daily Notes")
REVIEWS_DIR = _vault_path("SIGNAL_BRIEF_REVIEWS_DIR", "Reviews")
INBOX_DIR = _vault_path("SIGNAL_BRIEF_INBOX_DIR", "Inbox")


def assert_required() -> None:
    """Fail fast if critical config is missing."""
    if not PUSH_SECRET:
        raise SystemExit("PUSH_SECRET missing — set in claude-telegram/.env")
    if VAULT_ROOT is None:
        raise SystemExit(
            "VAULT_ROOT not configured — set DEFAULT_CWD (shared with bot) "
            "or SIGNAL_BRIEF_VAULT_ROOT to the absolute path of your vault"
        )
    if not VAULT_ROOT.exists():
        raise SystemExit(f"VAULT_ROOT does not exist: {VAULT_ROOT}")
