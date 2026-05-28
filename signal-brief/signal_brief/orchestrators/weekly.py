"""Weekly review orchestrator (Sunday 20:00 HKT).

Spawns a vault-aware Claude subagent to:
  1. Read the last 7 daily notes
  2. Cluster Friction Log entries — surface emerging themes
  3. Audit Ideas/ status (which progressed, which stagnated, which need killing)
  4. Graph health check (orphan count, dangling links, link density)
  5. Write Reviews/YYYY-WXX.md
  6. Push chunked summary to Telegram

Usage:
    .venv/bin/python -m signal_brief.orchestrators.weekly
    .venv/bin/python -m signal_brief.orchestrators.weekly --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from signal_brief.config import LOG_DIR, REVIEWS_DIR, assert_required
from signal_brief.render import render_for_telegram
from signal_brief.telegram_client import TelegramPushError, push_messages
from signal_brief.vault_agent import result_to_digest, run_vault_agent

WEEKLY_PROMPT_TEMPLATE = """You are running the weekly review for an Obsidian-style vault (Sunday {today}).

**First read:**
- `CLAUDE.md` if present
- `.claude-memory/MEMORY.md` if present
- Any vault-specific runbook
- The last 7 daily notes (`Daily Notes/{date_range}`)
- `Ideas/Friction Log.md` if present
- Current state of `Ideas/` (status of each idea)
- `Resources/Learning/Teaching Queue.md` and `Resources/Learning/Research Log.md` if present

**Do these tasks (skip any that don't apply to this vault's structure):**

1. **Memory temporal sweep — DO FIRST, EXECUTE FIXES (don't just flag).**
   The single most common failure mode of this system is `.claude-memory/MEMORY.md`
   index lines decaying out of sync with their underlying memory files, and
   future-dated triggers ("stepping down by ~late May", "ship by 2026-04-30")
   never getting re-evaluated when the date passes. Fix this proactively:

   a. Read `.claude-memory/MEMORY.md`. For each line, scan for date-anchored
      phrases: `by YYYY-MM-DD`, `until YYYY-MM-DD`, `estimated <date>`,
      `end of <month> YYYY`, `~late <month>`, `~early <month>`, `Q[1-4] YYYY`,
      or any `<weekday> <date>` reference.
   b. For each match where the date is in the past relative to today ({today}):
      open the underlying memory file (path in the link) and check its body.
      - If the body reads past-tense (CLOSED, COMPLETED, frontmatter
        `status: completed`, name suffix `(CLOSED)`, or any "DONE/SHIPPED/KILLED"
        marker): **REWRITE the MEMORY.md index line** to reflect current status.
        This is deterministic — do it, do NOT ask permission.
      - If the body still reads future-tense but the trigger date has passed:
        flag it in the Telegram digest with one sentence stating the contradiction
        and the question Dylan needs to answer to resolve it.
   c. For any memory file whose frontmatter has `status: completed` or whose
      name has `(CLOSED)` suffix: also check if downstream vault notes still
      claim the project/role as current. If so, surface as a one-line "needs
      cleanup" item in the Telegram digest.
   d. Report the sweep results in the `rationale` field of the JSON output
      (which goes to the long-form Review note, not Telegram).

2. **Cluster Friction Log entries from this week.** Group by friction type.
   If a theme has >= 3 entries, mark as a "pattern" and propose a product angle.

3. **Idea status audit.** For each note in `Ideas/`: is it #seed, #growing,
   #active, or stale? Flag stale ideas (no updates in 30+ days) for kill/archive
   review.

4. **Graph health.** Count: orphans (notes with no inbound or outbound links),
   under-linked notes (only stub links), dangling wikilinks (link targets that
   don't exist). Surface the worst offenders.

5. **Active threads review.** For each active project note or memory file not
   marked KILLED/completed, is there forward motion? What's the next concrete step?

6. **Write the review note.** Save to `Reviews/{week_filename}.md` with full
   detail — this is the long-form record. The Telegram digest is the short
   surface-level skim.

**Constraints:**
- Proactive suggestions, not passive summaries — flag what should change,
  not just what happened.
- Highlight surprises and pattern shifts, not routine.
- If a project has gone quiet, ask "kill or commit?" — don't soften.
- Save memory updates aggressively for direction shifts.
- **When MEMORY.md index lines and the underlying memory file body disagree,
  trust the body. Rewrite the index. Do not "flag for review" — execute the fix.**
- **For unambiguous archive triggers (frontmatter `status: completed`, name
  suffix `(CLOSED)`, file matches `Areas/University/Final Exams *` after exams),
  execute the `git mv` to `Archive/` directly. Last week's review surfaced
  archive candidates that sat untouched — be the janitor, not the advisor.**

**Output (STRICT JSON, nothing else):**

```json
{{
  "headline": "1-line frame for the week. <120 chars.",
  "sections": [
    {{
      "title": "Section heading",
      "body": "Telegram markdown. <700 chars per bubble. Concise + sharp."
    }}
  ],
  "rationale": "Long-form trace: what got reviewed, what shifted, what didn't. Goes to daily note, not Telegram."
}}
```

**Suggested sections (omit if empty):**
1. **Week frame** — what was the dominant arc of this week
2. **Patterns** — friction clusters / direction shifts noticed
3. **Active threads — kill/commit calls** — explicit calls on stalled work
4. **Graph health** — orphan / under-linked counts + worst offenders
5. **Next week** — top 1-3 priorities going in

Aim for 4-6 Telegram bubbles total including the headline.

Output ONLY the JSON object. No fences. No prose around it.
"""


def _setup_logging(date_str: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{date_str}-weekly.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stderr)],
        force=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Sunday weekly review.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print Telegram messages; don't push.")
    args = parser.parse_args()

    today = date.today()
    today_iso = today.isoformat()
    _setup_logging(today_iso)
    log = logging.getLogger("weekly")
    assert_required()

    # ISO week number for filename
    iso_year, iso_week, _ = today.isocalendar()
    week_filename = f"{iso_year}-W{iso_week:02d}"

    # Date range string for prompt
    start = today - timedelta(days=6)
    date_range = f"{start.isoformat()} → {today_iso}"

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=== weekly review %s (dry_run=%s) ===", week_filename, args.dry_run)

    prompt = WEEKLY_PROMPT_TEMPLATE.format(
        today=today_iso,
        date_range=date_range,
        week_filename=week_filename,
    )

    if args.dry_run:
        prompt = (
            "**DRY RUN MODE — DO NOT WRITE THE Reviews/ NOTE OR ANY OTHER FILE.** "
            "Inspect vault state and produce the JSON summary describing what you WOULD do.\n\n"
        ) + prompt

    result = run_vault_agent(prompt)
    digest = result_to_digest(result, date=today_iso)

    if digest.headline and not digest.headline.startswith("📊"):
        digest.headline = f"📊 Weekly — {digest.headline}"

    messages = render_for_telegram(digest)

    if args.dry_run:
        print("=" * 60)
        print(f"DRY RUN — would push {len(messages)} Telegram messages:")
        print("=" * 60)
        for i, m in enumerate(messages, 1):
            print(f"\n--- message {i} ({len(m)} chars) ---\n{m}")
        return 0

    try:
        result_push = push_messages(messages)
        log.info("telegram: %d sent, %d failed",
                 len(result_push.get("sent", [])), len(result_push.get("failed", [])))
    except TelegramPushError as e:
        log.error("telegram push failed: %s", e)
        return 2

    log.info("=== weekly review done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
