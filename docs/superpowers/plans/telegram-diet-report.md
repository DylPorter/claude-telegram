# Telegram diet — signal-brief

**Branch:** `telegram-diet` · **Date:** 2026-09-04 · **Scope:** `signal-brief/` only
(`job-sift/`, `hk-events/` untouched by instruction).

## Operator instruction

> "'inbox, links added, git housekeeping, patterns / logs, tomorrow' all useless.
> 'your open threads, quick checkins' those all suck. i like the morning intro,
> today's signal, broad tech AI, bubble breaker, quiet rest, they just need to be
> bullet pointed esp for the 'quiet rest', current today's signal format is perf."

## Measured before / after

Baseline is `sent=N` from `signal-brief/.data/logs/*.log`, 14 days to 2026-09-04.

| Routine | Timer | Bubbles before | Bubbles after |
|---|---|---|---|
| Morning brief | 07:00 | 7 (median; 6–8 range) — 5 signal + 2 thread | **5** |
| Evening sweep | 22:00 | 5 (median; 2–7 range) | **0** (alarm-only lane) |
| Agent-identity trip-wire | 13:00 | **0** (every run "quiet run — nothing tripped") | 0 — unchanged |
| Weekly review | Sun 20:00 | 10 (median) | 10 — out of scope, explicitly exempted |

Daily total **~12 → ~5**. Weekly adds ~10 once a week, unchanged.

Verified against today's real brief (`Daily Notes/2026-09-04.md`, the run that
pushed `sent=7`): re-rendered through the new path it produces exactly 5
bubbles at 165 / 677 / 472 / 283 / 461 chars.

## What still runs but no longer notifies

- **The entire evening sweep.** `run_vault_agent` is untouched: inbox
  processing, orphan / under-linked sweep, Friction Log pattern review,
  Research Log append, Teaching Queue re-sort and the gbrain resync all still
  execute at 22:00, and the full summary is still written to the daily note
  under `## 🌙 Evening Sweep`. Only `push_messages` was removed.
- **Thread reconciliation.** Still runs inside the morning job, still writes
  `## 🧵 Thread Reconciliation` (including "🔎 Quick check-ins") to the daily
  note, still persists `threads.json`. `render_threads_for_telegram` is deleted.
- **Filter rationale + suppressed list.** Already note-only before this change;
  now pinned by test.

## What still notifies

- Morning: intro · Today's Signal · Broad Tech/AI · Bubble Breaker · Quiet rest.
- **Alarm lane, everywhere.** Any section whose title or body carries ⚠️ / 🚨,
  plus `fallback` / `happening now` / `live now`, bypasses the keep-list. This
  covers the LLM-filter fallback digest, a live conference keynote, and the
  degraded-vault-agent path. A zero-item morning is now explicitly marked ⚠️
  (it previously rendered as a neutral "Quiet day", which would have hidden a
  total source outage behind the new keep-list).
- Evening pushes exactly one short bubble if and only if the sweep degraded.
- The 13:00 trip-wire is unchanged — see Concerns.

## Formatting

- `Today's Signal` body is passed through verbatim. `NEVER_BULLETIZE` guards it
  and a test asserts the exact prose survives.
- Broad Tech/AI, Bubble Breaker, Quiet rest run through `bulletize()`:
  bracket-aware clause splitting (never cuts a markdown link or a decimal),
  max 6 bullets, trimmed from the end to the 600-char cap, idempotent on
  already-bulleted input. Alarm bodies are exempt (splitting them broke their
  `_italics_`).
- The filter prompt now names the four Telegram titles, says which are bullets
  and which is prose, and states that other sections are vault-only.

## Tests

`77 passed` (was `39 passed`). No network, no `claude -p` subprocess, no writes
outside `tmp_path`. Two new files: `tests/test_telegram_diet.py` (rendering)
and `tests/test_orchestrator_delivery.py` (both orchestrators' `main()` with
every outbound edge stubbed). `tests/test_render_threads.py` was rewritten —
its Telegram assertions described behaviour that no longer exists; its
daily-note assertions were kept and extended.

Verified failing on HEAD before the change: `test_telegram_diet.py` errors at
import, `test_orchestrator_delivery.py` 5 failed / 2 passed — the 2 that pass
on both are the daily-note-preservation guards, which is the point.

```sh
cd signal-brief
find . -name __pycache__ -type d -exec rm -rf {} +
PYTHONPATH="$PWD" SIGNAL_BRIEF_VAULT_ROOT=/home/tdporter/Documents/Obsidian \
  .venv/bin/python -m pytest tests/ -q
```

`--dry-run` on both orchestrators pushes nothing and writes nothing; confirmed
by test and by a live `morning --dry-run` (real `.data/` untouched, vault
`Daily Notes/` mtimes unchanged).

## Concerns

1. **The brief said agentwatch sends the "quick checkins". It doesn't.**
   "🔎 Quick check-ins" is `render.py`'s thread renderer, pushed with the
   morning brief — that is what I cut. `agent_watch.py` is a rare trip-wire
   that logged `quiet run — nothing tripped` on all 14 runs in the fortnight to
   2026-09-04, i.e. **0 messages**. Cutting its push would have saved nothing
   and silenced the one alert it exists to deliver — directly against the
   "alarms must survive on a quiet day" constraint. **I left it running and
   pinned it with a test.** Reverse this if you actually want it gone.

2. **Conferences.** The operator's five don't include "Happening Now", but the
   filter prompt calls a missed live keynote the failure this tool exists to
   prevent. Compromise: `Happening This Week` is vault-only; a section titled
   *happening now* / *live now* goes through the alarm lane. Rare, but it means
   a conference morning can be 6 bubbles.

3. **`Today's Signal` runs over the 600-char cap** (677 on today's real brief).
   Untouched by instruction — it's the one format called perfect. If you want
   it inside the cap the prompt has to ask for fewer items, not a reflow.

4. **The keep-list is title-matched.** Prompt drift renames a section and it
   silently stops notifying. Mitigated by `KEEP_LIST_MISS_FALLBACK`: if *no*
   section matches, the first 4 are pushed anyway and a warning is logged —
   a noisy brief beats a silent one. Older notes (Aug) show a previous prompt
   generation using entirely different titles ("💼 Signal For You", "🌐 Ambient"),
   so this drift is real and has happened before.

5. **Weekly was silently in the blast radius** and is not in scope, so it now
   opts out via `render_for_telegram(digest, restrict_sections=False)`. If you
   want the weekly cut too, that's a separate call.

6. **No systemd unit was changed** and none needed to be. Note that
   `signal-brief-agentwatch.{service,timer}` is installed under
   `~/.config/systemd/user/` but has no unit file in this repo — pre-existing
   drift, unrelated to this change.
