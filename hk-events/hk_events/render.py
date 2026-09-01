"""Format classified events into Telegram-friendly chunked messages.

Mirrors job-sift/render.py. Per the operator's delivery preferences
(feedback_telegram_delivery_format): short, chunked into multiple sub-messages —
concision is the deliverable, one event per bubble.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from hk_events.schema import Event, RelevanceResult

_HKT = ZoneInfo("Asia/Hong_Kong")

_TAG_LABEL = {
    "founder_ai": "🚀 Founder / AI",
    "sme_buyer": "🏢 SME-buyer",
}


def _fmt_when(dt) -> str:
    """Render an event start in Hong Kong time (feeds give UTC/tz-aware datetimes)."""
    return dt.astimezone(_HKT).strftime("%a %d %b, %H:%M") + " HKT"


def _days_until(event: Event) -> int | None:
    if event.start is None:
        return None
    from datetime import datetime, timezone

    start = event.start if event.start.tzinfo else event.start.replace(tzinfo=timezone.utc)
    return (start.astimezone(_HKT).date() - datetime.now(_HKT).date()).days


def _fmt_event(event: Event, result: RelevanceResult, stage: str = "new") -> str:
    head = f"{_TAG_LABEL.get(result.tag, '')} — **{event.title}**".strip(" —")
    if stage == "soon":
        days = _days_until(event)
        if days == 0:
            head = f"⏰ **TODAY** — {event.title}"
        elif days == 1:
            head = f"⏰ **TOMORROW** — {event.title}"
        elif days is not None:
            head = f"⏰ **In {days} days** — {event.title}"
    parts = [head]
    if event.start:
        parts.append(f"🗓 {_fmt_when(event.start)}")
    if event.location:
        parts.append(f"📍 {event.location}")
    if event.url:
        parts.append(f"[register]({event.url})")
    return "\n".join(parts)


def _fmt_source_health(source_errors: dict[str, str] | None) -> str | None:
    """Format a ⚠️ health bubble for sources that failed to run.

    Mirrors job_sift/render.py::_fmt_source_health. Without this, a dead feed
    or a bot-blocked scrape returns zero and the digest reads as a clean "no
    matches" — indistinguishable from a genuinely quiet day. Surfacing the
    failure turns a silent blind spot into a visible, same-day fix.
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
    banners = [b for b in (staleness_alarm, drop_notice) if b]
    return banners + messages


def render(
    *,
    surfaced: list[tuple[Event, RelevanceResult, str]],
    total_new: int,
    total_processed: int,
    calendar_stats: dict[str, int] | None,
    today: date,
    source_errors: dict[str, str] | None = None,
    staleness_alarm: str | None = None,
    drop_notice: str | None = None,
) -> list[str]:
    """Build the chunked message list for /push. One event per bubble.

    Reminders lead — an event starting in two days is more actionable than one
    discovered six weeks out, so it goes at the top where it will actually be read.

    If nothing surfaced, returns a single quiet heartbeat bubble. The caller
    decides whether to actually send that (see HK_EVENTS_PUSH_EMPTY) — pushing
    "nothing today" every day is what trains you to stop opening the digest.
    Any failed sources get a ⚠️ health bubble appended so a broken source is
    never mistaken for a quiet day, and a `staleness_alarm` (a source dead for
    3+ consecutive runs) is PREPENDED so it leads the digest it invalidates —
    the orchestrator pushes an otherwise-silent empty digest when one is set.
    """
    health = _fmt_source_health(source_errors)

    if not surfaced:
        out = [
            f"🎟 *HK events — {today.isoformat()}*\n"
            f"No new relevant events today. "
            f"Scanned {total_processed} events, {total_new} new."
        ]
        if health:
            out.append(health)
        return _prepend_alarm(out, staleness_alarm, drop_notice)

    soon = [s for s in surfaced if s[2] == "soon"]
    fresh = [s for s in surfaced if s[2] != "soon"]
    founder = [s for s in fresh if s[1].tag == "founder_ai"]
    sme = [s for s in fresh if s[1].tag == "sme_buyer"]

    headline_bits = []
    if soon:
        headline_bits.append(f"{len(soon)} starting soon")
    if fresh:
        headline_bits.append(
            f"{len(fresh)} new ({len(founder)} founder/AI · {len(sme)} SME-buyer)"
        )

    messages: list[str] = [
        f"🎟 *HK events — {today.isoformat()}*\n" + "  ·  ".join(headline_bits)
    ]
    for event, result, stage in soon + fresh:
        messages.append(_fmt_event(event, result, stage))

    if health:
        messages.append(health)

    footer = f"_Scanned {total_processed}, {total_new} new, {len(surfaced)} surfaced."
    if calendar_stats:
        footer += (
            f" Calendar: {calendar_stats.get('created', 0)} added, "
            f"{calendar_stats.get('skipped_existing', 0)} already there._"
        )
    else:
        footer += "_"
    messages.append(footer)
    return _prepend_alarm(messages, staleness_alarm, drop_notice)


def render_vault_archive(
    *,
    surfaced: list[tuple[Event, RelevanceResult, str]],
    dropped: list[tuple[Event, RelevanceResult, str]],
    today: date,
    source_errors: dict[str, str] | None = None,
    staleness_alarm: str | None = None,
    drop_notice: str | None = None,
) -> str:
    """Render the per-day Markdown archive that lands in the vault (audit trail)."""
    lines = [
        "---",
        f"date: {today.isoformat()}",
        "type: hk-events",
        "tags: [hk-events, automation]",
        "---",
        "",
        f"# HK Events — {today.isoformat()}",
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
        lines.append("_Events from these sources are MISSING today. \"None\" below is not authoritative until fixed._")
        lines.append("")
        for src, msg in sorted(source_errors.items()):
            lines.append(f"- **{src}** — {msg}")
        lines.append("")

    if surfaced:
        lines.append("## Surfaced")
        lines.append("")
        for event, result, stage in surfaced:
            when = _fmt_when(event.start) if event.start else "TBD"
            marker = " ⏰ reminder" if stage == "soon" else ""
            lines.append(f"- **{event.title}** ({_TAG_LABEL.get(result.tag, result.tag)}){marker}")
            lines.append(f"  - When: {when}")
            if event.location:
                lines.append(f"  - Where: {event.location}")
            lines.append(f"  - Register: {event.url}")
            lines.append(f"  - Source: {event.source} · Reason: {result.reason}")
            lines.append("")
    else:
        lines.append("## Surfaced")
        lines.append("")
        lines.append("_None today._")
        lines.append("")

    if dropped:
        lines.append("## Dropped (precision-biased filter)")
        lines.append("")
        for event, result, _stage in dropped:
            lines.append(f"- {event.title} ({event.source}) — {result.reason}")
        lines.append("")

    return "\n".join(lines)
