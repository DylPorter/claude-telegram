# Telegram diet — signal-brief

**Branch:** `telegram-diet` · **Date:** 2026-09-04 · **Scope:** `signal-brief/` only
(`job-sift/`, `hk-events/` untouched by instruction).

## Operator instruction (paraphrased)

Keep five morning bubbles — intro, Today's Signal, Broad Tech/AI, Bubble
Breaker, Quiet rest — and drop the rest of the daily traffic: the evening
sweep's inbox / links-added / git-housekeeping / patterns / tomorrow sections,
and the morning's open-threads and quick-check-ins bubbles. Bullet-point
everything except Today's Signal, whose current format is to stay exactly as
it is. Quiet rest in particular must become bullets.

## Measured before / after

Baseline is `sent=N` from `signal-brief/.data/logs/*.log`, 14 days to 2026-09-04.

| Routine | Timer | Bubbles before | Bubbles after |
|---|---|---|---|
| Morning brief | 07:00 | 7 (median; 6–8 range) — 5 signal + 2 thread | **5** |
| Evening sweep | 22:00 | 5 (median; 2–7 range) | **0** (alarm-only lane) |
| Agent-identity trip-wire | 13:00 | **0** (every run "quiet run — nothing tripped") | 0 — unchanged |
| Weekly review | Sun 20:00 | 10 (median) | 10 — out of scope, explicitly exempted |

Daily total **~12 → ~5**. Weekly adds ~10 once a week, unchanged.

Verified against the 2026-09-04 brief (the run that pushed `sent=7`): re-rendered through the new path it produces exactly 5
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
PYTHONPATH="$PWD" SIGNAL_BRIEF_VAULT_ROOT=<your vault> \
  .venv/bin/python -m pytest tests/ -q
```

`--dry-run` on both orchestrators pushes nothing and writes nothing; confirmed
by test and by a live `morning --dry-run` (real `.data/` untouched, vault
daily-note mtimes unchanged).

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
   the user systemd unit directory but has no unit file in this repo — pre-existing
   drift, unrelated to this change.

---

# Review round 2 — findings addressed

Both safety nets did fail exactly when needed. Reproduced first, then fixed,
then pinned by tests that fail on the previous commit.

## CRITICAL 1 — the drift fallback disabled itself on alarm days

`select_for_telegram` folded the alarm lane into `kept`, so `if kept: return
kept` short-circuited before the fallback. Reproduced with the real August
section titles plus one ⚠️: **2 bubbles, five sections dropped, no log line.**

Rewritten as three independent lanes — keep-list, alarm, live — unioned in
section order. The miss-fallback now fires on the *keep-list* match alone, and
kept/dropped titles are logged unconditionally at INFO so partial drift (one
section renamed) also leaves a trace.

Confirmed: drift + alarm → **6 bubbles, all five sections present, WARNING
logged**. Same input without the alarm still gives 4 + warning.

## CRITICAL 2 — the LLM-filter fallback shipped a label over an empty page

The keep-list dropped every per-source section while the ⚠️ header counted as
a match, so the miss-fallback never fired: 8 items in, **0 delivered**. Fixed
by the same rewrite — no keep-list title matches, so the fallback lane picks
the per-source sections while the ⚠️ header comes through the alarm lane.

Confirmed: **5 bubbles, 8/8 item titles delivered.** The old assertion (`any
("Fallback digest" in m)`) is replaced by one that counts delivered items.

## IMPORTANT 3 — the conference gate is now liveness-shaped

`happening now` / `live now` are gone from the alarm markers. New
`is_live_section()` reads `currently_running` / `days_until <= 0` off the
section's items — set by `sources/conferences.py`, not by the title. All six
historical conference sections were titled `Happening Now` regardless of
urgency, so the title never carried the signal.

`Happening This Week` has never been emitted by this pipeline; the tests now
use `Happening Now` for both cases — pushed when an item is running today,
vault-only when it is four days out.

**New dependency, stated in the prompt:** liveness needs the filter to attach
`item_urls`. A `Happening Now` section with no items can never be live, and a
test pins that.

## IMPORTANT 4 — weekly escaped the keep-list but not the bulletizer

`restrict_sections=False` skipped selection but still ran `bulletize()`: 544
chars in, 416 out, 3 of 9 clauses dropped by `MAX_BULLETS`. The flag is now
`diet=False` and governs both.

Confirmed: **467 chars in, 467 out, body verbatim, no bullets.**

## IMPORTANT 5 — abbreviations

`_split_clauses` now refuses to split after an initialism (`U.S.`, `e.g.`,
`a.m.` — matched by pattern) or a known abbreviation (`Dr.`, `Mr.`, `etc.`,
`Inc.` — matched by list). The reported sentence went from 5 fragments to 2
correct bullets.

## Minors

- Unmapped titles now fall through to `📌`, not `•`, so a header can never
  read as a bullet above a bullet list. Asserted on both the dieted and
  long-read paths.
- A single clause longer than the cap is truncated with `…` instead of
  shipping uncapped: 901 chars → 600.

## Tests asserting on file text

All four are gone. Replaced by behaviour:

- evening's vault work is asserted on the **prompt `run_vault_agent` is
  actually called with** (all six tasks, plus the dry-run no-write directive)
- weekly is asserted on **what it pushes** (every section, bodies verbatim,
  unbulletized)
- the trip-wire is asserted by **running `agent_watch.main()`** with stubbed
  feed/config/push/email: pushes on a TIER1 hit, silent on a quiet run,
  silent under `--dry-run`
- morning's thread cut was already covered behaviourally

## Privacy

New fixtures use neutral placeholders (`Northwind`, `Contoso`, `Client B`,
`Example Corp`). The verbatim operator message and the absolute vault path are
out of this report. **Not fixed, pre-existing:** `tests/test_threads.py` still
carries real client and contact names — untouched to keep this diff scoped,
but it should be scrubbed, and this repo is public.

## Test count

**86 → 92** across the two rounds (39 before any change). All 17 of the
round-2 regression tests fail against the previous commit.

---

# Review round 3 — closing notes

## IMPORTANT — unresolvable `item_urls` demoted a live conference in silence

`_build_digest` dropped URLs that didn't resolve against the collected items
with no trace: `[by_url[u] for u in urls if u in by_url]`. A `Happening Now`
section whose conference URL came back slightly rewritten (a stripped query
string is enough) resolved to `items=[]`, `is_live_section` returned False, and
a conference running *today* became vault-only. The only trace was the neutral
`note-only:` INFO, which reads identically to a section that legitimately had
no items.

Same shape as the two Criticals — the gate turning itself off on exactly the
data it needs — so it's closed with them. `_build_digest` now logs a WARNING
when a section cites `item_urls` and none resolve, and a second WARNING naming
the unresolved URLs when only some do (one live item still resolving is enough
to keep the lane open, so the partial case is a warning, not a demotion).

Confirmed by execution — a live KubeCon whose URL lost its `?utm=1`:

```
WARNING section 'Happening Now' cited 1 item_url(s) but none resolved against
        the collected items — section will have no items
        (urls=['https://example.test/kubecon'])
INFO    telegram: pushing 1/2 sections ["Today's Signal"]; note-only: ['Happening Now']
```

The demotion still happens — the fix makes it visible, not silent. Three tests
pin it: total failure warns, partial failure warns and stays live, and a
section that cited nothing warns about nothing.

## MINOR — stale doc

`README.md` said the weekly opts out via `restrict_sections=False`. It's
`diet=False`.

## Test tightened

The LLM-filter fallback delivery assertion was `>= 5` against an 8-item
fixture, which would have passed a 3-item regression. Now `== 8`.

## Left alone, as directed

The two `_split_clauses` residuals (a sentence ending in an abbreviation merges
with the next; an ellipsis splits) — both under-split, no content loss. And
`tests/test_threads.py`'s real names, being handled separately.

## Test count

**92 → 95.** Both new resolution tests fail against the previous commit.
