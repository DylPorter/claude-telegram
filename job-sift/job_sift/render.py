"""Format a run into Telegram messages, the vault archive and the register note.

TELEGRAM IS NOW A POINTER, NOT A DIGEST. It used to be one bubble per surfaced
listing — the operator's own summary was that "they're all in separate messages
which is kinda overwhelming". Since capture went broad the count only goes up,
and a per-listing digest of a deliberately-unfiltered capture is unreadable by
construction. So the run reports ONE bubble — how many are new, how many are
open, what is closing, and where the board is — and the board is where the
reading happens.

TWO THINGS ARE EXEMPT and still push on their own. The staleness alarm and the
⚠️ source-health line exist to be seen on a day when everything else is quiet;
folding them into the summary line would put "cedars has been dead for three
runs" in the same bubble the reader learns to skim. They lead, they are
separate, and they are not suppressed by anything.
"""

from __future__ import annotations

from datetime import date

from job_sift.open_roles import OpenRole, active_roles, closing_within, in_lane
from job_sift.profile import floor_lane_config
from job_sift.schema import ClassifierResult, JobListing


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
def _split_lanes(
    items: list[tuple[JobListing, ClassifierResult]],
) -> dict[str, list[tuple[JobListing, ClassifierResult]]]:
    """Partition surfaced listings by lane. Every item lands in exactly one.

    Three buckets now, not two — see `schema.Lane`. An unrecognised lane is
    filed under "broad" rather than dropped: the archive is an audit trail, and
    an entry silently missing from it is worse than one under a weak heading.
    """
    out: dict[str, list[tuple[JobListing, ClassifierResult]]] = {
        "prestige": [], "floor": [], "broad": [],
    }
    for pair in items:
        out.get(pair[1].lane, out["broad"]).append(pair)
    return out


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
    return _banners(staleness_alarm, drop_notice) + messages


def _banners(staleness_alarm: str | None, drop_notice: str | None) -> list[str]:
    """The standing banners, in the order they lead the digest."""
    return [b for b in (staleness_alarm, drop_notice) if b]


def summary_index(
    *,
    staleness_alarm: str | None = None,
    drop_notice: str | None = None,
) -> int:
    """Which entry of `render()`'s list is the summary bubble.

    Board attachment replaces the summary bubble with a document carrying the
    same text as its caption, so delivery has to know which entry that is. It is
    computed from the same `_banners` the prepend uses rather than re-derived,
    so it cannot drift from the ordering it describes — `render` puts the
    banners first and everything else (the summary, then source health) after.
    """
    return len(_banners(staleness_alarm, drop_notice))


def _closing_line(open_roles: list[OpenRole] | None, today: date) -> str:
    """The soonest deadlines, named. A bare count of "closing this week" is a
    number the reader cannot act on; two employers and their dates is."""
    if not open_roles:
        return ""
    closing = closing_within(open_roles, today)
    if not closing:
        return ""
    # Grouped by employer+date: two roles at one employer closing the same day
    # is one thing to know, printed twice.
    groups: dict[tuple[str, str | None], int] = {}
    for role in closing:
        if not role.employer:
            continue
        groups[(role.employer, role.deadline)] = groups.get((role.employer, role.deadline), 0) + 1
    items = [
        f"{employer} ({deadline})" + (f" ×{count}" if count > 1 else "")
        for (employer, deadline), count in list(groups.items())[:3]
    ]
    more = f" +{len(groups) - 3} more" if len(groups) > 3 else ""
    return f"\n⏰ Closing: {'; '.join(items)}{more}"


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
    board_path=None,
    board_problem: str | None = None,
    purged: int = 0,
) -> list[str]:
    """Build the message list for /push. ONE summary bubble, plus exemptions.

    The shape is:

        [staleness alarm]   — exempt, leads, never suppressed
        [drop notice]       — exempt, leads
         summary bubble     — the only bubble the normal run produces
        [⚠️ source health]  — exempt, follows

    `skipped` is still accepted and is still written to the archive, but it no
    longer produces a bubble of its own. The "near miss" digest it used to feed
    is gone: it was a list of things the prestige gate rejected, and there is no
    prestige gate any more — everything in scope is on the board, where a reader
    who wants to see the marginal employers filters for them instead of being
    sent a summary of what he was not shown.

    The two exemptions are load-bearing and must stay separate bubbles. They are
    the only channel that distinguishes "nothing was found" from "I could not
    look", and the day they matter most is the day the summary line says zero.
    """
    health = _fmt_source_health(source_errors)

    open_count = len(active_roles(open_roles)) if open_roles is not None else None
    lines = [f"📋 *Job sift — {today.isoformat()}*"]
    counted = f"{len(surfaced)} new · "
    counted += "open register unavailable" if open_count is None else f"{open_count} open"
    if purged:
        counted += f" · {purged} purged"
    lines.append(counted)
    closing = _closing_line(open_roles, today)
    if closing:
        lines.append(closing.lstrip("\n"))
    lines.append(
        f"_Processed {total_processed} listings, {total_new} new._"
    )
    if board_path:
        lines.append(f"🗂 Board: `{board_path}`")
    else:
        # Said out loud rather than omitted: a summary that points nowhere,
        # silently, reads as a summary that had nothing to point at. And the
        # REASON is whatever actually happened — this line used to hardcode
        # "no board path configured" for a `None` that also meant a render
        # exception, which is a cause reported without being checked.
        why = board_problem or "reason unrecorded"
        lines.append(f"🗂 Board: not written this run ({why}).")

    out = ["\n".join(lines)]
    if health:
        out.append(health)
    return _prepend_alarm(out, staleness_alarm, drop_notice)


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

    lanes = _split_lanes(surfaced)

    # Written even when empty, for the same reason the register keeps its empty
    # sections explicit: a missing heading reads as a rendering bug, an explicit
    # "none" reads as a fact about the day.
    for key, heading in (
        ("prestige", "## Surfaced — prestige lane (recognisable employer + in-scope)"),
        ("floor", "## Surfaced — floor lane (technical contract / part-time, any employer)"),
        ("broad", "## Surfaced — broad capture (in scope, claimed by neither lane)"),
    ):
        lines.append(heading)
        lines.append("")
        if lanes[key]:
            lines.extend(_archive_entries(lanes[key]))
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

    lines.append("## 🗂 Open — broad capture")
    lines.append("")
    lines.append(
        "_In scope and captured, but claimed by neither lane above. Since "
        "prestige and technical-ness became tags rather than gates this is "
        "most of what comes in — filter it on the HTML board, not here._"
    )
    lines.append("")
    broad_rest = in_lane(rest, "broad")
    if broad_rest:
        for role in broad_rest:
            lines.extend(_fmt_open_role_entry(role, today))
            lines.append("")
    else:
        lines.append("_No open broad-capture roles._")
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
