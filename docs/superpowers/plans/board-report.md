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
