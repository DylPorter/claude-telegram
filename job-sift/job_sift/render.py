"""Format classified listings into Telegram-friendly chunked messages.

Per the operator's delivery preferences (feedback_telegram_delivery_format):
short, chunked into multiple sub-messages — concision is the deliverable.
"""

from __future__ import annotations

from datetime import date

from job_sift.open_roles import OpenRole, active_roles, closing_within, in_lane
from job_sift.profile import floor_lane_config
from job_sift.schema import ClassifierResult, JobListing


def _fmt_listing(listing: JobListing) -> str:
    parts = [f"**{listing.employer}** — {listing.title}"]
    if listing.location:
        parts.append(f"📍 {listing.location}")
    if listing.deadline:
        parts.append(f"⏰ deadline {listing.deadline.isoformat()}")
    parts.append(f"[apply]({listing.apply_url})")
    return "\n".join(parts)


# The two lanes are rendered under separate headings everywhere — digest,
# archive and register — and never merged into one list. That separation IS the
# feature: the floor lane is deliberately looser (any employer, as long as the
# work is technical, reachable and short-term), so folding its matches in with
# the prestige ones would spend the prestige lane's credibility on them. A
# reader must be able to tell at a glance which question a line answered.
#
# Nothing appears twice: `ClassifierResult.lane` and `OpenRole.lane` hold ONE
# value each, assigned by precedence in `classifier.assign_lane`, so these
# partitions are disjoint by construction rather than by the caller being
# careful.
_FLOOR_HEADER = "🧱 *Floor lane — technical contract / part-time, any employer:*"


def _split_lanes(
    items: list[tuple[JobListing, ClassifierResult]],
) -> tuple[list[tuple[JobListing, ClassifierResult]], list[tuple[JobListing, ClassifierResult]]]:
    """Partition surfaced listings into (prestige lane, floor lane)."""
    prestige = [pair for pair in items if pair[1].lane != "floor"]
    floor = [pair for pair in items if pair[1].lane == "floor"]
    return prestige, floor


def _fmt_source_health(source_errors: dict[str, str] | None) -> str | None:
    """Format a ⚠️ health bubble for sources that failed to run.

    Without this, an expired cookie/token makes a source return zero and the
    digest reads as a clean "no matches" — indistinguishable from a genuinely
    quiet day. Surfacing the failure turns a silent multi-week blind spot into
    a same-day re-auth.

    NOT ONLY SOURCES ANY MORE. The orchestrator also puts a `"classifier"` entry
    in this map when the LLM produced no verdict for some listings, because the
    reader's question is the same one ("what is missing from what I am
    reading?") and one banner mechanism is easier to trust than two. The key
    names whatever did not run; it is not required to be a fetch source.
    """
    if not source_errors:
        return None
    lines = ["⚠️ *Source health — did NOT run (results missing):*"]
    for src, msg in sorted(source_errors.items()):
        first = msg.split(" — ")[0].split(". ")[0]
        lines.append(f"• *{src}* — {first}")
    return "\n".join(lines)


def _fmt_near_misses(skipped: list[tuple[JobListing, ClassifierResult]]) -> str | None:
    """Format a 'near miss' digest for prestige companies that failed scope.

    Only shows prestige=prestige entries — these are companies the operator cares about
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


def _fmt_rolling_state(
    open_roles: list[OpenRole] | None, surfaced_count: int, today: date
) -> str | None:
    """One-line rolling-state chip: what's live right now, not just today's delta.

    The whole point of the register is that a role the operator didn't act on yesterday
    is still his problem today, so the digest has to report the standing total
    and the ones about to close — not only the new arrivals.
    """
    if open_roles is None:
        return None
    open_count = len(active_roles(open_roles))
    closing = len(closing_within(open_roles, today))
    return (
        f"📋 {surfaced_count} new · {open_count} open · {closing} closing this week"
    )


def _prepend_alarm(
    messages: list[str],
    staleness_alarm: str | None,
    drop_notice: str | None = None,
) -> list[str]:
    """Put the standing banners FIRST, ahead of the digest they qualify.

    Alarm before notice: "a source is dead" outranks "a source stopped being
    tracked". Both go above the digest for the same reason — a reader who stops
    after the first bubble must have seen it before they read "none today".
    """
    banners = [b for b in (staleness_alarm, drop_notice) if b]
    return banners + messages


def render(
    *,
    surfaced: list[tuple[JobListing, ClassifierResult]],
    skipped: list[tuple[JobListing, ClassifierResult]],
    total_new: int,
    total_processed: int,
    today: date,
    source_errors: dict[str, str] | None = None,
    open_roles: list[OpenRole] | None = None,
    staleness_alarm: str | None = None,
    drop_notice: str | None = None,
) -> list[str]:
    """Build the chunked message list for /push.

    Each listing gets its own bubble. A header chip leads, a footer chip closes
    with stats. If nothing surfaced, returns a single quiet "no matches" bubble
    so the daily heartbeat is visible. Any failed sources get a ⚠️ health
    bubble appended so a broken source is never mistaken for a quiet day, and a
    `staleness_alarm` (a source dead for 3+ consecutive runs) is PREPENDED so it
    leads the digest it invalidates.
    """
    health = _fmt_source_health(source_errors)
    rolling = _fmt_rolling_state(open_roles, len(surfaced), today)

    if not surfaced and not any(r.prestige == "prestige" for _, r in skipped):
        quiet = (
            f"📋 *Job sift — {today.isoformat()}*\n"
            f"No new prestige matches today. "
            f"Processed {total_processed} listings, {total_new} new."
        )
        out = [quiet]
        if rolling:
            out.append(rolling)
        if health:
            out.append(health)
        return _prepend_alarm(out, staleness_alarm, drop_notice)

    prestige_lane, floor_lane = _split_lanes(surfaced)

    messages: list[str] = []
    messages.append(
        f"📋 *Job sift — {today.isoformat()}*\n"
        f"{len(prestige_lane)} new prestige match"
        f"{'es' if len(prestige_lane) != 1 else ''}"
        + (f" · {len(floor_lane)} floor" if floor_lane else "")
        + " ↓"
    )
    for listing, _ in prestige_lane:
        messages.append(_fmt_listing(listing))

    if floor_lane:
        messages.append(_FLOOR_HEADER)
        for listing, _ in floor_lane:
            messages.append(_fmt_listing(listing))

    near_miss_bubble = _fmt_near_misses(skipped)
    if near_miss_bubble:
        messages.append(near_miss_bubble)

    if rolling:
        messages.append(rolling)

    if health:
        messages.append(health)

    messages.append(
        f"_Processed {total_processed} listings, {total_new} new, "
        f"{len(surfaced)} surfaced._"
    )
    return _prepend_alarm(messages, staleness_alarm, drop_notice)


def _archive_entries(items: list[tuple[JobListing, ClassifierResult]]) -> list[str]:
    """The per-listing block shared by both lane sections of the archive."""
    lines: list[str] = []
    for listing, result in items:
        lines.append(f"- **{listing.employer}** — {listing.title}")
        lines.append(f"  - Apply: {listing.apply_url}")
        if listing.deadline:
            lines.append(f"  - Deadline: {listing.deadline.isoformat()}")
        lines.append(
            f"  - Verdict: lane={result.lane}, prestige={result.prestige}, scope={result.scope}"
        )
        lines.append(f"  - Reason: {result.reason}")
        lines.append("")
    return lines


def render_vault_archive(
    *,
    surfaced: list[tuple[JobListing, ClassifierResult]],
    skipped: list[tuple[JobListing, ClassifierResult]],
    today: date,
    source_errors: dict[str, str] | None = None,
    staleness_alarm: str | None = None,
    drop_notice: str | None = None,
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

    if staleness_alarm:
        lines.append("## 🚨 Stale source alarm")
        lines.append("")
        lines.append("_A source has failed on 3+ consecutive runs. Everything below is incomplete._")
        lines.append("")
        for line in staleness_alarm.splitlines():
            lines.append(f"> {line}")
        lines.append("")

    if drop_notice:
        lines.append("## ℹ️ Dropped from health tracking")
        lines.append("")
        lines.append(
            "_A source carrying a standing alarm was pruned this run because it "
            "had no config. Nothing failed — but nothing looked, either._"
        )
        lines.append("")
        for line in drop_notice.splitlines():
            lines.append(f"> {line}")
        lines.append("")

    if source_errors:
        lines.append("## ⚠️ Source health — these did NOT run")
        lines.append("")
        lines.append("_Listings from these sources are MISSING today. \"None\" below is not authoritative until fixed._")
        lines.append("")
        for src, msg in sorted(source_errors.items()):
            lines.append(f"- **{src}** — {msg}")
        lines.append("")

    prestige_lane, floor_lane = _split_lanes(surfaced)

    if prestige_lane:
        lines.append("## Surfaced — prestige lane (prestige + in-scope)")
        lines.append("")
        lines.extend(_archive_entries(prestige_lane))
    else:
        lines.append("## Surfaced — prestige lane")
        lines.append("")
        lines.append("_None today._")
        lines.append("")

    # Written even when empty, for the same reason the register keeps its empty
    # sections explicit: a missing heading reads as a rendering bug, an explicit
    # "none" reads as a fact about the day.
    lines.append("## Surfaced — floor lane (technical contract / part-time, any employer)")
    lines.append("")
    if floor_lane:
        lines.extend(_archive_entries(floor_lane))
    else:
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


def _fmt_open_role_entry(role: OpenRole, today: date) -> list[str]:
    """One register entry, plus the status marker the operator can hand-edit.

    The status marker is UNCHANGED by the lane split — it still holds exactly
    `status` and `dedup_key`, in the same `<!-- status:open cedars:123 -->`
    shape `parse_status_overrides` reads. That is deliberate: the lane is the
    classifier's opinion and may change between runs, the marker is the
    operator's decision and may not, so the lane is kept out of the key the
    decision is filed under. A role that moves lanes carries its `applied` mark
    with it.
    """
    lane_tag = " `[floor]`" if role.lane == "floor" else ""
    lines = [f"- **{role.employer}** — {role.title}{lane_tag}"]
    lines.append(f"  <!-- status:{role.status} {role.dedup_key} -->")
    if role.deadline:
        left = role.days_left(today)
        suffix = "" if left is None else f" ({left} days left)"
        lines.append(f"  - Deadline: {role.deadline}{suffix}")
    else:
        lines.append("  - Deadline: none listed")
    lines.append(f"  - Apply: {role.apply_url}")
    lines.append(f"  - First seen: {role.first_seen}")
    if role.reason:
        lines.append(f"  - Why: {role.reason}")
    return lines


def render_open_roles(roles: list[OpenRole], today: date) -> str:
    """Render the rolling register note (`Areas/Work/Open Roles.md`).

    Deadline-sorted with undated roles last, so the top of the note is always
    the thing closing soonest. Empty sections say "none" rather than vanishing —
    a missing heading reads as a rendering bug, an explicit "none" reads as fact.
    """
    closing = closing_within(roles, today)
    closing_keys = {r.dedup_key for r in closing}
    rest = [r for r in active_roles(roles) if r.dedup_key not in closing_keys]
    applied = sorted(
        (r for r in roles if r.status == "applied"),
        key=lambda r: (r.employer.lower(), r.title.lower()),
    )

    lines = [
        "---",
        "type: open-roles",
        f"updated: {today.isoformat()}",
        "tags: [job-sift, automation, career]",
        "---",
        "",
        "# Open Roles",
        "",
        "> [!info] Auto-generated by job-sift — rewritten every run.",
        "> Your `status:` edits ARE preserved. Each entry carries a hidden HTML "
        "comment marker holding its status and key; edit the status word in it "
        "to **applied** or **dismissed** and the next run keeps it — it will "
        "never flip back to open.",
        "",
        "## ⏰ Closing this week",
        "",
    ]

    if closing:
        for role in closing:
            lines.extend(_fmt_open_role_entry(role, today))
            lines.append("")
    else:
        lines.append("_Nothing closing in the next 7 days._")
        lines.append("")

    lines.append("## 📋 Open — prestige lane")
    lines.append("")
    prestige_rest = in_lane(rest, "prestige")
    if prestige_rest:
        for role in prestige_rest:
            lines.extend(_fmt_open_role_entry(role, today))
            lines.append("")
    else:
        lines.append("_No other open roles._")
        lines.append("")

    lines.append("## 🧱 Open — floor lane")
    lines.append("")
    # Geography comes from the profile, not from this string. Writing "Hong
    # Kong" here would put the operator's criteria in the renderer, which is
    # exactly the split `config/profile.yaml` exists to prevent.
    where = ", ".join(floor_lane_config().locations) or "any configured location"
    lines.append(
        f"_Technical, short-term, and in scope for: {where} — regardless of who "
        "is hiring. Deliberately a looser net than the prestige lane above._"
    )
    lines.append("")
    floor_rest = in_lane(rest, "floor")
    if floor_rest:
        for role in floor_rest:
            lines.extend(_fmt_open_role_entry(role, today))
            lines.append("")
    else:
        lines.append("_No open floor-lane roles._")
        lines.append("")

    lines.append("## ✅ Applied")
    lines.append("")
    if applied:
        for role in applied:
            deadline = role.deadline or "no deadline"
            lines.append(
                f"- **{role.employer}** — {role.title} "
                f"(first seen {role.first_seen}, deadline {deadline}) "
                f"<!-- status:applied {role.dedup_key} -->"
            )
        lines.append("")
    else:
        lines.append("_Nothing marked applied yet._")
        lines.append("")

    return "\n".join(lines)
