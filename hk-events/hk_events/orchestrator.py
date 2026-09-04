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
from collections.abc import Callable
from datetime import date
from typing import NamedTuple

from hk_events import board as board_mod
from hk_events import config, source_health
from hk_events.calendar_sync import sync_events
from hk_events.classifier import classify
from hk_events.concurrency import run_with_budget
from hk_events.dedupe import (
    STAGE_NEW,
    STAGE_SOON,
    collapse_cross_source,
    filter_due,
    mirror_collapsed,
    log_classification,
    record_verdict,
    save_seen,
)
from hk_events.errors import SourceNotConfiguredError
from hk_events.open_events import (
    OpenEvent,
    age_events,
    load_events,
    purge,
    save_events,
    upcoming,
    upsert_events,
)
from hk_events.render import render, render_vault_archive
from hk_events.schema import Event, RelevanceResult
from hk_events.sources import (
    aitinkerers,
    cyberport,
    luma,
    luma_discover,
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


def _source_tasks() -> list[tuple[str, Callable[[], list[Event]]]]:
    """The sources this run will attempt, in fetch order.

    Built per call so tests (and hot-patches) that swap a module attribute
    still take effect; the previous inline list had the same property.
    """
    return [
        # Clean iCal/feed tier
        ("meetup", meetup.fetch_meetup_events),
        ("luma", luma.fetch_luma_events),
        # Server-rendered structured-data tier (2026-09-01). Not DOM scraping:
        # both read a JSON island the server already ships in the initial HTML
        # (schema.org JSON-LD, and Next.js __NEXT_DATA__), so there are no CSS
        # selectors to rot. Each closes a hole the repo had written off:
        #   aitinkerers    — the recorded 403 is gone; the homepage serves the
        #                    chapter's events as schema.org Event objects.
        #   luma_discover  — standalone Luma events belong to no calendar and so
        #                    reach NO .ics feed (this is why CodeChella Week was
        #                    invisible). The city page lists them.
        # luma_discover overlaps `luma` on purpose — collapse_cross_source below
        # merges the duplicates.
        ("aitinkerers", aitinkerers.fetch_aitinkerers_events),
        ("luma_discover", luma_discover.fetch_luma_discover_events),
        # DISABLED 2026-08-09 — these two returned 0 events on every run for two
        # months and only added latency + log noise:
        #   cyberport    — HTTP 403 on every fetch (bot-blocked at the edge)
        #   startmeuphk  — scraper selectors never landed, parses 0
        # The adapters are kept, and as of 2026-09-02 both were ported to the
        # SourceFetchError contract — but re-enabling is NOT a one-line change,
        # and the previous wording here said it was. The port fixed the
        # TRANSPORT half only (a 403 or a network error now raises instead of
        # returning [], which source_health scored as a success). The PARSERS
        # still return [] when their placeholder selectors match nothing, which
        # is precisely startmeuphk's recorded failure mode — "selectors never
        # landed, parses 0" — so a 200 carrying the real DOM would still be
        # scored a clean success. Fix the selectors AND the parser's
        # empty-vs-unreadable signal before uncommenting either line.
        # Being absent from this list also
        # keeps them out of the staleness counters: they never run, so they
        # never land in the error map or the succeeded list, and update_health
        # prunes them.
        # ("cyberport", cyberport.fetch_cyberport_events),
        # ("startmeuphk", startmeuphk.fetch_startmeuphk_events),
        # Extension points (clean to add later): hktdc, hkstp, aws_summit_hk
    ]


def enabled_sources() -> list[str]:
    """Names of the sources this run attempts, derived from the real fetch list.

    NOT what drives the staleness counters. Those are fed by what the fetch
    phase actually observed — `_fetch_all_sources`' `succeeded` list and error
    map — precisely so a source that is in this list but never really reported
    cannot be scored as a success. This stays as an introspection accessor: it
    answers "what does a run try?", which is a different question from "what
    happened?".
    """
    return [name for name, _ in _source_tasks()]


def _fetch_all_sources() -> tuple[list[Event], dict[str, str], list[str]]:
    """Run every source adapter CONCURRENTLY.

    Returns `(events, errors, succeeded)`, where `succeeded` names the sources
    that actually completed a fetch. That third value is a POSITIVE success
    signal for `source_health` — see `update_health`. It is not derivable from
    the other two: "in the task list and not in the error map" is exactly the
    inference that let a total network outage reset every failure streak, and a
    source can legitimately land in NEITHER (see SourceNotConfiguredError
    below), so the two returned sets do not partition the task list.

    Mirrors job_sift/orchestrator.py::_fetch_all_sources. Individual failures
    are caught so one dead source never kills the run — but they are recorded
    in the returned error map (keyed by source name) so the digest + archive
    can surface a ⚠️ health line, the same way job-sift already does.

    Sources run in parallel under a hard wall-clock budget (see
    hk_events/concurrency.py for why an httpx timeout is not enough — this
    module's own `_ical_common._TIMEOUT = 25.0` did not stop a 135s DNS
    block). A source that blows the budget is abandoned, its partial result
    discarded, and it lands in the SAME error map as a crashed source — a
    timeout is a failed source, not a quiet zero.
    """
    events: list[Event] = []
    errors: dict[str, str] = {}
    succeeded: list[str] = []

    tasks = _source_tasks()

    budget_s = config.fetch_budget_s()
    settled, abandoned = run_with_budget(tasks, budget_s, thread_name_prefix="hk-events-fetch")

    for name, future in settled:
        try:
            got = future.result()
            log.info("%s: %d events", name, len(got))
            events.extend(got)
            # Returning at all is the success signal. An adapter that could not
            # look now raises SourceFetchError, so an empty list here honestly
            # means "I looked, there was nothing".
            succeeded.append(name)
        except SourceNotConfiguredError as exc:
            # NEITHER list. A source with no usable feed URL was never asked
            # anything, so this run is no evidence about it — scoring it a
            # success would reset a real failure streak and stamp a
            # `last_success` we never observed. Absent from both `succeeded`
            # and `errors`, update_health PRUNES it, the same as the two
            # adapters commented out of _source_tasks. Not a digest health line
            # either: nothing failed.
            log.warning("%s not configured — skipped, health record pruned: %s", name, exc.message)
        except Exception as exc:
            log.error("%s fetch failed: %s", name, exc)
            errors[name] = f"fetch failed: {exc}"

    for name in abandoned:
        log.error("%s fetch failed: exceeded the %.0fs fetch budget", name, budget_s)
        errors[name] = f"fetch failed: exceeded the {budget_s:.0f}s fetch budget"

    return events, errors, succeeded


def _update_event_register(
    events: list[Event],
    seen_by_source: dict[str, dict[str, dict]],
    today: date,
    *,
    dry_run: bool,
) -> tuple[list[OpenEvent], int]:
    """Fold this run's FETCHED events into the rolling register.

    Fetched, not surfaced — and the difference is the whole point. `filter_due`
    only yields an event at the two moments it deserves a notification, so a
    register fed from `surfaced` would record an event once and then never
    learn that its source is still listing it. `last_seen` would decay to
    "nobody has listed this in a month" for an event that is on every feed
    today, and the purge would then delete it. So every event inside the
    horizon is upserted every run, and the room tag rides along from the
    seen-set, where the classifier's verdict is already cached.

    Runs even on a zero-surfaced day: ageing and purging are time-driven, so
    the register would go stale if we only touched it when something new
    landed.
    """
    stored = load_events()
    rows = []
    for event in events:
        rec = (seen_by_source.get(event.source) or {}).get(event.external_id) or {}
        # `tag` may be absent (legacy state) or None (never classified). Both
        # mean UNTAGGED, and `upsert_events` leaves any stored tag alone rather
        # than clearing it — "nobody classified it this run" is not a verdict.
        rows.append((event, rec.get("tag"), None))
    merged = upsert_events(stored, rows, today)
    aged = age_events(merged, today)
    kept, purged = purge(
        aged,
        today,
        past_after_days=config.purge_past_after_days(),
        unseen_after_days=config.purge_unseen_after_days(),
        max_age_days=config.purge_max_age_days(),
    )
    for record, why in purged:
        log.info("purged %s (%s): %s", record.dedup_key, record.title[:60], why)
    log.info(
        "event register: %d row(s), %d upcoming, %d purged",
        len(kept), len(upcoming(kept)), len(purged),
    )

    if dry_run:
        log.info("dry-run — NOT writing open_events.json")
        return kept, len(purged)
    save_events(kept)
    return kept, len(purged)


class _BoardWrite(NamedTuple):
    """What happened to the board this run.

    A bare `Path | None` was not enough, and the gap showed up in the push:
    `None` meant three different things — dry run, no path configured, and a
    render that raised — while the summary bubble printed only one of them,
    "no board path configured". That is a cause the code never checked,
    reported to the one reader who could act on it. Same shape as every other
    bug this codebase keeps deleting: one value standing in for several facts.

    `path` is truthy exactly when a file was written, so `if result.path:`
    still reads naturally at the call sites.
    """

    path: object
    problem: str | None = None


def _write_board(records: list[OpenEvent], today: date, *, dry_run: bool):
    """Write the HTML board and the feed job-sift reads. Returns the board path,
    or None if nothing was written.

    Returns a `_BoardWrite`: the path when a file was written, otherwise the
    reason there is none, so the push can report the cause it actually
    observed rather than guessing at one.

    Wrapped whole: the board is a VIEW of state that is already persisted, so a
    failure to render it must never take down a run that has fetched,
    classified, pushed and saved. Returning None rather than a path that does
    not exist is what lets the push say "not written this run" instead of
    pointing the reader at an absent file.
    """
    if dry_run:
        # --dry-run writes no state, pushes nothing, and writes NO BOARD.
        log.info("dry-run — NOT writing the board")
        return _BoardWrite(None, "dry run")
    # The feed first, and above the board-path check: it is what the OTHER
    # service reads for its Events tab, so it is not conditional on this
    # deployment having somewhere to put an HTML file of its own.
    try:
        board_mod.write_feed(config.events_feed_path(), records, today)
    except Exception as exc:  # noqa: BLE001
        log.error("events feed could not be written: %s", exc)
    path = config.board_path()
    if path is None:
        log.info("no board path configured — skipping the board")
        return _BoardWrite(None, "no board path configured")
    try:
        html = board_mod.build_board(
            records, today, jobs_feed_path=config.jobs_feed_path()
        )
        return _BoardWrite(board_mod.write_board(path, html))
    except Exception as exc:  # noqa: BLE001 — a view must not kill the run
        log.error("board could not be written to %s: %s", path, exc)
        return _BoardWrite(None, f"could not be written to {path}")


def run(*, dry_run: bool = False, stub: bool = False) -> int:
    _setup_logging()
    today = date.today()
    log.info("hk-events starting for %s (dry_run=%s, stub=%s)", today.isoformat(), dry_run, stub)

    if stub:
        os.environ["HK_EVENTS_STUB"] = "1"

    if not stub and not dry_run:
        config.assert_required()

    # 1. Fetch raw events from all sources
    events, source_errors, fetched_ok = _fetch_all_sources()
    if source_errors:
        log.warning(
            "source health: %d source(s) did not run: %s",
            len(source_errors),
            ", ".join(sorted(source_errors)),
        )
    # 1b. Roll the per-source consecutive-failure counters forward and decide
    #     whether anything has been dead long enough to escalate. Computed
    #     BEFORE either render path so the alarm rides on the empty digest too
    #     — that is the run where "nothing today" is most likely to be believed.
    prior_health = source_health.load_health()
    health = source_health.update_health(
        prior_health,
        succeeded=fetched_ok,
        errors=source_errors,
        today=today,
    )
    staleness_alarm = source_health.render_alarm(health)
    if staleness_alarm:
        for name, rec in source_health.stale_sources(health):
            log.error(
                "STALE SOURCE %s: %d consecutive failed runs, last success %s",
                name,
                rec["consecutive_failures"],
                rec["last_success"] or "never",
            )
    # 1c. A source can also leave the counters entirely: pruned because it
    #     reported nothing this run. That is correct — you cannot be stale if
    #     nobody asked you anything — but it is the one way to make a STANDING
    #     alarm disappear without fixing anything, and the digest cannot show it
    #     (the ⚠️ health line is driven by the error map, and a pruned source is
    #     in neither map). Worse, the drop resets the re-arm clock: restore the
    #     config and the source comes back with no `first_seen` and no
    #     `last_success`, needing another ALARM_THRESHOLD runs before it can
    #     shout again. So a drop that took a live alarm with it gets one line in
    #     the push — not an alarm, because nothing failed; a statement of what
    #     stopped being watched. Self-clearing: the record is gone from the
    #     state file after this run, so the next run says nothing.
    drop_notice = source_health.render_drop_notice(prior_health, health)
    for name, rec in source_health.dropped_while_stale(prior_health, health):
        log.error(
            "DROPPED WHILE STALE %s: pruned with %d consecutive failed runs on the clock",
            name,
            rec["consecutive_failures"],
        )

    # Persisted before the push, not after: if the push itself blows up, the
    # failure that caused the alarm still happened and must survive the run.
    #
    # NOT under --stub, mirroring job-sift. hk-events is not vulnerable TODAY
    # — its two stub adapters (cyberport, startmeuphk) are both commented out
    # of _source_tasks, so a stub run still fetches all four live sources
    # (meetup, luma, aitinkerers, luma_discover) for real — but
    # the hazard is latent: re-enabling either is a documented one-line change,
    # and it would silently start resetting counters from canned data. A stub
    # run is not evidence about the real world, so it does not get to write the
    # record of it.
    if not dry_run and not stub:
        source_health.save_health(health)

    if not events:
        log.warning("no events fetched from any source — pushing heartbeat")
        # Ageing and purging are time-driven, so the register still needs a
        # pass on a dead day.
        records, purged = _update_event_register([], {}, today, dry_run=dry_run)
        board = _write_board(records, today, dry_run=dry_run)
        if not dry_run:
            push_messages(render(surfaced=[], total_new=0, total_processed=0, calendar_stats=None, today=today, source_errors=source_errors, staleness_alarm=staleness_alarm, drop_notice=drop_notice, board_path=board.path, board_problem=board.problem, upcoming_count=len(upcoming(records)), purged=purged))
            write_archive(today, render_vault_archive(surfaced=[], dropped=[], today=today, source_errors=source_errors, staleness_alarm=staleness_alarm, drop_notice=drop_notice))
        return 0

    log.info("fetched %d events across all sources", len(events))

    # 1d. Collapse events that two sources both reported. MUST run before the
    #     seen-set diff: `filter_due` keeps a separate seen-set per source, so a
    #     duplicate that survives to that point is notified twice, classified
    #     twice, and written to the calendar twice. `luma` and `luma_discover`
    #     really do overlap — see dedupe.collapse_cross_source.
    events, collapsed = collapse_cross_source(events)
    if collapsed:
        log.info("collapsed %d cross-source duplicate(s) — %d events remain",
                 len(collapsed), len(events))

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

    # 3b. Propagate each surviving event's seen-record back to the source(s) that
    #     lost the collapse. Without this the loser's state file never learns the
    #     event exists, so the run where the winner stops reporting it — the city
    #     page is a ~12-event listing, the .ics horizon is 45 days, so this is the
    #     normal life cycle, not an edge case — re-pushes it and writes a second
    #     calendar entry. AFTER record_verdict so the tag rides along.
    mirror_collapsed(seen_by_source, collapsed)

    # 4. Calendar sync (idempotent; gated by HK_EVENTS_CALENDAR_ENABLED + dry_run)
    calendar_stats = sync_events([e for e, _, _ in surfaced], dry_run=dry_run)
    log.info("calendar sync: %s", calendar_stats)

    # 4b. Roll the rolling register forward, then write the board — BEFORE the
    #     push, so the summary bubble can only point at a file that exists.
    records, purged = _update_event_register(events, seen_by_source, today, dry_run=dry_run)
    board = _write_board(records, today, dry_run=dry_run)

    # 5. Push to Telegram
    messages = render(
        surfaced=surfaced,
        total_new=n_new,
        total_processed=len(events),
        calendar_stats=calendar_stats,
        today=today,
        source_errors=source_errors,
        staleness_alarm=staleness_alarm,
        drop_notice=drop_notice,
        board_path=board.path,
        board_problem=board.problem,
        upcoming_count=len(upcoming(records)),
        purged=purged,
    )
    if dry_run:
        log.info("dry-run — would push %d messages:", len(messages))
        for m in messages:
            print(m)
            print("---")
    else:
        if (
            not surfaced
            and not config.HK_EVENTS_PUSH_EMPTY
            and staleness_alarm is None
            and drop_notice is None
        ):
            log.info("nothing surfaced — staying silent (HK_EVENTS_PUSH_EMPTY=0)")
        else:
            # A staleness alarm OVERRIDES the empty-digest silence above. The
            # gate exists so a daily "nothing today" doesn't train the reader to
            # stop opening the digest — but on the day a source has been dead
            # for three runs, that same silence is the bug: "nothing found" and
            # "I could not look" would render identically, which is exactly how
            # CEDARS stayed broken for fifty days in the sibling bot.
            #
            # A drop notice overrides it for the same reason, one step removed:
            # a source that was carrying a live alarm has just left the counters
            # because it had no config. Staying silent about that is how a real
            # alarm gets deleted by a YAML edit that nobody meant to make.
            push_messages(messages)
            log.info("pushed %d messages", len(messages))

        # 6. Persist the seen-set as soon as delivery has settled — a push that
        #    succeeded, or a deliberate silence — and BEFORE the archive write.
        #    These events have now been dispatched; nothing after this point may
        #    undeliver them. With the archive write in between, an OSError there
        #    exited non-zero after the push but before this line, so a re-run
        #    re-classified and re-pushed the same events. Nothing retries
        #    automatically today (the unit carries no Restart= — see
        #    systemd/hk-events.service), but a manual re-run has the same shape,
        #    and this ordering is what makes any future retry safe.
        for source, seen in seen_by_source.items():
            save_seen(source, seen)

    # 7. Vault archive (audit trail — after delivery is committed)
    archive_md = render_vault_archive(surfaced=surfaced, dropped=dropped, today=today, source_errors=source_errors, staleness_alarm=staleness_alarm, drop_notice=drop_notice)
    if not dry_run:
        write_archive(today, archive_md)

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
