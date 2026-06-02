"""Format classified events into Telegram-friendly chunked messages.

Mirrors job-sift/render.py. Per Dylan's delivery preferences
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


def _fmt_event(event: Event, result: RelevanceResult) -> str:
    parts = [f"{_TAG_LABEL.get(result.tag, '')} — **{event.title}**".strip(" —")]
    if event.start:
        parts.append(f"🗓 {_fmt_when(event.start)}")
    if event.location:
        parts.append(f"📍 {event.location}")
    if event.url:
        parts.append(f"[register]({event.url})")
    return "\n".join(parts)


def render(
    *,
    surfaced: list[tuple[Event, RelevanceResult]],
    total_new: int,
    total_processed: int,
    calendar_stats: dict[str, int] | None,
    today: date,
) -> list[str]:
    """Build the chunked message list for /push. One event per bubble.

    If nothing surfaced, returns a single quiet heartbeat bubble.
    """
    if not surfaced:
        return [
            f"🎟 *HK events — {today.isoformat()}*\n"
            f"No new relevant events today. "
            f"Scanned {total_processed} events, {total_new} new."
        ]

    founder = [s for s in surfaced if s[1].tag == "founder_ai"]
    sme = [s for s in surfaced if s[1].tag == "sme_buyer"]

    messages: list[str] = [
        f"🎟 *HK events — {today.isoformat()}*\n"
        f"{len(surfaced)} new ↓  "
        f"({len(founder)} founder/AI · {len(sme)} SME-buyer)"
    ]
    for event, result in surfaced:
        messages.append(_fmt_event(event, result))

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
    surfaced: list[tuple[Event, RelevanceResult]],
    dropped: list[tuple[Event, RelevanceResult]],
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
        for event, result in surfaced:
            when = _fmt_when(event.start) if event.start else "TBD"
            lines.append(f"- **{event.title}** ({_TAG_LABEL.get(result.tag, result.tag)})")
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
        for event, result in dropped:
            lines.append(f"- {event.title} ({event.source}) — {result.reason}")
        lines.append("")

    return "\n".join(lines)
