"""Daily X/Twitter drafts → Telegram. Runs 08:30 HKT, after the morning brief.

Picks the 3 most talked-about AI/dev topics of the day (HN, GitHub trending,
AI/dev RSS), writes one ready-to-post draft per topic grounded in a fixed facts
block, and pushes them as plain-text bubbles for copy-paste. Never touches X.

Usage:
    .venv/bin/python -m signal_brief.orchestrators.x_drafts            # full run
    .venv/bin/python -m signal_brief.orchestrators.x_drafts --dry-run  # print, no push, no history write
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from signal_brief import x_drafts
from signal_brief.config import LOG_DIR, assert_required
from signal_brief.telegram_client import TelegramPushError, push_messages


def _setup_logging(date_str: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"{date_str}-xdrafts.log"),
            logging.StreamHandler(sys.stderr),
        ],
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send today's X post drafts to Telegram.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the messages; don't push or record topics as used.")
    args = parser.parse_args(argv)

    today = date.today()
    _setup_logging(today.isoformat())
    log = logging.getLogger("x_drafts")
    assert_required()
    log.info("=== x drafts %s (dry_run=%s) ===", today, args.dry_run)

    try:
        res = x_drafts.run(today=today)
    except Exception as e:  # noqa: BLE001
        log.exception("x drafts failed: %s", e)
        if not args.dry_run:
            try:
                push_messages([f"⚠️ x drafts failed today: {str(e)[:200]}"], parse_mode=None)
            except TelegramPushError:
                pass
        return 1

    messages = x_drafts.render_messages(res)

    if args.dry_run:
        for i, m in enumerate(messages, 1):
            print(f"\n--- message {i} ({len(m)} chars) ---\n{m}")
        return 0

    try:
        result = push_messages(messages, parse_mode=None, disable_preview=True)
    except TelegramPushError as e:
        log.error("telegram push failed: %s", e)
        return 2
    if result.get("failed"):
        log.warning("some bubbles failed: %s", result["failed"])

    # Only mark topics used once they actually reached the phone.
    x_drafts.save_history(x_drafts.load_history(), x_drafts.history_entries(res), today=today)
    log.info("=== x drafts done: %d drafts, %d dropped ===", len(res.entries), len(res.dropped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
