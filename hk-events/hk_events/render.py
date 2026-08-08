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


def render(
    *,
    surfaced: list[tuple[Event, RelevanceResult, str]],
    total_new: int,
    total_processed: int,
    calendar_stats: dict[str, int] | None,
    today: date,
) -> list[str]:
    """Build the chunked message list for /push. One event per bubble.

    Reminders lead — an event starting in two days is more actionable than one
    discovered six weeks out, so it goes at the top where it will actually be read.

    If nothing surfaced, returns a single quiet heartbeat bubble. The caller
    decides whether to actually send that (see HK_EVENTS_PUSH_EMPTY) — pushing
    "nothing today" every day is what trains you to stop opening the digest.
    """
    if not surfaced:
        return [
            f"🎟 *HK events — {today.isoformat()}*\n"
            f"No new relevant events today. "
            f"Scanned {total_processed} events, {total_new} new."
        ]

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

    footer = f"_Scanned {total_processed}, {total_new} new, {len(surfaced)} surfaced."
    if calendar_stats:
        footer += (
            f" Calendar: {calendar_stats.get('created', 0)} added, "
            f"{calendar_stats.get('skipped_existing', 0)} already there._"
        )
    else:
        footer += "_"
    messages.append(footer)
    return messages


def render_vault_archive(
    *,
    surfaced: list[tuple[Event, RelevanceResult, str]],
    dropped: list[tuple[Event, RelevanceResult, str]],
    today: date,
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
