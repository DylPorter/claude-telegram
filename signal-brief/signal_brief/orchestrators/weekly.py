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

from signal_brief.config import LINK_HEALTH_SKILL, LOG_DIR, REVIEWS_DIR, assert_required
from signal_brief.home_refresh import apply_home_refresh
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
        and the question the operator needs to answer to resolve it.
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

4. **Graph health.** SKIP — handled by a dedicated fan-out link-health sweep that
   runs immediately after this review (it finds under-linking, auto-applies
   high-confidence wikilinks, and surfaces stub candidates). Do NOT count orphans
   or links here, and do NOT emit a "Graph health" section — it is appended automatically.

5. **Active threads review — read before surfacing.**
   For each item in the `.claude-memory/MEMORY.md` index that looks like an open
   action or pending task (invoice, message to send, application to file, decision
   pending, etc.):
   a. **Open the underlying memory file** (the link in the index line). Do NOT
      surface the item based on the index line alone — index lines decay and are
      often stale.
   b. **Scan the last 7 daily notes** (`Daily Notes/{date_range}`) for any entry
      that marks this action done (e.g. "sent invoice", "replied to Adam", "submitted
      application"). If found, update the memory file body to past-tense and rewrite
      the MEMORY.md index line accordingly — then skip it from the Telegram surface.
   c. Only surface an item in Telegram if both (a) the memory file body still reads
      as open AND (b) there is no matching completion evidence in the daily notes.
   d. For project/role memories (not action items), apply the same body-read check
      before claiming a project is still active — the body may say CLOSED/KILLED
      while the index line still reads as current.

6. **Write the review note.** Save to `Reviews/{week_filename}.md` with full
   detail — this is the long-form record. The Telegram digest is the short
   surface-level skim.

7. **Home dashboard refresh + Done Log sweep — emit STRUCTURED DATA, do NOT edit
   `Home.md` or `Done Log.md` yourself.** Deterministic Python applies these edits
   after you finish (it does a surgical block-replace so the rest of Home stays
   byte-for-byte intact). Your job is only to DECIDE the contents from this review:

   a. **Read `Home.md`.** Note its current `## 🎯 This Week (...) — ranked` block
      AND its `## 🔭 Next / in the picture` backlog.
   b. **`this_week`**: produce the new ranked immediate list — the few things that
      matter THIS coming week — derived from this review's priorities + the current
      Home state. Pull items up from `Next / in the picture` when they're ready.
      Keep wikilinks (`[[...]]`) exactly as written. Each item: `text` (the bullet,
      no leading number) + `status` emoji from the existing convention
      (🔴 urgent / 🟠 important / 🟢 ready-but-not-urgent / ✍️ writing / etc.).
      Order = rank (most important first). **Derive from reality — never invent a
      task that isn't grounded in the vault/review.** Omit the field (or use the
      same items) if nothing changed.
   c. **`sweep_to_done`**: list every item currently in Home's `This Week` (or
      elsewhere in Home) that is now DONE/SHIPPED/SENT/COMPLETED, to move into the
      Done Log. Each: `date` (YYYY-MM-DD the thing completed; default today {today})
      + `bullet` (a `✅ ...`-style one-liner with wikilinks). Only genuinely
      completed items — the deterministic step skips duplicates, so it's safe to
      include borderline ones, but do not fabricate completions.
   d. **Do NOT delete the `Next / in the picture` backlog** and do NOT touch any
      other Home section. You are only deciding `this_week` + `sweep_to_done`.

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
  "rationale": "Long-form trace: what got reviewed, what shifted, what didn't. Goes to daily note, not Telegram.",
  "home_refresh": {{
    "this_week": [
      {{ "text": "The bullet text WITH [[wikilinks]], no leading number.", "status": "🔴" }}
    ],
    "sweep_to_done": [
      {{ "date": "{today}", "bullet": "✅ One-liner of the completed item, with [[wikilinks]]." }}
    ]
  }}
}}
```

`home_refresh` is consumed by deterministic Python, NOT shown on Telegram. Emit it
based on task 7. If nothing about Home should change, set `"this_week": []` and
`"sweep_to_done": []` (or omit `home_refresh` entirely) — the Python step then
leaves Home untouched.

**Suggested sections (omit if empty):**
1. **Week frame** — what was the dominant arc of this week
2. **Patterns** — friction clusters / direction shifts noticed
3. **Active threads — kill/commit calls** — explicit calls on stalled work
4. (Graph / link health is appended automatically by the link-health pass — do NOT produce it here)
5. **Next week** — top 1-3 priorities going in

Aim for 4-6 Telegram bubbles total including the headline.

Output ONLY the JSON object. No fences. No prose around it.
"""


LINK_HEALTH_PROMPT = """You are running the weekly LINK-HEALTH sweep for the Obsidian vault (today {today}).

Follow the procedure in `{skill_path}` EXACTLY. Mode: {mode}.

Read that skill file first, then execute every step: precompute, fan out parallel scan
subagents over the folder groups, synthesize + completeness-critic, apply per the mode,
and return the result.

**After applying (APPLY mode only):** once the link-health skill has committed + pushed
its wikilink edits, re-index gbrain so it doesn't drift from HEAD — call the
`mcp__gbrain__sync_brain` MCP tool with `no_pull: true`. It runs inside your own
`gbrain serve` (no lock contention). Do NOT use the `gbrain` CLI (it would deadlock
against your serve's PGLite lock). In REPORT-ONLY mode nothing was committed, so skip this.

**Output (STRICT JSON for the digest, nothing else):**
{{
  "headline": "1-line link-health frame. <120 chars.",
  "sections": [ {{ "title": "...", "body": "Telegram markdown, <700 chars" }} ],
  "rationale": "Full trace: notes scanned, proposals, exactly what was auto-applied (with commit hash), stub candidates ranked, coverage gaps."
}}

Suggested sections (omit if empty):
- **🔗 Link health** — notes scanned, N links auto-applied, M stub candidates
- **Auto-applied** — the high-confidence links inserted this week (file → entity)
- **Stub candidates** — top dangling entities with no backing note, ranked

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

    # Dedicated link-health sweep — replaces the old inline "graph health" step.
    # Report-only on dry runs; auto-applies high-confidence links on live runs.
    lh_mode = (
        "REPORT-ONLY — do NOT edit, commit, or push any files"
        if args.dry_run
        else "APPLY mode ON — auto-apply HIGH-confidence first-mention links, then commit + push"
    )
    lh_prompt = LINK_HEALTH_PROMPT.format(
        today=today_iso, mode=lh_mode, skill_path=LINK_HEALTH_SKILL
    )
    log.info("running link-health pass (mode=%s)",
             "report-only" if args.dry_run else "apply")
    lh_result = run_vault_agent(lh_prompt)

    # Merge the link-health sections + rationale into the weekly digest.
    result.sections.extend(lh_result.sections)
    if lh_result.rationale:
        result.rationale = (
            f"{result.rationale}\n\n---\n## Link-health pass\n{lh_result.rationale}"
        ).strip()

    # Home dashboard refresh + Done Log sweep — deterministic, surgical. Driven by
    # the `home_refresh` payload the review agent emitted (never fabricated here).
    # On --dry-run this computes diffs and writes nothing.
    if result.home_refresh:
        hr = apply_home_refresh(
            result.home_refresh, run_date=today_iso, dry_run=args.dry_run
        )
        log.info(
            "home refresh: home_updated=%s done_swept=%s errors=%s",
            hr.get("home_updated"), hr.get("done_swept"), hr.get("errors"),
        )
        if args.dry_run:
            if hr.get("home_diff"):
                print("\n" + "=" * 60)
                print("DRY RUN — Home.md This Week block diff:")
                print("=" * 60 + "\n" + hr["home_diff"])
            if hr.get("done_diff"):
                print("\n" + "=" * 60)
                print("DRY RUN — Done Log sweep diff:")
                print("=" * 60 + "\n" + hr["done_diff"])
        # Surface a one-line summary into the long-form review rationale.
        if hr.get("home_updated") or hr.get("done_swept"):
            result.rationale = (
                f"{result.rationale}\n\n---\n## Home refresh\n"
                f"This Week block {'refreshed' if hr.get('home_updated') else 'unchanged'}; "
                f"{hr.get('done_swept', 0)} item(s) swept to Done Log."
            ).strip()
    else:
        log.info("home refresh: no home_refresh payload from review agent; skipping")

    digest = result_to_digest(result, date=today_iso)

    if digest.headline and not digest.headline.startswith("📊"):
        digest.headline = f"📊 Weekly — {digest.headline}"

    # The weekly review is out of scope for the 2026-09-04 daily-digest diet:
    # it's a once-a-week long read, not part of the ~10-a-day noise floor.
    # Opt out of the five-bubble keep-list so it keeps all of its sections.
    messages = render_for_telegram(digest, diet=False)

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
