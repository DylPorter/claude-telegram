# Plan — Close the source blind spots + finish the health hardening

## Context

Branch `harden-cron` merged: sources fetch concurrently under a 240s budget,
a per-source staleness alarm escalates 3 consecutive failed runs into the
Telegram push, and job-sift's daily run refreshes the CEDARS cookie.

Two things remain. First, two open issues from that work's final review.
Second, hk-events is structurally blind to most HK events — a gap the repo
had recorded as requiring Playwright. It does not.

**Verified empirically on 2026-09-01 (do not re-litigate, build on it):**

- `https://hong-kong.aitinkerers.org/` returns **HTTP 200** (the 403 recorded
  in `config/sources.yaml` on 2026-06-01 is gone) and server-renders
  **schema.org `Event` JSON-LD** in `<script type="application/ld+json">`
  blocks — an `ItemList` whose `itemListElement` entries are full `Event`
  objects with `name`, `description`, `startDate`, `endDate`, `location`
  (nested `Place` + `PostalAddress`), and `@id` as the event URL. Both
  upcoming events (2026-09-12 hackathon, 2026-09-29 meetup) are present.
  The blocks also contain non-Event types (`Organization`, `WebSite`,
  `CollectionPage`, `BreadcrumbList`, `BlogPosting`) which must be filtered out.

- `https://lu.ma/hong-kong` server-renders **12 upcoming HK events** inside
  `<script id="__NEXT_DATA__">`. Luma is a Next.js app: the data is in the
  initial HTML *before* any JS runs. Event objects carry `name`, `start_at`,
  `api_id`, and a url slug. This reaches **standalone** Luma events that
  belong to no calendar — the CodeChella blind spot that no iCal feed can
  ever see.
  NOTE: `lu.ma/hk` is NOT a city page — it is a stale 2023 event whose slug
  happens to be "hk". The city path is `lu.ma/hong-kong`.
  The `api.lu.ma/discover/*` JSON endpoints all 404; do not use them.

## Global Constraints

- Python 3.11+, stdlib + httpx + BeautifulSoup (all already used). **No new
  runtime dependencies. Do NOT add Playwright or any browser.**
- Never log/print/write a cookie or PHPSESSID value.
- Per-source failures must degrade to a partial run, never kill the run.
- New adapters MUST follow the post-`harden-cron` contract: raise
  `SourceFetchError` when the source could not be read at all, return `[]`
  only when the source was genuinely read and had nothing. Returning `[]` on
  a failure re-introduces the silent-zero bug that branch existed to kill.
- `--dry-run` must write NO state and push NOTHING.
- Every task ships tests that fail before the change and pass after.
- Parsers must be tested against SAVED FIXTURES of the real HTML, not live
  network calls. Tests must not hit the network.
- Do not touch `signal-brief/`.

## Task 1 — Close #5 and #4 (source-health residuals)

**#5, part a — the config-read path still scores silent-zero as success.**
When a source's config is absent or empty the adapter returns `[]` without
raising and is scored a SUCCESS, resetting an accumulated streak and stamping
today as `last_success`. Reproduced live: with `companies.yaml` missing, a
seeded 12-run streak went to 0.
Sites: `job-sift/job_sift/sources/_ats_common.py:34-37` (the `_CFG_CACHE = {}`
degrade, consumed at `greenhouse.py:95` and the lever/ashby equivalents), and
`hk-events/hk_events/sources/_ical_common.py:210` (the escalation guard reads
`if urls and ...`, so empty `urls` skips the raise).
Fix: treat "no config" as **neither success nor failure** — prune the source,
exactly as a source absent from both the `succeeded` and `errors` sets is
already pruned. Success must require positive evidence.

**#5, part b — nothing pins the `run()`-level `succeeded` wiring.**
Mutating the orchestrator to pass `succeeded=enabled_sources()` — the exact
original inference bug — currently passes both suites. Add an
orchestrator-level test in each module that fails under that mutation.

**#4 — `source_health.py` is duplicated byte-identical across both modules**
and nothing stops the two `ALARM_THRESHOLD` values drifting. If they drift the
bots silently disagree about what "dead" means. Do NOT restructure into a
shared package (the repo is a candidate to be split). Add a test in each
module asserting the two files agree on `ALARM_THRESHOLD` and on their public
function signatures, so drift fails CI.

Tests: a source with absent/empty config is pruned, not reset, and its
existing streak survives; the two threshold constants are pinned equal.

## Task 2 — Two new hk-events sources: AI Tinkerers (JSON-LD) + Luma discovery

Both are plain `httpx` GET + parse. Follow the existing adapter conventions in
`hk-events/hk_events/sources/`, and register both in the fetcher list in
`hk_events/orchestrator.py::_fetch_all_sources`.

**2a — AI Tinkerers.** Replace the stub in `sources/aitinkerers.py`. Fetch
`https://hong-kong.aitinkerers.org/`, extract every
`<script type="application/ld+json">` block, parse each, walk `ItemList`
→ `itemListElement`, and keep entries whose `@type` is `Event`. Map to the
existing `Event` schema. Skip non-Event types. A past event must be filtered
by the existing horizon logic, not by the parser.
This source is already in `AUTO_FOUNDER_SOURCES` (auto-tagged `founder_ai`) —
confirm that still holds and that the config's "no feed exists" comment is
corrected.

**2b — Luma discovery.** New adapter `sources/luma_discover.py`. Fetch
`https://lu.ma/hong-kong`, extract `<script id="__NEXT_DATA__">`, parse the
JSON, and walk it for event objects (they have both `name` and `start_at`).
Build the event URL from the slug/`api_id`. This is a DIFFERENT source name
from the existing calendar-feed `luma` adapter — they will overlap, and the
existing dedupe must collapse duplicates rather than double-report. Verify
that dedupe actually catches an event appearing in both, and say how in the
report.
Treat the `__NEXT_DATA__` shape as unstable: if the script tag is missing or
the JSON does not parse, RAISE (that is "could not look"), but if it parses
and simply contains zero events, return `[]` after logging — a genuinely
quiet week is not a failure.

Both: send a browser-like User-Agent (the existing `_ical_common._HEADERS`
already does this and both hosts require it), and reuse the existing timeout
conventions. Save one real HTML fixture per source under `hk-events/tests/`
and test the parsers against those.

## Task 3 — Correct the stale documentation

These actively mislead a reader (including the person this repo may be shared
with):
- `job-sift/job_sift/sources/cedars.py` — the module docstring still opens
  **"v0 STATUS: stub — returns hardcoded sample listings"**. It has been a
  real scraper for months. Rewrite it to describe what the module does now,
  including that `_stub_listings` is only reachable via `JOB_SIFT_STUB=1`.
- `job-sift/README.md` — the "Cookie export" section still describes the
  manual EditThisCookie flow and says "Future: a Chrome-cookies-DB direct
  reader could automate the refresh, but manual is fine for v0." That reader
  exists (`refresh_cookie.py`), is wired into `sift`, and runs daily. Rewrite
  the section around the real flow, and state the honest residual limit: it
  refreshes from Firefox's cookie DB, so it only helps while Firefox itself
  holds a live CEDARS session. Note Firefox is the default and why.
- `hk-events/config/sources.yaml` — correct the `aitinkerers` comment
  ("no public iCal/RSS feed") and the standalone-events note claiming a
  discovery scrape "would need Playwright". Record what was actually verified
  on 2026-09-01 and keep the dated-verification convention the file already uses.
- `hk-events/README.md` — add the two new sources to the architecture diagram.

No behaviour changes in this task. Docs only.
