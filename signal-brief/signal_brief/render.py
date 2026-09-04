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
import re
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
    return "📌"


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

# Alarm lane. These bypass the keep-list entirely: a degraded run or a dead
# source must reach the phone even though neither is "news". Rare by
# construction. Title and body are treated the same — a marker anywhere counts.
ALARM_MARKERS = ("⚠️", "🚨")
ALARM_BODY_MARKERS = ALARM_MARKERS  # back-compat alias

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
    return any(m in section.title or m in section.body for m in ALARM_MARKERS)


def is_live_section(section: DigestSection) -> bool:
    """True when a section carries something happening RIGHT NOW.

    Gated on the item data, not the section title. `Happening Now` is the
    title the pipeline has always used for conferences whether they start
    today or next Monday, so the title says nothing about urgency —
    `currently_running` / `days_until` (set by sources/conferences.py) do.

    Depends on the filter attaching `item_urls` to the section; a section with
    no items can never be live.
    """
    for item in section.items:
        meta = item.meta or {}
        if meta.get("currently_running"):
            return True
        days = meta.get("days_until")
        if isinstance(days, int) and not isinstance(days, bool) and days <= 0:
            return True
    return False


# Tokens that end in "." without ending a sentence. Split on one of these and
# a paragraph shatters into fragments — "The U.S." / "AI Action Plan landed."
ABBREVIATIONS = frozenset("""
e.g i.e etc et al cf vs viz approx est fig figs no nos vol vols pp ca
dr mr mrs ms prof sr jr st mt rev gen sen rep gov pres
inc ltd co corp dept univ assn bros
a.m p.m u.s u.k u.n e.u
""".split())

# "U.S." / "e.g." / "a.m." — any run of single letters each followed by a dot.
_INITIALISM = re.compile(r"^(?:[A-Za-z]\.)+$")


def _ends_abbreviation(buf: list[str]) -> bool:
    """True when the '.' just consumed closes an abbreviation, not a sentence."""
    token = "".join(buf).rsplit(" ", 1)[-1].lstrip("([{\"'\u201c\u2018")
    if _INITIALISM.match(token):
        return True
    return token.lower().rstrip(".") in ABBREVIATIONS


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
                if ch == "." and _ends_abbreviation(buf):
                    i += 1
                    continue
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
    if len(_joined(bullets)) > BUBBLE_CHAR_CAP:
        # One clause longer than the whole bubble budget. Trim it rather than
        # shipping an uncapped wall — the daily note still has it in full.
        bullets = [bullets[0][:BUBBLE_CHAR_CAP - 3].rstrip() + "…"]
    return _joined(bullets)


def select_for_telegram(sections: list[DigestSection]) -> list[DigestSection]:
    """The subset of sections that earns a Telegram bubble.

    Three independent lanes, unioned in the original section order:

      1. keep-list — the titles the operator asked for
      2. alarm     — ⚠️ / 🚨 anywhere in the title or body
      3. live      — a conference actually running today (item data, not title)

    The keep-list match is computed on its own and the miss-fallback fires on
    *that* alone. Folding alarms into the match would switch the fallback off
    on exactly the days it exists for: a drifted digest whose sources are also
    failing would ship the ⚠️ and silently drop every real section.
    """
    if not sections:
        return []

    keep_idx = {i for i, s in enumerate(sections)
                if _title_matches(s.title, TELEGRAM_KEEP)}
    extra_idx = {i for i, s in enumerate(sections)
                 if is_alarm_section(s) or is_live_section(s)}

    if not keep_idx:
        # Prompt drift, or a degraded digest whose sections are named after
        # something else entirely. Push the first few real sections so the
        # brief is never a label with nothing under it.
        fallback_idx = [i for i in range(len(sections)) if i not in extra_idx]
        keep_idx = set(fallback_idx[:KEEP_LIST_MISS_FALLBACK])
        log.warning(
            "no section title matched the Telegram keep-list %s — titles were "
            "%s; falling back to the first %d non-alarm section(s)",
            list(TELEGRAM_KEEP), [s.title for s in sections],
            KEEP_LIST_MISS_FALLBACK,
        )

    chosen = sorted(keep_idx | extra_idx)
    kept = [sections[i] for i in chosen]

    # Unconditional, so a partially-drifted digest (one section renamed and
    # quietly dropped) leaves a trace instead of vanishing.
    log.info(
        "telegram: pushing %d/%d sections %s; note-only: %s",
        len(kept), len(sections),
        [s.title for s in kept],
        [s.title for i, s in enumerate(sections) if i not in keep_idx | extra_idx],
    )
    return kept


def _format_section_for_telegram(section: DigestSection, idx: int, total: int) -> str:
    title = section.title.strip()
    counter = f" ({idx}/{total})" if total > 1 else ""
    # A title that already leads with its own marker doesn't get a second one.
    if title.startswith(ALARM_MARKERS):
        header = f"*{title}*{counter}"
    else:
        header = f"{_emoji_for(title)} *{title}*{counter}"
    body = section.body.strip()
    if not body:
        return header
    return f"{header}\n\n{body}"


def render_for_telegram(digest: Digest, *, diet: bool = True) -> list[str]:
    """Produce the Telegram bubbles for a digest.

    Five on a normal day: the headline intro plus Today's Signal, Broad
    Tech/AI, Bubble Breaker and Quiet rest. Sections outside that set are
    note-only. Everything except Today's Signal is bullet-pointed.

    `diet=False` opts out of BOTH the keep-list and the bulletizer — the weekly
    review is a different product (a once-a-week long read, not a daily skim)
    and was not part of the 2026-09-04 diet. Bulletizing it would silently
    truncate its content at MAX_BULLETS even though its section count survived.
    """
    messages: list[str] = []

    # Headline as a leading bubble — sets the day's frame.
    if digest.headline:
        messages.append(f"🌅 *{digest.date}*\n\n{digest.headline}")

    sections = select_for_telegram(digest.sections) if diet else digest.sections
    kept = [s for s in sections if s.body or s.title]
    total = len(kept)
    for idx, s in enumerate(kept, 1):
        # Alarm bodies are short and often carry their own markdown emphasis —
        # reflowing them mangles it for no gain.
        if not diet or _title_matches(s.title, NEVER_BULLETIZE) or is_alarm_section(s):
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
