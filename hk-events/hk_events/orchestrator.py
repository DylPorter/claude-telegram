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
from hk_events.dedupe import (
    STAGE_NEW,
    STAGE_SOON,
    filter_due,
    log_classification,
    record_verdict,
    save_seen,
)
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


def _fetch_all_sources() -> tuple[list[Event], dict[str, str]]:
    """Run every source adapter, return (combined events, per-source errors).

    Mirrors job_sift/orchestrator.py::_fetch_all_sources. Individual failures
    are caught so one dead source never kills the run — but they are recorded
    in the returned error map (keyed by source name) so the digest + archive
    can surface a ⚠️ health line, the same way job-sift already does.

    Feed (iCal) sources first — clean. Scrape sources after — brittle, each one
    already degrades to [] internally, but we also wrap here for belt-and-braces.
    """
    events: list[Event] = []
    errors: dict[str, str] = {}
    fetchers = [
        # Clean iCal/feed tier
        (meetup.fetch_meetup_events, "meetup"),
        (luma.fetch_luma_events, "luma"),
        # DISABLED 2026-08-09 — these three returned 0 events on every run for
        # two months and only added latency + log noise:
        #   aitinkerers  — no public feed exists (email-only chapter)
        #   cyberport    — HTTP 403 on every fetch (bot-blocked at the edge)
        #   startmeuphk  — scraper selectors never landed, parses 0
        # The adapters are kept so re-enabling is a one-line change once any of
        # them has a real feed.
        # (aitinkerers.fetch_aitinkerers_events, "aitinkerers"),
        # (cyberport.fetch_cyberport_events, "cyberport"),
        # (startmeuphk.fetch_startmeuphk_events, "startmeuphk"),
        # Extension points (clean to add later): hktdc, hkstp, aws_summit_hk
    ]
    for fetch_fn, name in fetchers:
        try:
            got = fetch_fn()
            log.info("%s: %d events", name, len(got))
            events.extend(got)
        except Exception as exc:
            log.error("%s fetch failed: %s", name, exc)
            errors[name] = f"fetch failed: {exc}"
    return events, errors


def run(*, dry_run: bool = False, stub: bool = False) -> int:
    _setup_logging()
    today = date.today()
    log.info("hk-events starting for %s (dry_run=%s, stub=%s)", today.isoformat(), dry_run, stub)

    if stub:
        os.environ["HK_EVENTS_STUB"] = "1"

    if not stub and not dry_run:
        config.assert_required()

    # 1. Fetch raw events from all sources
    events, source_errors = _fetch_all_sources()
    if source_errors:
        log.warning(
            "source health: %d source(s) did not run: %s",
            len(source_errors),
            ", ".join(sorted(source_errors)),
        )
    if not events:
        log.warning("no events fetched from any source — pushing heartbeat")
        if not dry_run:
            push_messages(render(surfaced=[], total_new=0, total_processed=0, calendar_stats=None, today=today, source_errors=source_errors))
            write_archive(today, render_vault_archive(surfaced=[], dropped=[], today=today, source_errors=source_errors))
        return 0

    log.info("fetched %d events across all sources", len(events))

    # 2. Diff against seen-sets (per-source). Two notification stages: newly
    #    discovered, and starting within HK_EVENTS_REMINDER_DAYS.
    due, seen_by_source = filter_due(events)
    n_new = sum(1 for _, stage, _ in due if stage == STAGE_NEW)
    n_soon = sum(1 for _, stage, _ in due if stage == STAGE_SOON)
    log.info("%d due (%d newly discovered, %d starting soon)", len(due), n_new, n_soon)

    # 3. Classify (precision-biased: uncertain → drop). Reminder-stage events
    #    reuse the verdict cached at discovery — no second LLM call, and no risk
    #    of a non-deterministic re-classify flipping a settled decision. A null
    #    cached tag means legacy state, so fall back to classifying.
    surfaced: list[tuple[Event, RelevanceResult, str]] = []
    dropped: list[tuple[Event, RelevanceResult, str]] = []
    for event, stage, cached_tag in due:
        if stage == STAGE_SOON and cached_tag:
            result = RelevanceResult(tag=cached_tag, reason="cached verdict from discovery run")
        else:
            result = classify(event)
            log_classification(event, result)
        record_verdict(seen_by_source, event, result.tag)
        if result.surface:
            surfaced.append((event, result, stage))
        else:
            dropped.append((event, result, stage))
        log.info("[%s/%s] %s — %s: %s", event.source, stage, event.title[:50], result.tag, result.reason)

    log.info("%d surfaced, %d dropped", len(surfaced), len(dropped))

    # 4. Calendar sync (idempotent; gated by HK_EVENTS_CALENDAR_ENABLED + dry_run)
    calendar_stats = sync_events([e for e, _, _ in surfaced], dry_run=dry_run)
    log.info("calendar sync: %s", calendar_stats)

    # 5. Push to Telegram
    messages = render(
        surfaced=surfaced,
        total_new=n_new,
        total_processed=len(events),
        calendar_stats=calendar_stats,
        today=today,
        source_errors=source_errors,
    )
    if dry_run:
        log.info("dry-run — would push %d messages:", len(messages))
        for m in messages:
            print(m)
            print("---")
    elif not surfaced and not config.HK_EVENTS_PUSH_EMPTY:
        log.info("nothing surfaced — staying silent (HK_EVENTS_PUSH_EMPTY=0)")
    else:
        push_messages(messages)
        log.info("pushed %d messages", len(messages))

    # 6. Vault archive (audit trail)
    archive_md = render_vault_archive(surfaced=surfaced, dropped=dropped, today=today, source_errors=source_errors)
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
