"""HTTP client that POSTs to the bot's /push endpoint.

Copied from job-sift (which copied it from signal-brief) — small enough that
duplication is cheaper than cross-package import coupling. If the push
interface ever drifts, lift this to a shared module.

RECOMMENDATION (not done here, per task constraints): this is now the THIRD
identical copy (signal-brief, job-sift, hk-events). A shared
`claude_telegram_push` package would be the right refactor — left as a note.
"""

from __future__ import annotations

import logging
import time

import requests

from hk_events.config import PUSH_SECRET, PUSH_URL

log = logging.getLogger(__name__)


class TelegramPushError(RuntimeError):
    pass


def push_messages(
    messages: list[str],
    *,
    parse_mode: str = "Markdown",
    disable_preview: bool = True,
    delay_ms: int = 350,
    timeout: float = 60.0,
    retries: int = 2,
) -> dict:
    """POST a list of messages to the bot's /push endpoint.

    Each entry becomes its own Telegram message bubble.
    """
    if not PUSH_SECRET:
        raise TelegramPushError("PUSH_SECRET not configured")

    payload = {
        "messages": messages,
        "parseMode": parse_mode,
        "disablePreview": disable_preview,
        "delayMs": delay_ms,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Push-Secret": PUSH_SECRET,
    }

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(PUSH_URL, json=payload, headers=headers, timeout=timeout)
            if resp.status_code >= 400:
                raise TelegramPushError(f"push failed: {resp.status_code} {resp.text}")
            return resp.json()
        except Exception as exc:
            last_err = exc
            log.warning("push attempt %d failed: %s", attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))

    assert last_err is not None
    raise TelegramPushError(f"push failed after {retries + 1} attempts: {last_err}")
