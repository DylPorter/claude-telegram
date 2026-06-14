"""Render a Digest into:
  (a) a list of chunked Telegram messages (one per section), and
  (b) a markdown blob for the daily-note audit trail.
"""

from __future__ import annotations

import logging

from signal_brief.schema import Digest, DigestSection
from signal_brief.threads import ReconcileResult, Thread

log = logging.getLogger(__name__)

# Glyphs for reconciled thread statuses in the audit trail.
_STATUS_GLYPH = {
    "done": "✅",
    "deferred": "⏸️",
    "dropped": "🗑️",
    "in_progress": "🔄",
    "open": "•",
}

# Telegram-friendly bubble headers — emoji prefix so the user can skim by glance.
SECTION_EMOJI = {
    "today's signal": "🎯",
    "signal": "🎯",
    "happening now": "⏰",
    "happening this week": "📅",
    "bubble breaker": "🫧",
    "outside the bubble": "🫧",
    "live now": "🔴",
    "quiet rest": "🌫️",
    "rest": "🌫️",
    "research": "📄",
    "industry": "🏭",
    "conferences": "📅",
}


def _emoji_for(title: str) -> str:
    key = title.strip().lower()
    if key in SECTION_EMOJI:
        return SECTION_EMOJI[key]
    for k, v in SECTION_EMOJI.items():
        if k in key:
            return v
    return "•"


def _format_section_for_telegram(section: DigestSection, idx: int, total: int) -> str:
    emoji = _emoji_for(section.title)
    counter = f" ({idx}/{total})" if total > 1 else ""
    header = f"{emoji} *{section.title}*{counter}"
    body = section.body.strip()
    if not body:
        return header
    return f"{header}\n\n{body}"


def render_for_telegram(digest: Digest) -> list[str]:
    """Produce one message per logical section. Telegram sender will further
    split anything past the 4000-char hard limit, but we aim much shorter.
    """
    messages: list[str] = []

    # Headline as a leading bubble — sets the day's frame.
    if digest.headline:
        messages.append(f"🌅 *{digest.date}*\n\n{digest.headline}")

    total = len([s for s in digest.sections if s.body or s.title])
    idx = 0
    for s in digest.sections:
        if not (s.body or s.title):
            continue
        idx += 1
        messages.append(_format_section_for_telegram(s, idx, total))

    return messages


def render_threads_for_telegram(result: ReconcileResult) -> list[str]:
    """Produce the thread-related Telegram bubbles:
      1. 🧵 Open Threads — only ACTIVE threads (resolved ones never re-surface).
      2. 🔎 Quick check-ins — the (≤3) questions, OMITTED ENTIRELY when there
         are none. Prefer 0 over filler per the brevity constraint.

    Returns [] when there's nothing live to say at all.
    """
    messages: list[str] = []

    active = result.active_threads()
    if active:
        lines = ["🧵 *Your Open Threads*", ""]
        for t in active:
            detail = f" — {t.detail}" if t.detail else ""
            lines.append(f"• *{t.title}*{detail}")
        messages.append("\n".join(lines))

    # Questions bubble — omitted entirely if empty (hard brevity rule).
    if result.questions:
        lines = ["🔎 *Quick check-ins*", ""]
        for q in result.questions:
            lines.append(f"• {q}")
        messages.append("\n".join(lines))

    return messages


def render_threads_for_daily_note(result: ReconcileResult) -> str:
    """Audit-trail markdown for the reconciliation pass. Keeps every thread
    (including resolved ones, with a status glyph) plus the questions and
    rationale. Mirrors what went to Telegram so the daily note is a faithful
    record.
    """
    lines: list[str] = ["## 🧵 Thread Reconciliation", ""]

    if result.threads:
        for t in sorted(result.threads, key=lambda x: (not x.is_active(), x.title.lower())):
            glyph = _STATUS_GLYPH.get(t.status, "•")
            detail = f" — {t.detail}" if t.detail else ""
            lines.append(f"- {glyph} **{t.title}** (`{t.status}`){detail}")
        lines.append("")
    else:
        lines.append("_No open threads tracked._")
        lines.append("")

    if result.questions:
        lines.append("### 🔎 Quick check-ins")
        lines.append("")
        for q in result.questions:
            lines.append(f"- {q}")
        lines.append("")

    if result.rationale:
        lines.append("### Reconciliation rationale")
        lines.append("")
        lines.append(f"```\n{result.rationale}\n```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_for_daily_note(digest: Digest) -> str:
    """Full markdown for the daily-note audit trail. Keeps everything: items,
    URLs, rationale, suppressed list. This is the backend record for grep.
    """
    lines: list[str] = []
    lines.append("## 🌅 Morning Signal Brief")
    lines.append("")
    if digest.headline:
        lines.append(f"> {digest.headline}")
        lines.append("")

    for s in digest.sections:
        lines.append(f"### {s.title}")
        lines.append("")
        lines.append(s.body)
        lines.append("")
        if s.items:
            lines.append("**Items:**")
            for i in s.items:
                date_str = i.published_at.strftime("%Y-%m-%d") if i.published_at else ""
                lines.append(f"- [{i.title}]({i.url}) — `{i.source}` {date_str}".strip())
            lines.append("")

    if digest.rationale:
        lines.append("### Filter rationale")
        lines.append("")
        lines.append(f"```\n{digest.rationale}\n```")
        lines.append("")

    if digest.suppressed:
        lines.append("### Suppressed (deliberately dropped)")
        lines.append("")
        for i in digest.suppressed[:30]:
            lines.append(f"- [{i.title}]({i.url}) — `{i.source}`")
        if len(digest.suppressed) > 30:
            lines.append(f"- _… and {len(digest.suppressed) - 30} more_")
        lines.append("")

    return "\n".join(lines)
