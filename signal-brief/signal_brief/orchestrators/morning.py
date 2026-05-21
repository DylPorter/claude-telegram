"""Morning brief orchestrator.

Fetch all sources → LLM filter → write daily note → push chunked Telegram.

Usage:
    .venv/bin/python -m signal_brief.orchestrators.morning            # full run
    .venv/bin/python -m signal_brief.orchestrators.morning --dry-run  # print, no push
    .venv/bin/python -m signal_brief.orchestrators.morning --no-cache # ignore seen-urls
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from signal_brief.config import LOG_DIR, assert_required
from signal_brief.daily_note import upsert_signal_section
from signal_brief.exposure import record_digest
from signal_brief.filter import filter_items
from signal_brief.render import render_for_daily_note, render_for_telegram
from signal_brief.schema import Item
from signal_brief.sources import (
    fetch_conferences,
    fetch_newsletters,
    fetch_rss,
    fetch_twitter,
)
from signal_brief.telegram_client import TelegramPushError, push_messages


def _setup_logging(date_str: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{date_str}-morning.log"
    handlers = [
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _collect(*, skip_seen: bool) -> list[Item]:
    """Run every source, swallow per-source failures, return combined items."""
    log = logging.getLogger("collect")
    all_items: list[Item] = []

    for name, fn, kwargs in [
        ("conferences", fetch_conferences, {}),
        ("rss",         fetch_rss,         {"skip_seen": skip_seen}),
        ("newsletters", fetch_newsletters, {}),
        ("twitter",     fetch_twitter,     {}),
    ]:
        try:
            items = fn(**kwargs)
            log.info("source %s: %d items", name, len(items))
            all_items.extend(items)
        except Exception as e:
            log.exception("source %s crashed: %s — continuing", name, e)

    return all_items


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the morning signal brief.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print Telegram messages to stdout; don't push or write daily note.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore the seen-urls cache (useful for backfill/test).")
    parser.add_argument("--items-from", type=Path, default=None,
                        help="Skip fetching; load items from this JSON file (test mode).")
    args = parser.parse_args()

    today = date.today().isoformat()
    _setup_logging(today)
    log = logging.getLogger("morning")
    assert_required()

    log.info("=== morning brief %s (dry_run=%s no_cache=%s) ===",
             today, args.dry_run, args.no_cache)

    if args.items_from:
        # Test path: pre-collected items from JSON.
        with open(args.items_from) as f:
            raw = json.load(f)
        items = [Item(**r) for r in raw]
        log.info("loaded %d items from %s", len(items), args.items_from)
    else:
        items = _collect(skip_seen=not args.no_cache)

    log.info("=== %d total items to filter ===", len(items))

    digest = filter_items(items, today=today)

    telegram_messages = render_for_telegram(digest)
    daily_note_md = render_for_daily_note(digest)

    if args.dry_run:
        print("\n" + "=" * 60)
        print(f"DRY RUN — would push {len(telegram_messages)} Telegram messages:")
        print("=" * 60)
        for i, msg in enumerate(telegram_messages, 1):
            print(f"\n--- message {i} ({len(msg)} chars) ---")
            print(msg)
        print("\n" + "=" * 60)
        print("DAILY NOTE SECTION (would replace):")
        print("=" * 60)
        print(daily_note_md)
        return 0

    # Write daily note FIRST (audit trail) — if Telegram fails, we still have it.
    note_path = upsert_signal_section(today, daily_note_md)
    log.info("wrote daily note: %s", note_path)

    # Push to Telegram.
    try:
        result = push_messages(telegram_messages)
        log.info("telegram push: %d sent, %d failed",
                 len(result.get("sent", [])), len(result.get("failed", [])))
    except TelegramPushError as e:
        log.error("telegram push failed: %s — daily note still written", e)
        return 2

    # Update exposure log (anti-bubble state).
    record_digest(digest)

    log.info("=== morning brief done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
