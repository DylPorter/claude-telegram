# Plan — Close every open issue on DylPorter/claude-telegram

## Context

Two branches already merged: the cron fleet is hardened (concurrent fetch under
a wall-clock budget, per-source staleness alarm, CEDARS cookie refresh) and
hk-events is un-blinded (AI Tinkerers + standalone Luma events, plus a
cross-source dedupe layer that had never existed).

The owner will not share a related repo with a friend until the board is clean.
Open: #1, #2, #5, #6, #7. (#4 auto-closes on push — its `Closes #4` is already
in a merged commit.)

**The through-line for #5/#6/#7:** these are the last members of a bug family
that has appeared in six costumes, every one of them `[]` overloaded to mean
both "I looked and there was nothing" and "I could not look". What worked was
never catching more cases — it was making the ambiguity *unrepresentable*:
a positive `succeeded` set, `None` vs `[]` at every sub-parser boundary, a typed
`SourceNotConfiguredError`, and pruning any source in neither set. Follow that
pattern; do not invent a new one.

## Global Constraints

- Python 3.11+, stdlib + httpx + BeautifulSoup. No new runtime dependencies.
- Never log/print/write a cookie or PHPSESSID value.
- Per-source failures must degrade to a partial run, never kill the run.
- `--dry-run` must write NO state and push NOTHING.
- Every task ships tests that fail before the change and pass after, and tests
  must not hit the network.
- Do not touch `signal-brief/`.
- Preserve the raise-vs-empty contract everywhere. RAISE when the source could
  not be read; return `[]` ONLY when it was genuinely read and had nothing;
  raise `SourceNotConfiguredError` when config is absent/empty so the source is
  pruned rather than scored.
- Baseline test counts: job-sift 132, hk-events 192. They should only go up.

## Task 1 — Close #6 and #7 (the last two silent-zero paths)

**#6 — an empty `location_allowlist` filters every listing out and scores a SUCCESS.**
`job-sift/job_sift/sources/_ats_common.py:70-72` — with a valid `companies.yaml`
carrying slugs but an empty/missing `location_allowlist`, `location_matches`
returns `False` for every listing that has a location. The adapter polls fine,
filters everything out, returns `[]`, raises nothing, and the streak resets.
Verified: `allowlist empty -> HK listing matches? False`.

Fix: an empty/missing allowlist is a config defect, not a filter that matches
nothing — raise `SourceNotConfiguredError` so the source is pruned.
⚠️ Preserve the real case: a *populated* allowlist that legitimately matches
zero listings today IS a successful fetch and must keep counting as one.

**#7 — a tableless CEDARS page 2+ returns a silent partial and scores a SUCCESS.**
`job-sift/job_sift/sources/cedars.py` raises when page 1 has no
`table.tablesorter`, but on pages 2+ logs, stops, and returns what it has.
A WAF trip on page 2 of 5 is a silent partial: streak zeroed, `last_success`
stamped, warning only in the journal.

The current page-1-only rationale is self-contradictory: page 1 is justified
because the template emits the table shell even at zero rows — but that is the
same template on page N, so walking off the end yields a zero-row table, which
the existing `if not page_listings: break` already handles. And `max_pages`
defaults to 5 against ~11 real pages, so a daily run stops on the cap, not by
walking off the end — while sequential page bursts are exactly what trips
bot-detection.

Fix (preferred): stop inferring from the page number and look at the page.
Add a cheap event-independent portal anchor — the tablesorter shell, or the
portal's nav/footer chrome — exactly as `hk_events.sources.aitinkerers._is_chapter_page`
does. Anchor present → end of results, break quietly. Anchor absent → we are off
the portal → raise, on ANY page. Page 1 stops being a special case.
Note `test_a_tableless_page_two_keeps_page_one_and_stops` currently does NOT
fail under a `_parse_listings_html -> []` mutation; whatever you write must.

## Task 2 — Close #1a and #2 (both are classifier.py; do them together)

**#1a — the keyword fallback bypasses the classifier entirely.**
`classifier.py:216-223`, `_scope_quick_classify`: if the title contains any of
`intern/internship/summer/winter/trainee/rotational/12-month/...` it returns
`ClassifierResult(prestige="prestige", scope="in_scope")` and `_route` marks it
`done` — **no LLM call at all**. So "Morgan Stanley IED Summer Analyst" is
admitted because the employer passed `_boost_check` and the title says "summer",
without anything ever asking whether a finance summer-analyst role is technical.

Measured effect: 20 of 35 entries in `Areas/Work/Open Roles.md` are
non-engineering finance/BD/sales roles, most carrying
`reason: "title contains intern/contract keyword"`. Roughly 3 actionable roles
out of 35 — the same hand-scanning problem the bot was built to remove.

> **Unreproducible from this repo.** `Areas/Work/Open Roles.md` is personal
> application data in a private vault and is not checked in, so nothing here
> lets a reader re-derive "20 of 35". A *different* snapshot of the same
> register, counted 45 entries, is cited further down. The two are not
> reconcilable from anything committed. Read both as dated observations, not as
> stable denominators, and do not derive one from the other — see the same
> caveat in `job-sift/README.md` under "The scope guard".

Fix: make the keyword match a **candidate** signal, not an admit. A title
matching only on the keyword must still pass the scope classifier. Add an
explicit negative-title list — Strategy, Business Develop*, Sales, Talent
Acquisition, Trading, Trainee, Asset Management, Risk, Finance, and
Analyst-not-qualified-by-Technology/Engineering/Software. A negative title must
never be admitted by the quick path.
⚠️ Keep the cost win the quick path exists for: `_SCOPE_KEYWORDS_OUT` (senior/
staff/principal/…) resolving `out_of_scope` without an LLM call is still correct
and must stay `done`. Only the *admit* direction becomes a candidate.

**#2 — add a second brand-agnostic "floor" lane.**
The prestige lane was built for a strict-prestige heuristic. Measured over 87
digests, **269 listings were `in_scope` but discarded on prestige grounds**
— same caveat as above. Where the count came from is not recorded here, and a
fresh checkout has no `classifier_log.jsonl` and no digest archive to replay
against, so nothing in this repo lets a reader reproduce or check it. Treat it
as a dated observation. Examples cited below are from the same unavailable
source: `Argyll Scott — 3x AI Platform Support Engineer / 12-month contract /
30-50K P/M` (the monthly rate is in the title), `GUTolution — Part time – AI &
Bioinformatics`, `ConnectedSolutions — Junior Automation Engineer (Rolling
Contract)`, `Aster Recruiting — Data Scientist 6-12 month contract`, and
`HK Metropolitan University — Temporary RA (AI/Data Science)`.

Fix: KEEP the prestige lane unchanged for the summer-2027 hunt. Add a second
parallel lane admitting a listing when it is (a) technical, (b) HK-based or
remote, and (c) part-time / contract / rolling / RA — regardless of employer
brand. Recruiter-posted contract roles must stop being an automatic skip, and a
named monthly rate in the title (e.g. `30-50K P/M`) is a strong POSITIVE signal.
Render the two lanes under **separate headings** in the digest and in the
`Open Roles` note so the prestige signal is not diluted.

## Task 3 — Close #1b (cross-source dedupe for job listings)

The same bug just fixed in hk-events exists here. From #1: IMC
"HK - 2027 - Software Engineer Intern" (`linkedin:1000000001`) and "Software
Engineer Intern 2027" (`linkedin:1000000002`) are the same posting; the HSBC CIB
programme is listed once from CEDARS (`G2600001`) and once from LinkedIn
(`1000000003`).

Port the hk-events approach — read `hk_events/schema.py::identity_key`,
`hk_events/dedupe.py::collapse_cross_source` and `mirror_collapsed` first, and
follow them.
⚠️ Two things that made the hk-events version correct and must carry over:
- The collapse must run BEFORE the seen-set diff.
- The winner's record must be mirrored into the loser's seen-set, or a
  hand-off between sources re-notifies. Do NOT re-key existing seen-sets —
  that re-pushes the whole backlog on first deploy.
⚠️ A false merge is a SILENT DROP and is strictly worse than the duplicate it
fixes. Job listings have no shared stable id across CEDARS and LinkedIn, so
exact-id matching will not work. Choose a conservative key, state your reasoning
in the report, and make the failure direction "missed collapse", never "wrong
merge". If you cannot find a key you trust, say so rather than shipping fuzzy
title matching.

## Task 4 — Close #1c (LinkedIn entries never age out)

LinkedIn roles carry `Deadline: none listed`, so the 30-day-unseen `stale` rule
is the only ageing mechanism and closed postings sit as `open` indefinitely.
IMC `linkedin:1000000001` is already "No longer accepting applications" while the
register still lists it open.

Fix: either parse a deadline from the JD body where one exists, or add a
liveness re-check for LinkedIn entries in the open-roles register. Prefer
whichever is more robust and say why. A liveness check must fail SAFE — an
unreachable posting is "could not check", never "closed", and must not silently
drop a live role from the register.

## Task 5 — Deferred minors worth closing

- `hk_events/dedupe.py` collapse log prints identical strings on both sides when
  it collapses an intra-source duplicate (`keeping luma/evt-X, dropping
  luma/evt-X`) because the same event arrives from two different Luma calendar
  feeds. Behaviour is right; the message should name which feed each copy came
  from.
- `startmeuphk._parse_events_html` still returns `[]` on unmatched placeholder
  selectors, so a 200 with the real DOM is scored a success. Both it and
  cyberport are commented out of `_source_tasks`, so this is latent — fix the
  empty-vs-unreadable signal, or make the comment at `orchestrator.py:85-88`
  say plainly that the selectors must be fixed first.
- `render.py` is a third near-duplicate across the bots with nothing guarding
  it. Extend the existing `test_source_health_parity` approach to cover it.
- `no_network` in both conftests does not block `socket.getaddrinfo`, so real
  DNS still goes out (the suite takes 44.7s instead of 2.4s with the source
  stub disabled). Close the hole.
