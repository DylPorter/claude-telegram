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

from hk_events.config import PUSH_DOCUMENT_URL, PUSH_SECRET, PUSH_URL

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


# Telegram's ceiling for a media caption. The whole point of the caption is that
# the board arrives as ONE notification rather than a document plus a bubble, so
# a summary that will not fit has to fall back rather than be truncated.
MAX_CAPTION_CHARS = 1024


def push_document(
    board: str,
    *,
    caption: str | None = None,
    parse_mode: str = "Markdown",
    timeout: float = 120.0,
) -> dict:
    """POST a board KEY to the bot's /push-document endpoint.

    `board` is a key out of the BOT's `PUSH_DOCUMENTS` allowlist, never a path.
    This process cannot name a file to send: the mapping from key to path lives
    only in the bot's environment, which is what keeps the endpoint from being
    an arbitrary-file-read primitive.

    Deliberately NOT retried, unlike `push_messages`. Sending a document is not
    idempotent — a read timeout after Telegram has already accepted the upload
    would put the board in the chat twice on retry — and the caller degrades to
    a plain text bubble on failure anyway, so a retry buys nothing a human
    would notice.
    """
    if not PUSH_SECRET:
        raise TelegramPushError("PUSH_SECRET not configured")
    if caption is not None and len(caption) > MAX_CAPTION_CHARS:
        raise TelegramPushError(
            f"caption is {len(caption)} chars, over Telegram's {MAX_CAPTION_CHARS} limit"
        )

    payload: dict = {"board": board}
    if caption is not None:
        payload["caption"] = caption
        payload["parseMode"] = parse_mode
    headers = {
        "Content-Type": "application/json",
        "X-Push-Secret": PUSH_SECRET,
    }

    try:
        resp = requests.post(
            PUSH_DOCUMENT_URL, json=payload, headers=headers, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 — the caller degrades, it does not crash
        raise TelegramPushError(f"push-document failed: {exc}") from exc
    if resp.status_code >= 400:
        # Truncated: the body is the bot's own error string, and a long one would
        # otherwise be pasted whole into the fallback Telegram bubble.
        raise TelegramPushError(
            f"push-document failed: {resp.status_code} {resp.text[:200]}"
        )
    return resp.json()


def push_with_board(
    messages: list[str],
    *,
    summary_index: int,
    board_key: str | None,
    board_written: bool,
    parse_mode: str = "Markdown",
    send_text=None,
    send_document=None,
) -> None:
    """Deliver a run's messages, attaching the board to the summary bubble.

    ONE notification either way. With attachment on, the summary bubble IS the
    document's caption — it is not sent as a message of its own and then
    followed by a file. The fleet was cut from ~12 bubbles a day to ~5 on
    purpose; delivering the board must not quietly add one back.

    Ordering is preserved around the swap. The banners that lead the digest (a
    staleness alarm, a drop notice) go first and the source-health banner last,
    exactly as they do today, because a reader who stops after the first bubble
    has to have seen the alarm.

    DEGRADATION, which is the point of the whole shape: if the document cannot
    be sent, the summary still goes as a text bubble with the reason appended.
    A board that failed to reach the phone must never look like a quiet day.

    `send_text` / `send_document` are injection seams so a caller (and the test
    suite) can substitute the transport; they default to this module's.
    """
    send_text = send_text or push_messages
    send_document = send_document or push_document

    if not messages:
        return

    attach = bool(board_key) and board_written and 0 <= summary_index < len(messages)
    if not attach:
        if board_key and not board_written:
            # Not silent: attachment was asked for and did not happen. The board
            # write already reported its own reason in the summary bubble.
            log.info("board attachment configured but no board was written this run")
        send_text(messages)
        return

    before = messages[:summary_index]
    summary = messages[summary_index]
    after = messages[summary_index + 1 :]

    if before:
        send_text(before)

    try:
        send_document(board_key, caption=summary, parse_mode=parse_mode)
    except TelegramPushError as exc:
        log.error("board attachment failed: %s", exc)
        send_text([f"{summary}\n\n\u26a0\ufe0f Board not attached: {str(exc)[:200]}"])

    if after:
        send_text(after)
