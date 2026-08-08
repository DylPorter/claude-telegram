"""Daily job-sift orchestrator. Wires sources → dedupe → classifier → push."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from job_sift import config
from job_sift.classifier import classify, classify_batch, classify_scope_only
from job_sift.dedupe import filter_new, load_seen, log_classification, save_seen
from job_sift.errors import SourceAuthError
from job_sift.open_roles import (
    OpenRole,
    active_roles,
    age_roles,
    apply_status_overrides,
    closing_within,
    load_open_roles,
    parse_status_overrides,
    prune,
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


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("JOB_SIFT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )


def _fetch_all_sources() -> tuple[list[JobListing], dict[str, str]]:
    """Run every source adapter, return (combined listings, per-source errors).

    Individual failures are caught so one dead source never kills the run — but
    they are recorded in the returned error map (keyed by source name) so the
    digest + archive can surface a ⚠️ health line. An auth failure raising
    SourceAuthError is the load-bearing case: it turns a silent "None today"
    into a visible "this source did not run".
    """
    listings: list[JobListing] = []
    errors: dict[str, str] = {}

    def _run(name: str, fetch_fn) -> None:
        try:
            listings.extend(fetch_fn())
        except SourceAuthError as exc:
            log.error("%s auth failure: %s", name, exc.message)
            errors[name] = exc.message
        except Exception as exc:
            log.error("%s fetch failed: %s", name, exc)
            errors[name] = f"fetch failed: {exc}"

    # CEDARS — uses seen-set for greedy pagination, so pre-load it
    cedars_seen = load_seen("cedars")
    _run("cedars", lambda: cedars.fetch_cedars_listings(seen_ids=cedars_seen))

    # Standardized-ATS sources — public JSON APIs, no pagination needed
    _run("greenhouse", greenhouse.fetch_greenhouse_listings)
    _run("lever", lever.fetch_lever_listings)
    _run("ashby", ashby.fetch_ashby_listings)

    # LinkedIn — gws CLI Gmail digest email parsing
    _run("linkedin", linkedin.fetch_linkedin_listings)

    return listings, errors


def _classify_one(listing: JobListing) -> ClassifierResult:
    """Route a listing to the right classifier based on its source."""
    if listing.source in _AUTO_PRESTIGE_SOURCES:
        return classify_scope_only(listing)
    return classify(listing)


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

    merged = upsert_roles(existing, [(l, r.reason) for l, r in surfaced], today)
    aged = age_roles(merged, today)
    kept = prune(aged, today)

    added = sum(1 for r in merged if r.dedup_key not in known_keys)
    updated = len(surfaced) - added
    expired = sum(1 for r in kept if r.status in ("expired", "stale"))
    log.info(
        "open-roles register: %d new, %d updated, %d open, %d expired/stale, %d closing this week",
        added,
        updated,
        len(active_roles(kept)),
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
    listings, source_errors = _fetch_all_sources()
    if source_errors:
        log.warning("source health: %d source(s) did not run: %s", len(source_errors), ", ".join(sorted(source_errors)))
    if not listings:
        log.warning("no listings fetched from any source — pushing heartbeat")
        # Ageing is time-driven, so the register still needs a pass on a dead day.
        roles = _update_open_roles([], today, dry_run=dry_run)
        if not dry_run:
            push_messages(render(surfaced=[], skipped=[], total_new=0, total_processed=0, today=today, source_errors=source_errors, open_roles=roles))
            write_archive(today, render_vault_archive(surfaced=[], skipped=[], today=today, source_errors=source_errors))
        return 0

    log.info("fetched %d listings across all sources", len(listings))

    # 2. Diff against seen-sets (per-source)
    new_listings, seen_by_source = filter_new(listings)
    log.info("%d new listings (after dedupe)", len(new_listings))

    # 3. Classify all new listings in one batched pass (≤1 LLM call per ~20
    #    listings per route) — see classifier.classify_batch. The old per-listing
    #    loop spawned one `claude` CLI each, which blew the 600s service timeout
    #    once the backlog grew, killing the run before push/state-save.
    surfaced: list[tuple[JobListing, ClassifierResult]] = []
    skipped: list[tuple[JobListing, ClassifierResult]] = []
    results = classify_batch(new_listings)
    for listing, result in zip(new_listings, results):
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

    log.info("%d surfaced, %d skipped", len(surfaced), len(skipped))

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
    )

    if dry_run:
        log.info("dry-run — would push %d messages:", len(messages))
        for m in messages:
            print(m)
            print("---")
    else:
        push_messages(messages)
        log.info("pushed %d messages", len(messages))

    # 6. Vault archive
    archive_md = render_vault_archive(surfaced=surfaced, skipped=skipped, today=today, source_errors=source_errors)
    if not dry_run:
        write_archive(today, archive_md)

    # 7. Persist seen-set (only after successful push)
    if not dry_run:
        for source, seen in seen_by_source.items():
            save_seen(source, seen)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="job-sift daily orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="don't push to Telegram or persist state")
    parser.add_argument("--stub", action="store_true", help="use stub source data (skip scraping)")
    args = parser.parse_args()
    return run(dry_run=args.dry_run, stub=args.stub)


if __name__ == "__main__":
    sys.exit(main())
