"""Evening orchestrator.

Spawns a vault-aware Claude subagent to:
  1. Process Inbox/ — convert quick-capture items to proper notes
  2. Orphan sweep — find notes modified in last 24h lacking backlinks; add links
  3. Friction Log pattern review
  4. Update Research Log + Teaching Queue
  5. Stub any obvious missing entity notes (per [[proactive-linking]])

Then pushes a SHORT chunked summary to Telegram.

Usage:
    .venv/bin/python -m signal_brief.orchestrators.evening
    .venv/bin/python -m signal_brief.orchestrators.evening --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from signal_brief.config import LOG_DIR, assert_required
from signal_brief.daily_note import upsert_signal_section
from signal_brief.render import render_for_daily_note, render_for_telegram
from signal_brief.telegram_client import TelegramPushError, push_messages
from signal_brief.vault_agent import result_to_digest, run_vault_agent

EVENING_PROMPT = """You are running the evening vault sweep for an Obsidian-style knowledge vault.

**First read:**
- `CLAUDE.md` if present (vault operating principles)
- `.claude-memory/MEMORY.md` if present (the user's memory index)
- Any vault-specific runbook (`Resources/Tools/*Runbook*.md` or similar)

**Do these tasks in this order (skip any that don't apply to this vault's structure):**

1. **Process Inbox.** Read `Inbox/Quick Capture.md` and any other files in `Inbox/`.
   For each captured item: decide the proper note type (idea, project, person,
   learning, etc.), apply the right template if one exists, place it in the
   correct folder, link it into the graph. After processing, leave a clean
   Quick Capture.md (header + empty).

2. **Orphan / under-linked sweep.** Find notes modified in the last 24 hours that
   lack inbound or outbound `[[wikilinks]]`. For each: scan body for entities
   that should be linked (people, projects, companies, technologies, concepts)
   and convert them to wikilinks. If a mentioned entity has no note yet but
   recurs across the vault, create a stub note for it.

3. **Friction Log pattern review.** If the vault has an `Ideas/Friction Log.md`
   or equivalent, cluster recent entries (past ~7 days). If a pattern threshold
   appears (>= 3 entries on the same friction), surface it. If a clear product
   idea emerges, draft an `Ideas/Idea - ...md` stub.

4. **Research Log update.** If a `Resources/Learning/Research Log.md` exists and
   anything was researched today, append date + topic + 1-line key takeaway.

5. **Teaching Queue.** If a `Resources/Learning/Teaching Queue.md` exists, re-sort
   based on what the user engaged with today and what's surfaced in active projects.

**Constraints:**
- ALL touched notes must be linked into the graph. Under-linking is failure.
- Save memories aggressively for anything notable (preferences, decisions, new
  people/projects/commitments). Storage is cheap, lost context is not.
- Be honest in the summary about what you actually did. No fluff.

**Output (STRICT JSON, nothing else):**

```json
{
  "headline": "1-line summary of what got done. <120 chars.",
  "sections": [
    {
      "title": "Section heading",
      "body": "Telegram-ready markdown. <600 chars per bubble. Concise."
    }
  ],
  "rationale": "Audit trail: what notes got created/edited, what links got added, what patterns surfaced. Keep under 400 chars — this is metadata, not an essay."
}
```

**Suggested sections:**
1. **Inbox processed** — what got filed where (count + 1-line per significant item)
2. **Links added** — what got wired into the graph
3. **Patterns surfaced** — friction log or idea-cluster observations (omit if none)
4. **Tomorrow** — any open thread or pending action worth flagging

If a section is empty, OMIT it entirely. No "nothing to report" filler.

Output ONLY the JSON object. No markdown fences. No prose before or after.
"""


def _setup_logging(date_str: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{date_str}-evening.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stderr)],
        force=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evening vault sweep.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print Telegram messages; don't push or write daily note.")
    args = parser.parse_args()

    today = date.today().isoformat()
    _setup_logging(today)
    log = logging.getLogger("evening")
    assert_required()

    log.info("=== evening sweep %s (dry_run=%s) ===", today, args.dry_run)

    prompt = EVENING_PROMPT
    if args.dry_run:
        prompt = (
            "**DRY RUN MODE — DO NOT MODIFY ANY VAULT STATE.** Do not create, edit, "
            "or move any files. Do not run any tool that writes. Inspect what NEEDS "
            "doing and produce the JSON summary describing what you WOULD do.\n\n"
        ) + EVENING_PROMPT

    result = run_vault_agent(prompt)
    digest = result_to_digest(result, date=today)

    # Prefix headline with evening marker so phone glance distinguishes it.
    if digest.headline and not digest.headline.startswith("🌙"):
        digest.headline = f"🌙 Evening — {digest.headline}"

    messages = render_for_telegram(digest)
    daily_md = render_for_daily_note(digest).replace(
        "## 🌅 Morning Signal Brief",
        "## 🌙 Evening Sweep",
    )

    if args.dry_run:
        print("=" * 60)
        print(f"DRY RUN — would push {len(messages)} Telegram messages:")
        print("=" * 60)
        for i, m in enumerate(messages, 1):
            print(f"\n--- message {i} ({len(m)} chars) ---\n{m}")
        print("\n" + "=" * 60)
        print("Daily note section (would append):\n" + "=" * 60)
        print(daily_md)
        return 0

    # Audit trail first.
    path = upsert_signal_section_evening(today, daily_md)
    log.info("daily note: %s", path)

    try:
        result_push = push_messages(messages)
        log.info("telegram: %d sent, %d failed",
                 len(result_push.get("sent", [])), len(result_push.get("failed", [])))
    except TelegramPushError as e:
        log.error("telegram push failed: %s", e)
        return 2

    log.info("=== evening sweep done ===")
    return 0


def upsert_signal_section_evening(date_str: str, section_md: str):
    """Variant that uses a distinct section marker so morning + evening don't clobber each other."""
    from signal_brief.daily_note import daily_note_path

    MARKER = "## 🌙 Evening Sweep"
    END = "<!-- evening:end -->"
    path = daily_note_path(date_str)
    bounded = f"{section_md}\n\n{END}\n"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {date_str}\n\n{bounded}")
        return path

    content = path.read_text()
    start = content.find(MARKER)
    end_marker = content.find(END, start) if start >= 0 else -1

    if start >= 0 and end_marker >= 0:
        end = end_marker + len(END)
        new_content = content[:start] + bounded + content[end:].lstrip("\n")
        path.write_text(new_content)
    else:
        sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
        path.write_text(content + sep + bounded)

    return path


if __name__ == "__main__":
    sys.exit(main())
