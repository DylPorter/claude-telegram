"""Format classified listings into Telegram-friendly chunked messages.

Per Dylan's delivery preferences (feedback_telegram_delivery_format):
short, chunked into multiple sub-messages — concision is the deliverable.
"""

from __future__ import annotations

from datetime import date

from job_sift.schema import ClassifierResult, JobListing


def _fmt_listing(listing: JobListing) -> str:
    parts = [f"**{listing.employer}** — {listing.title}"]
    if listing.location:
        parts.append(f"📍 {listing.location}")
    if listing.deadline:
        parts.append(f"⏰ deadline {listing.deadline.isoformat()}")
    parts.append(f"[apply]({listing.apply_url})")
    return "\n".join(parts)


def _fmt_near_misses(skipped: list[tuple[JobListing, ClassifierResult]]) -> str | None:
    """Format a 'near miss' digest for prestige companies that failed scope.

    Only shows prestige=prestige entries — these are companies Dylan cares about
    but that had the wrong role type (FT, non-HK, senior, etc.).
    """
    near_misses = [
        (listing, result)
        for listing, result in skipped
        if result.prestige == "prestige"
    ]
    if not near_misses:
        return None

    # Group by employer, collect (title, reason) pairs
    by_employer: dict[str, list[str]] = {}
    for listing, result in near_misses:
        employer = listing.employer
        short_reason = result.reason or result.scope
        entry = f"{listing.title} ({short_reason})"
        by_employer.setdefault(employer, []).append(entry)

    lines = ["📊 *Near misses — prestige but filtered:*"]
    for employer, roles in sorted(by_employer.items()):
        count = len(roles)
        role_str = "; ".join(roles[:2])
        if count > 2:
            role_str += f" +{count - 2} more"
        lines.append(f"• **{employer}** ({count}) — {role_str}")
    return "\n".join(lines)


def render(
    *,
    surfaced: list[tuple[JobListing, ClassifierResult]],
    skipped: list[tuple[JobListing, ClassifierResult]],
    total_new: int,
    total_processed: int,
    today: date,
) -> list[str]:
    """Build the chunked message list for /push.

    Each listing gets its own bubble. A header chip leads, a footer chip closes
    with stats. If nothing surfaced, returns a single quiet "no matches" bubble
    so the daily heartbeat is visible.
    """
    if not surfaced and not any(r.prestige == "prestige" for _, r in skipped):
        return [
            f"📋 *Job sift — {today.isoformat()}*\n"
            f"No new prestige matches today. "
            f"Processed {total_processed} listings, {total_new} new."
        ]

    messages: list[str] = []
    messages.append(
        f"📋 *Job sift — {today.isoformat()}*\n"
        f"{len(surfaced)} new prestige match"
        f"{'es' if len(surfaced) != 1 else ''} ↓"
    )
    for listing, _ in surfaced:
        messages.append(_fmt_listing(listing))

    near_miss_bubble = _fmt_near_misses(skipped)
    if near_miss_bubble:
        messages.append(near_miss_bubble)

    messages.append(
        f"_Processed {total_processed} listings, {total_new} new, "
        f"{len(surfaced)} surfaced._"
    )
    return messages


def render_vault_archive(
    *,
    surfaced: list[tuple[JobListing, ClassifierResult]],
    skipped: list[tuple[JobListing, ClassifierResult]],
    today: date,
) -> str:
    """Render the per-day Markdown archive that lands in the vault."""
    lines = [
        "---",
        f"date: {today.isoformat()}",
        "type: job-sift",
        "tags: [job-sift, automation]",
        "---",
        "",
        f"# Job Sift — {today.isoformat()}",
        "",
    ]

    if surfaced:
        lines.append("## Surfaced (prestige + in-scope)")
        lines.append("")
        for listing, result in surfaced:
            lines.append(f"- **{listing.employer}** — {listing.title}")
            lines.append(f"  - Apply: {listing.apply_url}")
            if listing.deadline:
                lines.append(f"  - Deadline: {listing.deadline.isoformat()}")
            lines.append(f"  - Verdict: prestige={result.prestige}, scope={result.scope}")
            lines.append(f"  - Reason: {result.reason}")
            lines.append("")
    else:
        lines.append("## Surfaced")
        lines.append("")
        lines.append("_None today._")
        lines.append("")

    if skipped:
        lines.append("## Filtered out")
        lines.append("")
        for listing, result in skipped:
            lines.append(
                f"- {listing.employer} — {listing.title} "
                f"({result.prestige} / {result.scope}) — {result.reason}"
            )
        lines.append("")

    return "\n".join(lines)
