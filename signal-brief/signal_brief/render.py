"""Render a Digest into:
  (a) a list of chunked Telegram messages (one per section), and
  (b) a markdown blob for the daily-note audit trail.

Telegram and the daily note deliberately diverge. The note keeps EVERYTHING
(every section, the filter rationale, the suppressed list, the full thread
reconciliation) because it is the grep-able backend record. Telegram carries
only the five bubbles the operator actually reads:

    intro · Today's Signal · Broad Tech/AI · Bubble Breaker · Quiet rest

Everything else is note-only. The one exception is the alarm lane (see
`ALARM_MARKERS`) — a source-health or staleness warning has to be seen on a
quiet day, which is the entire reason it exists.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from signal_brief.schema import Digest, DigestSection
from signal_brief.threads import ReconcileResult

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
    "broad tech": "🤖",
    "tech/ai": "🤖",
    "tech / ai": "🤖",
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


# ---------------------------------------------------------------------------
# Telegram diet (2026-09-04)
#
# The operator asked for exactly five bubbles and named them. Anything not on
# this list is written to the daily note and never pushed. Matched as a
# case-insensitive substring of the section title so small prompt drift
# ("Broad Tech / AI" vs "Broad Tech/AI") doesn't silently drop a bubble.
# ---------------------------------------------------------------------------
TELEGRAM_KEEP = (
    "today's signal",
    "todays signal",
    "broad tech",
    "tech/ai",
    "tech / ai",
    "bubble breaker",
    "quiet rest",
)

# "Today's Signal format is perfect — do not touch it." Reflowing this body is
# the one edit that is never allowed.
NEVER_BULLETIZE = (
    "today's signal",
    "todays signal",
)

# Alarm lane. These bypass the keep-list entirely: a degraded run, a dead
# source, or a conference that is live RIGHT NOW must reach the phone even
# though none of them are "news". Rare by construction.
ALARM_TITLE_MARKERS = ("⚠️", "🚨", "fallback", "happening now", "live now")
ALARM_BODY_MARKERS = ("⚠️", "🚨")

# If the keep-list matches nothing (prompt drift, fallback digest), push the
# first few sections anyway. A silent brief is a worse failure than a long one.
KEEP_LIST_MISS_FALLBACK = 4

MAX_BULLETS = 6
BUBBLE_CHAR_CAP = 600  # per the delivery-format spec: aim ~300, hard cap ~600

_BULLET_PREFIXES = ("• ", "- ", "* ")


def _title_matches(title: str, needles: tuple[str, ...]) -> bool:
    key = title.strip().lower()
    return any(n in key for n in needles)


def is_alarm_section(section: DigestSection) -> bool:
    """True when a section must reach Telegram regardless of the keep-list."""
    return (
        _title_matches(section.title, ALARM_TITLE_MARKERS)
        or any(m in section.body for m in ALARM_BODY_MARKERS)
    )


def _split_clauses(text: str) -> list[str]:
    """Split a prose paragraph into bullet-sized clauses.

    Splits on sentence ends and semicolons, but only at bracket depth 0 so a
    markdown link — `[Foo v1.2](https://x.com/a.b/c)` — is never cut in half.
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        buf.append(ch)
        at_end = i + 1 >= n
        nxt = "" if at_end else text[i + 1]
        if depth == 0 and not at_end and nxt == " ":
            if ch == ";":
                out.append("".join(buf).rstrip("; ").strip())
                buf = []
                i += 2
                continue
            if ch in ".!?":
                out.append("".join(buf).strip())
                buf = []
                i += 2
                continue
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [c for c in out if c]


def bulletize(body: str) -> str:
    """Turn a prose body into a tight bullet list.

    Idempotent: a body that is already bulleted comes back with its bullets
    intact. Trims from the end until the bubble fits the char cap — the daily
    note still holds the full text, so dropping a trailing clause here loses
    nothing from the record.
    """
    body = body.strip()
    if not body:
        return body

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if any(ln.startswith(_BULLET_PREFIXES) for ln in lines):
        return "\n".join(lines)

    bullets: list[str] = []
    for para in lines:
        bullets.extend(_split_clauses(para))
    bullets = [b for b in bullets if b][:MAX_BULLETS]
    if not bullets:
        return body

    def _joined(bs: list[str]) -> str:
        return "\n".join(f"• {b}" for b in bs)

    while len(bullets) > 1 and len(_joined(bullets)) > BUBBLE_CHAR_CAP:
        bullets.pop()
    return _joined(bullets)


def select_for_telegram(sections: list[DigestSection]) -> list[DigestSection]:
    """The subset of sections that earns a Telegram bubble."""
    kept = [
        s for s in sections
        if _title_matches(s.title, TELEGRAM_KEEP) or is_alarm_section(s)
    ]
    if kept:
        return kept
    # Nothing matched — don't go silent.
    if sections:
        log.warning(
            "no section matched the Telegram keep-list (titles=%s); "
            "falling back to the first %d",
            [s.title for s in sections], KEEP_LIST_MISS_FALLBACK,
        )
    return sections[:KEEP_LIST_MISS_FALLBACK]


def _format_section_for_telegram(section: DigestSection, idx: int, total: int) -> str:
    title = section.title.strip()
    counter = f" ({idx}/{total})" if total > 1 else ""
    # A title that already leads with its own marker doesn't get a second one.
    if title.startswith(ALARM_BODY_MARKERS):
        header = f"*{title}*{counter}"
    else:
        header = f"{_emoji_for(title)} *{title}*{counter}"
    body = section.body.strip()
    if not body:
        return header
    return f"{header}\n\n{body}"


def render_for_telegram(digest: Digest, *, restrict_sections: bool = True) -> list[str]:
    """Produce the Telegram bubbles for a digest.

    Five on a normal day: the headline intro plus Today's Signal, Broad
    Tech/AI, Bubble Breaker and Quiet rest. Sections outside that set are
    note-only. Everything except Today's Signal is bullet-pointed.

    `restrict_sections=False` opts out of the keep-list — the weekly review is
    a different product (a once-a-week long read, not a daily skim) and was
    not part of the 2026-09-04 diet, so it keeps every section it emits.
    """
    messages: list[str] = []

    # Headline as a leading bubble — sets the day's frame.
    if digest.headline:
        messages.append(f"🌅 *{digest.date}*\n\n{digest.headline}")

    sections = select_for_telegram(digest.sections) if restrict_sections else digest.sections
    kept = [s for s in sections if s.body or s.title]
    total = len(kept)
    for idx, s in enumerate(kept, 1):
        # Alarm bodies are short and often carry their own markdown emphasis —
        # reflowing them mangles it for no gain.
        if _title_matches(s.title, NEVER_BULLETIZE) or is_alarm_section(s):
            shaped = s
        else:
            shaped = replace(s, body=bulletize(s.body))
        messages.append(_format_section_for_telegram(shaped, idx, total))

    return messages


def render_alarm_for_telegram(digest: Digest) -> list[str]:
    """Alarm-only rendering, used by routines whose digest no longer notifies.

    Returns [] on a healthy run — silence is the expected output. When the run
    degraded (⚠️ / 🚨 in the headline or any section) it returns exactly one
    short bubble so a broken sweep can't fail silently for days.
    """
    alarm_headline = any(m in digest.headline for m in ALARM_BODY_MARKERS)
    alarm_sections = [s for s in digest.sections if is_alarm_section(s)]
    if not (alarm_headline or alarm_sections):
        return []

    parts = [f"⚠️ *{digest.date} — needs a look*"]
    if digest.headline:
        parts.append(digest.headline.strip())
    for s in alarm_sections[:2]:
        body = s.body.strip().splitlines()
        parts.append(f"*{s.title.strip()}* — {body[0] if body else ''}".strip(" —"))

    msg = "\n\n".join(p for p in parts if p)
    if len(msg) > BUBBLE_CHAR_CAP:
        msg = msg[:BUBBLE_CHAR_CAP - 1].rstrip() + "…"
    return [msg]


# NOTE (2026-09-04): `render_threads_for_telegram` was removed. "🧵 Your Open
# Threads" and "🔎 Quick check-ins" were cut from Telegram by explicit operator
# instruction. Reconciliation still RUNS every morning and still writes the
# full audit section below — it just doesn't notify. Do not re-add a Telegram
# path here without asking.


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
