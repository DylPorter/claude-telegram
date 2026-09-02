"""Diff incoming listings against a persisted seen-set.

Persists `seen_ids` per source as JSON files under .data/state/. Two purposes:
1. Only surface NEW listings each daily run (suppress noise).
2. Build the long-term log used to derive a prestige whitelist in ~30 days.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from job_sift.config import STATE_DIR
from job_sift.schema import JobListing

log = logging.getLogger(__name__)


def _seen_path(source: str) -> Path:
    return STATE_DIR / f"seen_{source}.json"


def _log_path() -> Path:
    return STATE_DIR / "classifier_log.jsonl"


def load_seen(source: str) -> set[str]:
    p = _seen_path(source)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except Exception as exc:
        log.warning("failed to load seen-set for %s: %s — starting fresh", source, exc)
        return set()


def save_seen(source: str, seen: set[str]) -> None:
    _seen_path(source).write_text(json.dumps(sorted(seen), indent=2))


def _has_deadline(listing: JobListing) -> bool:
    return listing.deadline is not None


def collapse_duplicates(
    listings: list[JobListing], *, seen_lookup=load_seen
) -> tuple[list[JobListing], list[tuple[JobListing, JobListing]]]:
    """Collapse listings that are the same posting, BEFORE the seen-set diff.

    Returns `(kept, collapsed)`, where each `collapsed` pair is `(kept, dropped)`
    so the caller can mirror and log what it merged.

    ORDER MATTERS. This has to run ahead of `filter_new`, not after it. Run
    afterwards it would only ever see the rows that were new *today*, so the
    common case — a repost arriving while the original is already in the
    seen-set — would sail straight past it, and the duplicate would already have
    cost a classifier call and a register row by the time anything looked.

    The collision key is `JobListing.identity_key`, which is source-scoped;
    read its docstring before widening anything here. Two sources reporting one
    posting are deliberately NOT merged.

    CONTINUITY BEATS FRESHNESS when picking the winner, which is the subtle
    half. Choosing, say, the newest id would trade a same-run double-report for
    an across-run one: the original is already recorded in the seen-set, so
    promoting a different id makes `filter_new` miss, and the posting is
    notified a second time under its new id. So a candidate the seen-set already
    knows wins. Freshness only decides a genuinely first sighting, where no
    seen-set has an opinion and either choice is equally new; there, a listing
    carrying a deadline beats one without (the register can actually age it),
    and ties fall back to fetch order so the result is deterministic.

    `seen_lookup` is injected so tests can drive this without touching
    `.data/state/`.
    """
    by_identity: dict[str, list[JobListing]] = {}
    order: list[str] = []
    for listing in listings:
        key = listing.identity_key
        if key not in by_identity:
            by_identity[key] = []
            order.append(key)
        by_identity[key].append(listing)

    seen_cache: dict[str, set[str]] = {}

    def _already_seen(listing: JobListing) -> bool:
        if listing.source not in seen_cache:
            seen_cache[listing.source] = seen_lookup(listing.source)
        return listing.external_id in seen_cache[listing.source]

    kept: list[JobListing] = []
    collapsed: list[tuple[JobListing, JobListing]] = []
    for key in order:
        group = by_identity[key]
        if len(group) == 1:
            kept.append(group[0])
            continue
        # sorted() is stable, so a full tie preserves fetch order.
        winner = sorted(
            group, key=lambda l: (not _already_seen(l), not _has_deadline(l))
        )[0]
        kept.append(winner)
        for other in group:
            if other is not winner:
                collapsed.append((winner, other))
                log.info(
                    "collapsed duplicate listing: keeping %s, dropping %s (%s — %s)",
                    winner.dedup_key,
                    other.dedup_key,
                    winner.employer[:40],
                    winner.title[:60],
                )
    return kept, collapsed


def mirror_collapsed(
    seen_by_source: dict[str, set[str]],
    collapsed: list[tuple[JobListing, JobListing]],
    *,
    seen_lookup=load_seen,
) -> None:
    """Record the winner's sighting against every LOSER's id too.

    THE BUG THIS FIXES. `collapse_duplicates` lets only one row through, and
    `filter_new` populates the seen-set from the rows it actually iterates — so
    the dropped id is never written down. That is stable only while the source
    keeps listing both. It does not:

        run 1  original only          → notified, id A recorded
        run 2  original + repost      → A wins (continuity), B still unrecorded
        run 3  original ages off the  → B wins by default, is not in the
               alert, repost remains     seen-set, and RE-NOTIFIES

    Run 3 re-pushes a role the operator already saw and read.

    WHY MIRROR RATHER THAN RE-KEY THE SEEN-SET on `identity_key`. Re-keying is
    the tidier-looking fix and it is the wrong one: every id in the existing
    `seen_cedars.json` / `seen_linkedin.json` would stop matching, and the first
    run after deploy would re-push the entire backlog as newly discovered.
    Mirroring is purely additive — it only ever writes ids that were missing —
    so it needs no migration and cannot invalidate state that already exists.

    Call this AFTER `filter_new`, which is what puts the winner's sighting into
    `seen_by_source` in the first place — either because it was new today, or
    because `load_seen` brought it back off disk. If it is somehow not there,
    the winner is not actually known to be seen, and mirroring would mark the
    loser delivered on the strength of nothing. That case is skipped rather than
    guessed, and the loser is picked up by the next run that does surface the
    winner. Same reason a missing source bucket is left missing: `save_seen`
    truncates, so writing a one-element bucket over a full state file would
    delete the source's entire history.
    """
    for winner, loser in collapsed:
        bucket = seen_by_source.get(winner.source)
        if bucket is None or winner.external_id not in bucket:
            continue
        if loser.source not in seen_by_source:
            seen_by_source[loser.source] = seen_lookup(loser.source)
        if loser.external_id not in seen_by_source[loser.source]:
            seen_by_source[loser.source].add(loser.external_id)
            log.info(
                "mirrored sighting to %s from collapsed winner %s",
                loser.dedup_key,
                winner.dedup_key,
            )


def filter_new(listings: list[JobListing]) -> tuple[list[JobListing], dict[str, set[str]]]:
    """Return (only-new listings, per-source-updated-seen-sets).

    Caller is expected to persist seen sets AFTER classification + push so that
    a failed push doesn't permanently mark listings as seen.
    """
    seen_by_source: dict[str, set[str]] = {}
    new_listings: list[JobListing] = []
    for listing in listings:
        seen = seen_by_source.setdefault(listing.source, load_seen(listing.source))
        if listing.external_id in seen:
            continue
        new_listings.append(listing)
        seen.add(listing.external_id)
    return new_listings, seen_by_source


def log_classification(listing: JobListing, result) -> None:
    """Append one classification decision to the rolling JSONL log.

    Used after ~30 days to derive a prestige whitelist organically (see
    feedback_internship_strategy memory).
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": listing.source,
        "external_id": listing.external_id,
        "employer": listing.employer,
        "title": listing.title,
        "prestige": result.prestige,
        "scope": result.scope,
        "reason": result.reason,
    }
    with _log_path().open("a") as f:
        f.write(json.dumps(entry) + "\n")
