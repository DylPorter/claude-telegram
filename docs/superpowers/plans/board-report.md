# Report — capture broadly, filter in the UI

## What shipped

**1. Broad capture + tags.** `ClassifierResult.surface` is now
`scope == "in_scope"` and nothing else. Prestige and technical-ness are no
longer gates; they are columns. The existing LLM call was extended (it already
returned a reason) to also emit `industry` and `is_technical`; `role_type` is
derived from title keywords in `job_sift/tags.py` and never asked of a model.
`lane` gained a third value, `broad`, for the honest answer "neither lane
claimed this" — most of a broad capture. The near-miss digest is deleted.

**2. The purge** (`open_roles.purge`). Two clocks, either sufficient:
`last_seen` older than 30 days, `first_seen` older than 60. Three exemptions:
sticky `applied`/`dismissed`, a deadline still in the future, and an unreadable
date. Every drop is logged with the rule that fired.

**3. The board.** One HTML file, two tabs, no CDN / build step / framework /
network at view time. `board_html.py` is a byte-identical copy in all three
projects (they are separate distributions with nothing shared between them, and
the page has to open on a machine that has none of them installed).

**4. Telegram is a pointer.** One bubble. The staleness alarm and the ⚠️
source-health line stay separate and exempt.

**5. Sibling port.** `hku-cedars-scraper` gained a register, the purge and a
`cedars board` subcommand. Its filter set is derived from the data — a field
added to the register JSON by hand becomes a dropdown with no code change.

## The rule, and how it is enforced

Tags are advisory, never gates. Concretely:

* `clean_bool` returns `None`, never `False`, for anything unparseable — "I
  looked and it is not technical" and "nobody said" are different claims.
* `derive_role_type` returns `None` rather than defaulting to `full-time`,
  which would file every unlabelled title under the value a reader is most
  likely to have excluded.
* A tag absent from a run does not clear the stored one; "no answer today" is
  not a verdict.
* In the UI: an "All" facet hides nothing, a missing value is reachable under
  the `—` option, a missing cell renders `—`, a missing sort key sinks in BOTH
  sort directions, and every view prints "showing N of M".
* An unavailable feed is `Section(available=False)`, not `rows=[]`.

## The bug the live data caught

Generating the first board from the real 59-row register showed the unseen rule
deleting **eleven roles whose deadlines were three weeks away**. Cause: the
CEDARS adapter paginates greedily and stops at the first all-seen page, so
`last_seen` measures *our crawl depth*, not the portal's listing. That is the
same one-value-means-two-things failure this pipeline keeps removing, rebuilt
in the purge. Fix: a future deadline vetoes both clocks. Purge went from
deleting 16 of 59 to 5 of 59, and all five are genuinely dead (three expired
deadlines, two undated rows nobody has listed in a month).

## Numbers

| | before | after |
|---|---|---|
| job-sift tests | 539 | 613 |
| hk-events tests | 199 | 231 |
| hku-cedars-scraper tests | 212 | 251 |
| job-sift Telegram bubbles (a 6-role day) | 11 | 1 |
| register rows surviving the purge | — | 54 of 59 |

## Known limits

* The two services share the board through a **file handoff** (each writes a
  JSON feed, each reads the other's). They run on separate timers, so each
  tab's data is as fresh as that service's last run; the tab prints the feed's
  generation date, and says so plainly when the feed is missing.
* `industry` and `is_technical` are only populated going forward — rows already
  in the register predate the fields and show as untagged. `role_type` has no
  such gap: it is derived at view time for old rows, because it is a pure
  function and computing it is not a guess.
* The purge is unavoidably tuned to sources that re-list. LinkedIn digest
  emails never re-list a role, so LinkedIn rows without a deadline are governed
  entirely by the 30-day unseen clock.

---

# Review round 2 — one Critical, five Importants, seven minors

## CRITICAL 1 — the technical gate had survived as `negative_title`

The review was right and the finding was the important one. `negative_title`
stamped `scope="out_of_scope"` in four places, so a keyword list produced a
**scope** verdict — a technical-ness judgment laundered as the one gate the
redesign deliberately kept. Executed through `_route` with no LLM: "Marketing
Intern", "Data Analyst Intern", "Graduate Trainee Programme", "Sales
Development Representative", "Talent Acquisition Intern" and every "Trading" or
"Risk" title were dropped.

Two of those refuted the gate from inside the same commit. "Graduate Trainee
Programme" died on `trainee` while `rotational` is in the accepted scope
definition and is what the tag vocabulary maps "graduate trainee" **to** — so
the full lane could never produce a `rotational` row. "Data Analyst Intern"
died on `analyst`, the same family as the worked example the redesign was
argued from. And terminating near-misses had removed the last place such a
deletion was visible, while the seen-set has no TTL — so the rejection had gone
from costing a digest line to costing the role, permanently and silently.

**Fix.** `negative_title` is now a tag source and may never produce a verdict —
stated as a ⚠️ contract on the function itself. The keyword's information is
preserved as the **`function` tag**, a board column and facet. Removed from
`_route`, `_scope_quick_classify`, `classify` and `_employer_gated_result`;
retained inside `floor_reason`, which decides a *lane* (itself a tag).

Only **seniority** now resolves scope for free, because that is a genuine scope
judgment whose answer does not vary by reader. The cost is real and is stated
rather than hidden: those titles now reach the batched LLM. `README.md` no
longer contradicts itself 150 lines apart.

Generated from the live register, the `function` facet offers: analyst, asset
management, business develop, finance, risk, sales, strategy, trading, trainee.
Every one of those is a row that used to be deleted.

## IMPORTANT 2 — a malformed deadline lost the veto

`deadline_date` returned `None` for both "no deadline" and "I could not parse
the deadline", so the future-deadline veto did not apply to a hand-typed date —
one-value-two-meanings rebuilt inside the fix for it, in the field the
exemption matters most for, in a register documented as hand-editable.
`deadline_state` now returns `("none"|"unreadable"|"known", date)`; unreadable
keeps the row and logs a warning. Same fix for hk-events' `start_state` and the
sibling's `deadline_state`.

## IMPORTANT 3 — a test overwrote real state

`jobs_feed_path` was unpatched, so every `pytest` run wrote
`.data/state/jobs_feed.json` — the file hk-events renders as its Jobs tab.
Patched, and each suite now carries a `TestTestsDoNotTouchRealState` guard that
fails if any future test forgets, rather than only fixing this instance.

## IMPORTANT 4 — a failed board write reported an unchecked cause

`_write_board` returned `None` for three different reasons and the bubble
printed one of them. It now returns a `_BoardWrite(path, problem)` NamedTuple
and the bubble prints the reason actually observed.

## IMPORTANT 5 — the purge is irreversible, and the code said otherwise

`prune`'s docstring claimed a dropped row would be "re-captured as open when
the source re-lists it". It would not: `dedupe.filter_new` skips any id in the
seen-set and the seen-set has no TTL. Corrected — and the correction makes the
case for the sticky exemptions *stronger*. The irreversibility is now stated on
`purge` itself and in both READMEs.

The max-age clock also deleted a role with `last_seen == today`, inverting the
argument the deadline veto rests on. **A sighting today now vetoes it.** The
rule is kept, and still catches the intermittently-sighted row (seen ten days
ago, first seen seventy). The first attempt at this compared a `date` to an ISO
string and was dead code that read as if it worked; the test caught it.

## IMPORTANT 6 — no behavioural coverage of the board's JavaScript

The three invariants were asserted by grepping the emitted source, which passes
if the JS is syntactically broken. Added `board_harness.py` + a
`test_board_render.py` to all three suites: a Node DOM shim executes the
emitted page, replays real interactions (select, search, reset, sort) and
asserts on the rendered DOM — "showing N of M" including the denominator, the
`—` cell text, the untagged option selecting exactly the untagged rows, an
over-narrow filter reading as a filter, and the hostile row rendering but not
linking. Skipped when `node` is absent.

## Minors

All seven done. `docs/superpowers/plans/board-plan.md` is now gitignored with
the reason; the purge docstring no longer names real employers; capture derives
`role_type` from the **title only** so it genuinely matches the board fallback
(the code was fixed, not the claim — description-scanning mislabelled a
permanent role whose body mentioned an internship programme, which `tags.py`
forbids); the tautological lane test now asserts something falsifiable;
`safeHref` whitelists http/https/mailto and the refused row still renders;
`write_board` is atomic in all three; the `.gitignore` comment states what it
does not catch. The `function` tag also strips the term list's `*` prefix
marker — the live register produced a literal `business develop*` dropdown
option.

## Numbers after round 2

| | baseline | round 1 | round 2 |
|---|---|---|---|
| job-sift | 539 | 613 | 652 |
| hk-events | 199 | 231 | 253 |
| hku-cedars-scraper | 212 | 251 | 274 |

Purge on the live 59-row register is unchanged at **54 of 59**.
