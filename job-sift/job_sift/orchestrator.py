"""Daily job-sift orchestrator. Wires sources → dedupe → classifier → push."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from job_sift import config
from job_sift.classifier import classify, classify_scope_only
from job_sift.dedupe import filter_new, load_seen, log_classification, save_seen
from job_sift.render import render, render_vault_archive
from job_sift.schema import ClassifierResult, JobListing
from job_sift.sources import ashby, cedars, greenhouse, lever, linkedin
from job_sift.telegram_client import push_messages
from job_sift.vault_note import write_archive

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


def _fetch_all_sources() -> list[JobListing]:
    """Run every source adapter, swallow individual failures, return combined listings."""
    listings: list[JobListing] = []

    # CEDARS — uses seen-set for greedy pagination, so pre-load it
    cedars_seen = load_seen("cedars")
    try:
        listings.extend(cedars.fetch_cedars_listings(seen_ids=cedars_seen))
    except Exception as exc:
        log.error("cedars fetch failed: %s", exc)

    # Standardized-ATS sources — public JSON APIs, no pagination needed
    for fetch_fn, name in [
        (greenhouse.fetch_greenhouse_listings, "greenhouse"),
        (lever.fetch_lever_listings, "lever"),
        (ashby.fetch_ashby_listings, "ashby"),
    ]:
        try:
            listings.extend(fetch_fn())
        except Exception as exc:
            log.error("%s fetch failed: %s", name, exc)

    # LinkedIn — gws CLI Gmail digest email parsing
    try:
        listings.extend(linkedin.fetch_linkedin_listings())
    except Exception as exc:
        log.error("linkedin fetch failed: %s", exc)

    return listings


def _classify_one(listing: JobListing) -> ClassifierResult:
    """Route a listing to the right classifier based on its source."""
    if listing.source in _AUTO_PRESTIGE_SOURCES:
        return classify_scope_only(listing)
    return classify(listing)


def run(*, dry_run: bool = False, stub: bool = False) -> int:
    _setup_logging()
    today = date.today()
    log.info("job-sift starting for %s (dry_run=%s, stub=%s)", today.isoformat(), dry_run, stub)

    if stub:
        os.environ["JOB_SIFT_STUB"] = "1"

    if not stub and not dry_run:
        config.assert_required()

    # 1. Fetch raw listings from all sources
    listings = _fetch_all_sources()
    if not listings:
        log.warning("no listings fetched from any source — pushing heartbeat")
        if not dry_run:
            push_messages(render(surfaced=[], skipped=[], total_new=0, total_processed=0, today=today))
        return 0

    log.info("fetched %d listings across all sources", len(listings))

    # 2. Diff against seen-sets (per-source)
    new_listings, seen_by_source = filter_new(listings)
    log.info("%d new listings (after dedupe)", len(new_listings))

    # 3. Classify each new listing (per-source strategy)
    surfaced: list[tuple[JobListing, ClassifierResult]] = []
    skipped: list[tuple[JobListing, ClassifierResult]] = []
    for listing in new_listings:
        result = _classify_one(listing)
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

    # 4. Push to Telegram
    messages = render(
        surfaced=surfaced,
        skipped=skipped,
        total_new=len(new_listings),
        total_processed=len(listings),
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

    # 5. Vault archive
    archive_md = render_vault_archive(surfaced=surfaced, skipped=skipped, today=today)
    if not dry_run:
        write_archive(today, archive_md)

    # 6. Persist seen-set (only after successful push)
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
