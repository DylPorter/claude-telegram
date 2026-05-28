"""LLM filter + anti-bubble pass.

Calls `claude -p` as a subprocess, feeds it:
  - the collected Items (compact JSON)
  - the exposure log (anti-bubble state)
  - permission to read MEMORY.md and project memory files for relevance spec

Returns a structured Digest.

The Claude subprocess has full tool access in the vault working directory,
so it can read any memory file or vault note on demand. We don't pre-embed
memory content — the agent decides what's relevant to fetch.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone

from signal_brief.config import (
    CLAUDE_BIN,
    SIGNAL_BRIEF_EFFORT,
    SIGNAL_BRIEF_MODEL,
    VAULT_ROOT,
)
from signal_brief.exposure import format_for_prompt as exposure_for_prompt
from signal_brief.schema import Digest, DigestSection, Item

log = logging.getLogger(__name__)

CLAUDE_TIMEOUT = 600.0  # 10 min — opus + high effort can be slow

SYSTEM_PROMPT_HEADER = """You are the daily signal-brief agent for an Obsidian-style knowledge vault.

Your job: take a pile of raw items (RSS, conferences, newsletters, tweets) and produce
a personalized daily digest. The output is pushed directly to the user's Telegram and
also written to their vault daily note.

**Read these files first to calibrate (use the Read tool):**

1. `.claude-memory/MEMORY.md` — index of everything you know about the user. Read in full.
2. Then read whichever memory or vault notes look most relevant to today's items —
   active projects, lodestones (people the user follows closely), held positions,
   stated preferences. The vault graph IS the personalization spec; do not invent
   preferences from the items themselves.
3. If the vault has no `.claude-memory/` index, fall back to reading `README.md`,
   `CLAUDE.md`, and any `Projects/` notes you can find.

**Hard constraints (non-negotiable):**

1. **Anti-bubble** is an identity-level constraint for this user. Every digest MUST
   include a "Bubble Breaker" section surfacing exactly ONE item from a content
   domain the user has been underexposed to in the last 7 days. Use the exposure
   log below. If all items are in the user's core domains, pick the least
   in-domain item and frame it as the bubble breaker. Bubble-breaker-tagged feeds
   (typically Aeon / Quanta / Marginal Revolution / Dezeen-style sources) are
   strong candidates.

2. **Telegram chunked output**: each section becomes ONE Telegram message
   (one chat bubble). Aim for 4-7 bubbles total. Each bubble under ~600 chars
   where possible. Concise. No filler. No "executive summary" preamble.

3. **Conferences happening NOW or this week are ALWAYS surfaced.** Source items
   with `kind: conference` get a dedicated section. Missing a live keynote is
   the failure mode this tool exists to prevent.

4. **Don't surface what doesn't matter.** If a section has nothing meaningful,
   omit it. No "no updates today" filler.

5. **Identity-implicating signal** — news from people the user follows closely,
   or events that affect the user's active projects (look these up in memory) —
   ranks high regardless of mainstream newsworthiness.

6. **Broad tech/AI signal** — genuinely interesting technology and AI developments
   that any informed engineer should know about, even if they don't hook into a
   specific project. This includes: semiconductor geopolitics, hardware supply chain
   shifts, major platform announcements, programming language/tooling changes, AI
   safety developments, infrastructure shifts, notable acquisitions. These should
   surface NATURALLY as signal — do NOT force them into the bubble breaker slot.
   A story about China refusing Nvidia GPUs and building its own is broad tech
   signal; it belongs in Today's Signal or its own section, not buried in quiet rest.

**The Bubble Breaker is for genuinely OUTSIDE domains** — philosophy, biology,
architecture, economics, history, art, physics, sports science, linguistics.
It is NOT a catch-all for tech topics that don't hit a specific project. If the
only underexposed content is still tech-adjacent, pick the most non-tech item
and frame it as the bubble breaker. Do not water down the bubble breaker with
tech content that should have appeared naturally above.

**Wikilink convention:** Use `[[note-name]]` to reference vault notes when
relevant. The user reads this output in Telegram (no preview) and in their
vault (where wikilinks resolve). Link liberally when it adds context.

**Output format (STRICT — must be valid JSON, nothing else):**

```json
{
  "headline": "One-line frame for the day's signal. Punchy, specific. <120 chars.",
  "sections": [
    {
      "title": "Section heading (no markdown, just the title)",
      "body": "Telegram-ready markdown. Short. Use *bold* and links sparingly. <600 chars per bubble.",
      "item_urls": ["url1", "url2"]
    }
  ],
  "rationale": "1-2 paragraph trace of how you ranked + what you suppressed and why. For audit-trail only — won't be sent to Telegram.",
  "suppressed_urls": ["urls you deliberately dropped"]
}
```

**Suggested section order:**
1. **Today's Signal** — top 2-3 items hitting active projects/lodestones
2. **Broad Tech/AI** — notable tech developments worth knowing regardless of project hooks
3. **Happening Now** — conferences live or starting this week
4. **Bubble Breaker** — MANDATORY genuinely-outside-tech item
5. **Quiet rest** — short paragraph noting the rest

You may add/remove sections to fit the day's signal. Quality > template adherence.
"""


def _build_prompt(items: list[Item], today: str) -> str:
    items_json = json.dumps(
        [
            {
                "title": i.title,
                "url": i.url,
                "source": i.source,
                "kind": i.source_kind,
                "published_at": i.published_at.isoformat() if i.published_at else None,
                "excerpt": i.excerpt[:400],
                "domain": i.domain,
                "meta": {k: v for k, v in (i.meta or {}).items()
                         if k in {"bubble_breaker", "priority_boost", "currently_running",
                                  "days_until", "feed_name", "tags"}},
            }
            for i in items
        ],
        ensure_ascii=False,
    )

    exposure = exposure_for_prompt()

    return f"""{SYSTEM_PROMPT_HEADER}

---

## Today
{today}

---

{exposure}

---

## Raw items collected today ({len(items)} total)

```json
{items_json}
```

---

Produce the digest JSON now. Remember: read MEMORY.md first, then write the JSON.
Output **ONLY** the JSON object, nothing before or after. No markdown fences.
"""


def _parse_claude_response(raw: str) -> dict:
    """Pull the JSON object out of Claude's response. Defensive parser —
    handles cases where Claude wrapped the JSON in fences or added prose around it.
    """
    # Try direct parse first.
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # Find first { and last } as a last-ditch parse.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            # Truncated JSON recovery: extract headline + any fully-closed sections.
            partial = _recover_truncated_json(text[start:])
            if partial:
                log.warning("recovered partial JSON from truncated vault agent output")
                return partial
            raise ValueError(f"could not parse Claude response as JSON: {e}\n\nResponse was:\n{text[:1000]}")

    raise ValueError(f"no JSON object found in Claude response:\n{text[:1000]}")


def _recover_truncated_json(text: str) -> dict | None:
    """Best-effort recovery from a truncated JSON object.

    Extracts headline and any sections whose closing `}` is present.
    Falls back to None if nothing useful can be recovered.
    """
    result: dict = {"headline": "", "sections": [], "rationale": "⚠️ output truncated"}

    # Extract headline (always comes first and is usually short enough to survive)
    headline_match = re.search(r'"headline"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if headline_match:
        result["headline"] = headline_match.group(1)

    # Extract fully-closed section objects from the sections array
    for m in re.finditer(r'\{\s*"title"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"body"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}', text):
        result["sections"].append({"title": m.group(1), "body": m.group(2)})

    if result["headline"] or result["sections"]:
        return result
    return None


def filter_items(items: list[Item], *, today: str | None = None) -> Digest:
    """Run the LLM filter pass. Returns a Digest ready for rendering."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not items:
        return Digest(
            date=today,
            sections=[DigestSection(
                title="Quiet day",
                body="_No signal items collected. Sources may be down — check logs._",
            )],
            headline="Quiet day — no signal.",
            all_items=[],
            rationale="No items in.",
        )

    prompt = _build_prompt(items, today)

    log.info("invoking claude (%s/%s) with %d items", SIGNAL_BRIEF_MODEL, SIGNAL_BRIEF_EFFORT, len(items))
    proc = subprocess.run(
        [
            CLAUDE_BIN,
            "-p", prompt,
            "--output-format", "text",
            "--permission-mode", "bypassPermissions",
            "--model", SIGNAL_BRIEF_MODEL,
            "--effort", SIGNAL_BRIEF_EFFORT,
        ],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        check=False,
        cwd=str(VAULT_ROOT),
    )

    if proc.returncode != 0:
        log.error("claude exited %d: %s", proc.returncode, proc.stderr[-1000:])
        # Fallback: produce a degraded digest with raw items grouped by source.
        return _fallback_digest(items, today, error=proc.stderr.strip()[-300:])

    try:
        parsed = _parse_claude_response(proc.stdout)
    except ValueError as e:
        log.error("could not parse claude output: %s", e)
        return _fallback_digest(items, today, error=str(e)[:300])

    return _build_digest(parsed, items, today)


def _build_digest(parsed: dict, items: list[Item], today: str) -> Digest:
    by_url = {i.url: i for i in items}
    sections: list[DigestSection] = []
    for s in parsed.get("sections", []):
        urls = s.get("item_urls", []) or []
        sec_items = [by_url[u] for u in urls if u in by_url]
        sections.append(DigestSection(
            title=s.get("title", "").strip(),
            body=s.get("body", "").strip(),
            items=sec_items,
        ))

    suppressed_urls = set(parsed.get("suppressed_urls", []) or [])
    suppressed = [i for i in items if i.url in suppressed_urls]

    return Digest(
        date=today,
        sections=sections,
        headline=parsed.get("headline", "").strip(),
        all_items=items,
        suppressed=suppressed,
        rationale=parsed.get("rationale", "").strip(),
    )


def _fallback_digest(items: list[Item], today: str, *, error: str) -> Digest:
    """If the LLM call fails, emit a minimal grouped-by-source digest so something
    still lands in Telegram. Better than silence."""
    log.warning("using fallback digest path: %s", error[:200])
    by_source: dict[str, list[Item]] = {}
    for i in items:
        by_source.setdefault(i.source, []).append(i)

    sections = [DigestSection(
        title="⚠️ Fallback digest (LLM filter failed)",
        body=f"_LLM filter unavailable. Raw items grouped by source:_\n\nError: `{error[:200]}`",
    )]
    for src, src_items in sorted(by_source.items()):
        lines = [f"*{src}* — {len(src_items)} items:"]
        for it in src_items[:5]:
            lines.append(f"• {it.title[:100]}\n  {it.url}")
        sections.append(DigestSection(
            title=src,
            body="\n".join(lines),
            items=src_items,
        ))

    return Digest(
        date=today,
        sections=sections,
        headline="Fallback digest — LLM filter failed.",
        all_items=items,
        rationale=f"Fallback path used. Error: {error[:500]}",
    )
