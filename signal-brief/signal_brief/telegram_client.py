"""HTTP client that POSTs to the bot's /push endpoint."""

from __future__ import annotations

import logging
import time

import requests

from signal_brief.config import PUSH_SECRET, PUSH_URL

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
    """POST a list of messages to the bot's /push endpoint. Each entry becomes
    its own Telegram message bubble. Returns the response dict on success.
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
    for attempt in range(1, retries + 2):
        try:
            r = requests.post(PUSH_URL, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 200:
                result = r.json()
                log.info("push: sent=%d failed=%d",
                         len(result.get("sent", [])),
                         len(result.get("failed", [])))
                return result
            last_err = TelegramPushError(f"HTTP {r.status_code}: {r.text[:300]}")
            log.warning("push attempt %d failed: %s", attempt, last_err)
        except requests.RequestException as e:
            last_err = e
            log.warning("push attempt %d connection error: %s", attempt, e)
        time.sleep(min(2 ** attempt, 10))

    raise TelegramPushError(f"push failed after {retries + 1} attempts: {last_err}")
