"""Daily job-sift orchestrator. Wires sources → dedupe → classifier → push."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from datetime import date

from job_sift import config, liveness, source_health
from job_sift.classifier import classify, classify_batch, classify_scope_only
from job_sift.concurrency import run_with_budget
from job_sift.dedupe import (
    collapse_duplicates,
    filter_new,
    load_seen,
    log_classification,
    mirror_collapsed,
    save_seen,
    withhold_unclassified,
)
from job_sift.errors import SourceAuthError, SourceNotConfiguredError
from job_sift.open_roles import (
    OpenRole,
    active_roles,
    age_roles,
    apply_liveness,
    apply_status_overrides,
    closing_within,
    collapse_register,
    in_lane,
    load_open_roles,
    parse_status_overrides,
    prune,
    roles_due_liveness_check,
    save_open_roles,
    upsert_roles,
)
from job_sift.render import render, render_open_roles, render_vault_archive
from job_sift.schema import ClassifierResult, JobListing
from job_sift.sources import ashby, cedars, greenhouse, lever, linkedin
from job_sift.telegram_client import push_messages
from job_sift.vault_note import read_open_roles_note, write_archive, write_open_roles

log = logging.getLogger("job_sift")


# Sources whose curation already implies prestige — we skip the prestige
# classifier and just check scope (intern/contract vs FT-perm).
_AUTO_PRESTIGE_SOURCES: set[str] = {"greenhouse", "lever", "ashby"}

# Concurrent liveness probes allowed at once. Every probe targets linkedin.com,
# so this is a per-HOST cap, not a throughput knob — see `_liveness_pass`.
_LIVENESS_MAX_IN_FLIGHT = 2


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("JOB_SIFT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )


def _source_tasks(cedars_seen: set[str]) -> list[tuple[str, Callable[[], list[JobListing]]]]:
    """The sources this run will attempt, in fetch order.

    Built per call so tests (and hot-patches) that swap a module attribute
    still take effect; the previous inline list had the same property.
    """
    return [
        # CEDARS — greedy pagination with the pre-loaded seen-set
        ("cedars", lambda: cedars.fetch_cedars_listings(seen_ids=cedars_seen)),
        # Standardized-ATS sources — public JSON APIs, no pagination needed
        ("greenhouse", greenhouse.fetch_greenhouse_listings),
        ("lever", lever.fetch_lever_listings),
        ("ashby", ashby.fetch_ashby_listings),
        # LinkedIn — gws CLI Gmail digest email parsing
        ("linkedin", linkedin.fetch_linkedin_listings),
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
    return [name for name, _ in _source_tasks(set())]


def _fetch_all_sources() -> tuple[list[JobListing], dict[str, str], list[str]]:
    """Run every source adapter CONCURRENTLY.

    Returns `(listings, errors, succeeded)`, where `succeeded` names the sources
    that actually completed a fetch. That third value is a POSITIVE success
    signal for `source_health` — see `update_health`. It is not derivable from
    the other two: "in the task list and not in the error map" is exactly the
    inference that let a total network outage reset every failure streak, and a
    source can legitimately land in NEITHER (see SourceNotConfiguredError
    below), so the two returned sets do not partition the task list.

    Individual failures are caught so one dead source never kills the run — but
    they are recorded in the returned error map (keyed by source name) so the
    digest + archive can surface a ⚠️ health line. An auth failure raising
    SourceAuthError is the load-bearing case: it turns a silent "None today"
    into a visible "this source did not run".

    Sources run in parallel under a hard wall-clock budget (see
    job_sift/concurrency.py for why an httpx timeout is not enough). A source
    that blows the budget is abandoned, its partial result discarded, and it
    lands in the SAME error map as a crashed source — a timeout is a failed
    source, not a quiet zero.
    """
    listings: list[JobListing] = []
    errors: dict[str, str] = {}
    succeeded: list[str] = []

    # CEDARS uses the seen-set to drive its greedy pagination (fetch page 1,
    # then 2, ... stopping at the first all-seen page), so the set has to be
    # pre-loaded and handed in. Load it HERE, on the main thread, before the
    # fan-out — the workers must not race each other on the state files.
    cedars_seen = load_seen("cedars")

    tasks = _source_tasks(cedars_seen)

    budget_s = config.fetch_budget_s()
    settled, abandoned = run_with_budget(tasks, budget_s, thread_name_prefix="job-sift-fetch")

    for name, future in settled:
        try:
            got = future.result()
            log.info("%s: %d listings", name, len(got))
            listings.extend(got)
            # Returning at all is the success signal. An adapter that could not
            # look now raises (SourceAuthError / SourceFetchError), so an empty
            # list here honestly means "I looked, there was nothing".
            succeeded.append(name)
        except SourceNotConfiguredError as exc:
            # NEITHER list. A source with no config was never asked anything,
            # so this run is no evidence about it — scoring it a success would
            # reset a real failure streak and stamp a `last_success` we never
            # observed (reproduced: with companies.yaml removed, a seeded
            # 12-run streak went to 0). Absent from both `succeeded` and
            # `errors`, update_health PRUNES it, the same as a source that is
            # commented out of the fetch list. Not a digest health line either:
            # nothing failed.
            log.warning("%s not configured — skipped, health record pruned: %s", name, exc.message)
        except SourceAuthError as exc:
            log.error("%s auth failure: %s", name, exc.message)
            errors[name] = exc.message
        except Exception as exc:
            log.error("%s fetch failed: %s", name, exc)
            errors[name] = f"fetch failed: {exc}"

    for name in abandoned:
        log.error("%s fetch failed: exceeded the %.0fs fetch budget", name, budget_s)
        errors[name] = f"fetch failed: exceeded the {budget_s:.0f}s fetch budget"

    return listings, errors, succeeded


def _classify_one(listing: JobListing) -> ClassifierResult:
    """Route a listing to the right classifier based on its source."""
    if listing.source in _AUTO_PRESTIGE_SOURCES:
        return classify_scope_only(listing)
    return classify(listing)


def _probe_for(dedup_key: str):
    """A zero-arg probe for one register row, for `run_with_budget`.

    A named factory rather than an inline lambda: a lambda in the comprehension
    would close over the loop variable and every task would probe the last row.
    """
    job_id = dedup_key.split(":", 1)[-1]
    return lambda: liveness.probe_linkedin(job_id)


def _liveness_pass(roles: list[OpenRole], today: date) -> list[OpenRole]:
    """Re-check a bounded slice of the undated LinkedIn rows. Issue #1c.

    Wrapped whole in a try/except for the same reason every source adapter is:
    an ageing convenience must never be able to take down a run that has already
    fetched, classified and is about to push. A failure here leaves the register
    exactly as it was.

    Per-row failures are handled a level down — `liveness.probe_linkedin` never
    raises and returns UNKNOWN for anything it could not read, and
    `apply_liveness` treats UNKNOWN as "no change at all". So a total LinkedIn
    outage costs a few timeouts and changes nothing, which is the correct
    outcome: not being able to ask is not an answer.
    """
    limit = config.liveness_max_per_run()
    if limit <= 0:
        return roles
    try:
        due = roles_due_liveness_check(
            roles, today, interval_days=config.liveness_interval_days(), limit=limit
        )
        if not due:
            return roles

        # Bounded by a wall-clock budget, via the same machinery the fetch phase
        # uses and for the same reason: an httpx timeout is per socket
        # OPERATION, so it bounds neither a redirect chain nor a slow-drip body,
        # and a serial loop with no ceiling is exactly the shape that got a run
        # SIGTERM'd before it could push (see concurrency.py's header). An
        # abandoned probe simply never lands in `verdicts`, which `apply_liveness`
        # reads as "not checked" — the same no-op as UNKNOWN.
        budget_s = config.liveness_budget_s()
        tasks = [
            (role.dedup_key, _probe_for(role.dedup_key)) for role in due
        ]
        # THROTTLED, unlike the fetch phase. Every task here hits the SAME
        # host, so starting all ten at once is ten concurrent GETs at
        # linkedin.com from one IP. That fails safe (429 → UNKNOWN → nothing
        # retired), but a rate-limited run and a run where every role is still
        # open then produce the identical register, which is the ambiguity this
        # codebase exists to delete. Two in flight keeps the answers real, and
        # the wall-clock budget still bounds the whole pass either way.
        settled, abandoned = run_with_budget(
            tasks,
            budget_s,
            thread_name_prefix="job-sift-liveness",
            max_in_flight=_LIVENESS_MAX_IN_FLIGHT,
        )
        verdicts: dict[str, str] = {}
        for key, future in settled:
            try:
                verdicts[key] = future.result()
            except Exception as exc:  # noqa: BLE001 — one bad probe is not a failed pass
                log.info("liveness: %s could not be checked (%s)", key, exc)
        checked = apply_liveness(roles, verdicts, today)
        closed = sum(1 for v in verdicts.values() if v == liveness.CLOSED)
        unknown = len(due) - sum(1 for v in verdicts.values() if v != liveness.UNKNOWN)
        log.info(
            "liveness: checked %d role(s) — %d closed, %d could not be checked "
            "(%d abandoned at the %.0fs budget)",
            len(due), closed, unknown, len(abandoned), budget_s,
        )
        return checked
    except Exception as exc:  # noqa: BLE001 — never let this kill a run
        log.warning("liveness pass failed, register left untouched: %s", exc)
        return roles


def _update_open_roles(
    surfaced: list[tuple[JobListing, ClassifierResult]],
    today: date,
    *,
    dry_run: bool,
) -> list[OpenRole]:
    """Fold this run's surfaced roles into the rolling register.

    Runs even on a zero-surfaced day: ageing and pruning are time-driven, so the
    register would go stale if we only touched it when something new landed.

    Under --dry-run nothing is persisted — the operator will dry-run this against a
    21-day scrape backlog before letting it commit, so the deltas are logged
    instead of written.
    """
    stored = load_open_roles()
    overrides = parse_status_overrides(read_open_roles_note())
    if overrides:
        log.info("applying %d hand-edited status override(s) from the note", len(overrides))
    existing = apply_status_overrides(stored, overrides)
    known_keys = {r.dedup_key for r in existing}

    # The lane travels with the role so the register can keep the two headings
    # apart on days a listing's source does not re-list it.
    merged = upsert_roles(existing, [(l, r.reason, r.lane) for l, r in surfaced], today)
    # Fold rows that are the same posting under two ids. Runs BEFORE ageing so
    # the ager judges the merged `last_seen` and deadline, not one half of a
    # split record. See open_roles.collapse_register — this is the half of #1b
    # that catches duplicates which arrived on different days, and no fetch-time
    # collapse could ever have seen them together.
    collapsed_count = len(merged)
    merged = collapse_register(merged)
    collapsed_count -= len(merged)
    # Then retire anything LinkedIn has already closed (#1c). Also before
    # ageing, so a row confirmed closed today is `expired` in this run's note
    # rather than next run's. Skipped on a dry run and under --stub: it is the
    # one part of the register update that reaches the network, and neither mode
    # is allowed to.
    if not dry_run and os.environ.get("JOB_SIFT_STUB") != "1":
        merged = _liveness_pass(merged, today)
    aged = age_roles(merged, today)
    kept = prune(aged, today)

    if collapsed_count:
        log.info("open-roles register: collapsed %d duplicate row(s)", collapsed_count)
    added = sum(1 for r in merged if r.dedup_key not in known_keys)
    updated = len(surfaced) - added
    expired = sum(1 for r in kept if r.status in ("expired", "stale"))
    active = active_roles(kept)
    log.info(
        "open-roles register: %d new, %d updated, %d open (%d prestige / %d floor), "
        "%d expired/stale, %d closing this week",
        added,
        updated,
        len(active),
        len(in_lane(active, "prestige")),
        len(in_lane(active, "floor")),
        expired,
        len(closing_within(kept, today)),
    )

    if dry_run:
        log.info("dry-run — NOT writing open_roles.json or the Open Roles note")
        return kept

    save_open_roles(kept)
    write_open_roles(render_open_roles(kept, today))
    return kept


def run(*, dry_run: bool = False, stub: bool = False) -> int:
    _setup_logging()
    today = date.today()
    log.info("job-sift starting for %s (dry_run=%s, stub=%s)", today.isoformat(), dry_run, stub)

    if stub:
        os.environ["JOB_SIFT_STUB"] = "1"

    if not stub and not dry_run:
        config.assert_required()

    # 1. Fetch raw listings from all sources
    listings, source_errors, fetched_ok = _fetch_all_sources()
    if source_errors:
        log.warning("source health: %d source(s) did not run: %s", len(source_errors), ", ".join(sorted(source_errors)))

    # 1b. Roll the per-source consecutive-failure counters forward and decide
    #     whether anything has been dead long enough to escalate. Computed
    #     BEFORE either render path so the alarm rides on the empty digest too
    #     — that is the run where "none today" is most likely to be believed.
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
    # NOT under --stub. `--stub` sets JOB_SIFT_STUB=1, which makes
    # sources/cedars.py return canned listings and never fail — so cedars
    # leaves the error map and RESETS to zero. Writing that zero would let a
    # debug run on the live box wipe the evidence of the exact outage this
    # alarm exists to catch (verified: a 49-run streak went to 0 after one
    # `run(dry_run=False, stub=True)`). A stub run is not evidence about the
    # real world, so it does not get to write the record of it.
    if not dry_run and not stub:
        source_health.save_health(health)

    if not listings:
        log.warning("no listings fetched from any source — pushing heartbeat")
        # Ageing is time-driven, so the register still needs a pass on a dead day.
        roles = _update_open_roles([], today, dry_run=dry_run)
        if not dry_run:
            push_messages(render(surfaced=[], skipped=[], total_new=0, total_processed=0, today=today, source_errors=source_errors, open_roles=roles, staleness_alarm=staleness_alarm, drop_notice=drop_notice))
            write_archive(today, render_vault_archive(surfaced=[], skipped=[], today=today, source_errors=source_errors, staleness_alarm=staleness_alarm, drop_notice=drop_notice))
        return 0

    log.info("fetched %d listings across all sources", len(listings))

    # 2a. Collapse listings that are the same posting under two ids, BEFORE the
    #     seen-set diff — a repost carries a NEW id, so the seen-set cannot see
    #     past it, and running this afterwards would only ever inspect the rows
    #     that were new today. See dedupe.collapse_duplicates for why the key is
    #     source-scoped and refuses to merge across sources.
    # `seen_lookup=load_seen` is passed explicitly rather than left to the
    # default so it resolves through THIS module's name — the same hook the
    # cedars pagination already uses, and the one tests patch to keep off the
    # real state files.
    listings, collapsed = collapse_duplicates(listings, seen_lookup=load_seen)
    if collapsed:
        log.info("collapsed %d duplicate listing(s) before the seen-set diff", len(collapsed))

    # 2b. Diff against seen-sets (per-source)
    new_listings, seen_by_source = filter_new(listings)
    log.info("%d new listings (after dedupe)", len(new_listings))

    # 2c. Record the dropped ids against the winner's sighting, so the next run
    #     that hands over from one id to the other does not re-notify. Additive
    #     only — it never re-keys existing state.
    mirror_collapsed(seen_by_source, collapsed, seen_lookup=load_seen)

    # 3. Classify all new listings in one batched pass (≤1 LLM call per ~20
    #    listings per route) — see classifier.classify_batch. The old per-listing
    #    loop spawned one `claude` CLI each, which blew the 600s service timeout
    #    once the backlog grew, killing the run before push/state-save.
    surfaced: list[tuple[JobListing, ClassifierResult]] = []
    skipped: list[tuple[JobListing, ClassifierResult]] = []
    # Listings the classifier never actually judged — a `None` from
    # classify_batch. They are NOT skipped: skipped means "looked at and
    # rejected", and scoring an outage as a rejection is the fifty-day CEDARS
    # bug wearing a different hat. They are surfaced nowhere, logged nowhere as
    # a verdict, held out of the seen-set, and counted into the ⚠️ line below.
    unclassified: list[JobListing] = []
    results = classify_batch(new_listings)
    for listing, result in zip(new_listings, results):
        if result is None:
            unclassified.append(listing)
            log.error(
                "[%s] %s — %s: NO VERDICT (classifier unavailable) — held for the next run",
                listing.source,
                listing.employer[:30],
                listing.title[:40],
            )
            continue
        log_classification(listing, result)
        if result.surface:
            surfaced.append((listing, result))
        else:
            skipped.append((listing, result))
        log.info(
            "[%s] %s — %s: prestige=%s scope=%s",
            listing.source,
            listing.employer[:30],
            listing.title[:40],
            result.prestige,
            result.scope,
        )

    log.info(
        "%d surfaced, %d skipped, %d unclassified", len(surfaced), len(skipped), len(unclassified)
    )

    # The classifier is a stage that can FAIL, and until now only the fetch
    # stage could say so. Reuse the channel that already renders a ⚠️ bubble in
    # both the digest and the archive rather than inventing a second one — the
    # reader's question is identical ("what is missing from what I am reading?")
    # and one banner mechanism is easier to trust than two.
    #
    # Injected HERE, after `source_health.update_health` has already run on the
    # fetch-phase map above. That ordering is load-bearing: the health counters
    # track SOURCES, and letting a pseudo-source called "classifier" into them
    # would invent a staleness record for something that is not a source. If
    # health computation is ever moved below this point, this has to move with
    # it or be given its own map.
    if unclassified:
        source_errors = dict(source_errors)
        source_errors["classifier"] = (
            f"classification unavailable for {len(unclassified)} listing(s)"
            " — held, and retried on the next run"
        )
        log.error(
            "classifier produced no verdict for %d of %d new listing(s)",
            len(unclassified), len(new_listings),
        )

    # 4. Roll the persistent register forward BEFORE rendering — the digest
    #    reports standing state ("11 open, 2 closing"), not just today's delta.
    open_roles = _update_open_roles(surfaced, today, dry_run=dry_run)

    # 5. Push to Telegram
    messages = render(
        surfaced=surfaced,
        skipped=skipped,
        total_new=len(new_listings),
        total_processed=len(listings),
        today=today,
        source_errors=source_errors,
        open_roles=open_roles,
        staleness_alarm=staleness_alarm,
        drop_notice=drop_notice,
    )

    if dry_run:
        log.info("dry-run — would push %d messages:", len(messages))
        for m in messages:
            print(m)
            print("---")
    else:
        push_messages(messages)
        log.info("pushed %d messages", len(messages))

        # 6. Persist the seen-set IMMEDIATELY after a successful push, and
        #    BEFORE the archive write. These listings have now been delivered;
        #    anything that fails from here on must not be able to undeliver
        #    them. With the archive write in between, an OSError there exited
        #    non-zero after the push but before this line, so a re-run
        #    reclassified and re-pushed the same listings. Nothing retries
        #    automatically today (the units carry no Restart= — see
        #    systemd/job-sift.service), but a manual re-run has the same shape,
        #    and this ordering is what makes any future retry safe.
        # Withhold BEFORE the commit, never after: this is the only moment the
        # in-memory set and the on-disk set can still be made to disagree in the
        # safe direction. An id that was never judged must not be recorded as
        # delivered — see dedupe.withhold_unclassified.
        withheld = withhold_unclassified(seen_by_source, unclassified, collapsed)
        if withheld:
            log.warning(
                "withheld %d id(s) from the seen-set — no classifier verdict this run",
                withheld,
            )
        for source, seen in seen_by_source.items():
            save_seen(source, seen)

    # 7. Vault archive (audit trail — after delivery is committed)
    archive_md = render_vault_archive(surfaced=surfaced, skipped=skipped, today=today, source_errors=source_errors, staleness_alarm=staleness_alarm, drop_notice=drop_notice)
    if not dry_run:
        write_archive(today, archive_md)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="job-sift daily orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="don't push to Telegram or persist state")
    parser.add_argument("--stub", action="store_true", help="use stub source data (skip scraping)")
    args = parser.parse_args()
    return run(dry_run=args.dry_run, stub=args.stub)


if __name__ == "__main__":
    sys.exit(main())
