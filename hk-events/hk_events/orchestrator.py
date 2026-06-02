"""Daily hk-events orchestrator. Wires sources → dedupe → classify → calendar → push.

Mirrors job-sift/orchestrator.py: per-source try/except fetch, seen-set diff,
per-event classification, Telegram render+push, vault archive, seen-set persist
AFTER a successful push. Adds an idempotent Google Calendar sync step for
surfaced events.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from hk_events import config
from hk_events.calendar_sync import sync_events
from hk_events.classifier import classify
from hk_events.dedupe import filter_new, load_seen, log_classification, save_seen
from hk_events.render import render, render_vault_archive
from hk_events.schema import Event, RelevanceResult
from hk_events.sources import (
    aitinkerers,
    cyberport,
    luma,
    meetup,
    startmeuphk,
)
from hk_events.telegram_client import push_messages
from hk_events.vault_note import write_archive

log = logging.getLogger("hk_events")


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("HK_EVENTS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )


def _fetch_all_sources() -> list[Event]:
    """Run every source adapter, swallow individual failures, return combined events.

    Feed (iCal) sources first — clean. Scrape sources after — brittle, each one
    already degrades to [] internally, but we also wrap here for belt-and-braces.
    """
    events: list[Event] = []
    fetchers = [
        # Clean iCal/feed tier
        (meetup.fetch_meetup_events, "meetup"),
        (luma.fetch_luma_events, "luma"),
        (aitinkerers.fetch_aitinkerers_events, "aitinkerers"),
        # Brittle scrape tier
        (cyberport.fetch_cyberport_events, "cyberport"),
        (startmeuphk.fetch_startmeuphk_events, "startmeuphk"),
        # Extension points (clean to add later): hktdc, hkstp, aws_summit_hk
    ]
    for fetch_fn, name in fetchers:
        try:
            got = fetch_fn()
            log.info("%s: %d events", name, len(got))
            events.extend(got)
        except Exception as exc:
            log.error("%s fetch failed: %s", name, exc)
    return events


def run(*, dry_run: bool = False, stub: bool = False) -> int:
    _setup_logging()
    today = date.today()
    log.info("hk-events starting for %s (dry_run=%s, stub=%s)", today.isoformat(), dry_run, stub)

    if stub:
        os.environ["HK_EVENTS_STUB"] = "1"

    if not stub and not dry_run:
        config.assert_required()

    # 1. Fetch raw events from all sources
    events = _fetch_all_sources()
    if not events:
        log.warning("no events fetched from any source — pushing heartbeat")
        if not dry_run:
            push_messages(render(surfaced=[], total_new=0, total_processed=0, calendar_stats=None, today=today))
        return 0

    log.info("fetched %d events across all sources", len(events))

    # 2. Diff against seen-sets (per-source) — only SURFACE new ones
    new_events, seen_by_source = filter_new(events)
    log.info("%d new events (after dedupe)", len(new_events))

    # 3. Classify each new event (precision-biased: uncertain → drop)
    surfaced: list[tuple[Event, RelevanceResult]] = []
    dropped: list[tuple[Event, RelevanceResult]] = []
    for event in new_events:
        result = classify(event)
        log_classification(event, result)
        if result.surface:
            surfaced.append((event, result))
        else:
            dropped.append((event, result))
        log.info("[%s] %s — %s: %s", event.source, event.title[:50], result.tag, result.reason)

    log.info("%d surfaced, %d dropped", len(surfaced), len(dropped))

    # 4. Calendar sync (idempotent; gated by HK_EVENTS_CALENDAR_ENABLED + dry_run)
    calendar_stats = sync_events([e for e, _ in surfaced], dry_run=dry_run)
    log.info("calendar sync: %s", calendar_stats)

    # 5. Push to Telegram
    messages = render(
        surfaced=surfaced,
        total_new=len(new_events),
        total_processed=len(events),
        calendar_stats=calendar_stats,
        today=today,
    )
    if dry_run:
        log.info("dry-run — would push %d messages:", len(messages))
        for m in messages:
            print(m)
            print("---")
    else:
        push_messages(messages)
        log.info("pushed %d messages", len(messages))

    # 6. Vault archive (audit trail)
    archive_md = render_vault_archive(surfaced=surfaced, dropped=dropped, today=today)
    if not dry_run:
        write_archive(today, archive_md)

    # 7. Persist seen-set (only after successful push)
    if not dry_run:
        for source, seen in seen_by_source.items():
            save_seen(source, seen)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="hk-events daily orchestrator")
    parser.add_argument("--dry-run", action="store_true",
                        help="don't push to Telegram, don't write the calendar (validate only), don't persist state")
    parser.add_argument("--stub", action="store_true", help="use stub source data (skip live scraping/feeds)")
    args = parser.parse_args()
    return run(dry_run=args.dry_run, stub=args.stub)


if __name__ == "__main__":
    sys.exit(main())
