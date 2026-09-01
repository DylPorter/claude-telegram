# hk-events

Daily Hong Kong **events** digest for the operator — sibling to `job-sift`, same
architecture. Aggregates HK tech/startup/AI events **plus the right SME-buyer
events**, dedupes, LLM-classifies each for relevance (precision-biased: uncertain
→ drop), pushes a chunked Telegram digest via the same `/push` endpoint
signal-brief + job-sift use, and **idempotently creates Google Calendar events**
(registration link in the body) via the `gws` CLI.

## Architecture

```
 iCal feeds (clean)      structured pages (JSON island in     scrape sources (brittle,
 ├─ Meetup .ics           initial HTML — no feed needed)        degrade per-source)
 └─ Luma .ics             ├─ AI Tinkerers (schema.org JSON-LD)  ├─ Cyberport (cyberport.hk)
                          └─ Luma discovery (__NEXT_DATA__,     └─ StartmeupHK (startmeup.hk)
                             lu.ma/hong-kong — standalone events)
        │                              │                                    │
        └──────────────────────────────┴────────────────────────────────────┘
                                       ▼
        collapse_cross_source — merge duplicates on the Luma `evt-…` api_id
        (luma and luma_discover both see the same event; continuity with an
        existing per-source seen-set beats fixed source precedence, see dedupe.py)
                                       ▼
        dedupe (per-source seen-set) → LLM relevance classifier
                       │                              │
                       │                              ▼
                       │                       JSONL relevance log
        surfaced (founder_ai | sme_buyer) ──────────┐
                       │                            ▼
                       │              gws calendar +insert (idempotent,
                       │              keyed on stable hash in .data/cache/)
                       ▼
        /push to claude-telegram bot  +  daily Markdown archive to vault
```

`mirror_collapsed` runs after classification and writes the winner's seen-record
into the loser's seen-set too, so a source that stops reporting an event it once
shared with another source doesn't re-notify it later (see `dedupe.py` and the
"Two long-standing holes" note below).

Two relevance buckets (the analogue of job-sift's prestige+scope):
- **founder_ai** — funded-startup / AI / founder / hackathon / VC room.
- **sme_buyer** — rooms full of HK SME owners who could buy software/AI services.
- everything else → **drop**. The classifier defaults to drop on any ambiguity
  or failure (see `feedback_job_sift_precision`).

## Setup

```bash
cd ~/Documents/Programming/claude-telegram/hk-events
python3.11 -m venv .venv
.venv/bin/pip install -e .
```

## Env vars

Layers on top of `claude-telegram/.env` (PUSH_SECRET, CLAUDE_BIN, DEFAULT_CWD).
hk-events-specific knobs are in `.env.example` — notably:

- `HK_EVENTS_CALENDAR_ENABLED` — **default 0 (off)**. Calendar writes only happen
  when this is `1`. Until then the orchestrator validates via `gws --dry-run`.
- `HK_EVENTS_CALENDAR_ID` — target calendar; default `primary`. Recommend a
  dedicated secondary calendar ID so auto-added events stay segregated.
- `HK_EVENTS_FETCH_BUDGET_S` — hard wall-clock budget for the whole source-fetch
  phase, in seconds; **default 240**. Sources are fetched in parallel, so the
  phase costs `max(t)` rather than `sum(t)`, and anything still running when the
  budget expires is abandoned and recorded as a failed source — the run
  continues with what landed. This is the ceiling, because an httpx timeout does
  **not** bound a `getaddrinfo` block (on 2026-09-01 a DNS outage produced 135s
  fetches against `_ical_common`'s configured 25s timeout). Raise it only if you
  also raise the unit's `TimeoutStartSec` (currently 900).

## Calendar writes (gws)

Uses the **verified** `gws calendar +insert` helper (gws 0.22.5):

```bash
gws calendar +insert \
  --calendar <CALENDAR_ID> \
  --summary  "<title>" \
  --start    "2026-06-15T19:00:00+08:00" \
  --end      "2026-06-15T21:00:00+08:00" \
  --location "<place>" \
  --description "Register / source: <url>\n..." \
  --format json [--dry-run]
```

`--dry-run` validates the request body and returns `{"dry_run": true, ...}`
without hitting the API (confirmed). **Idempotency:** the `+insert` helper does
not expose iCalUID/extended-properties, so we de-dup ourselves via a map at
`.data/cache/calendar_synced.json` keyed on `Event.stable_hash`
(`sha256(source|normalized-title|start-date)`). Re-runs skip already-synced
events even if the seen-set is wiped.

If newsletter/Gmail in signal-brief works, gws auth is good. On
`invalid_grant` / token-expired, run `gws auth login`.

## Run

```bash
# Safest dry run — stub data, no Telegram, no real calendar, prints the digest:
.venv/bin/hk-events --stub --dry-run

# Dry run against LIVE feeds (no push, no calendar write — gws --dry-run only):
.venv/bin/hk-events --dry-run

# Real run (pushes to Telegram; calendar writes ONLY if HK_EVENTS_CALENDAR_ENABLED=1):
.venv/bin/hk-events
```

## Sources — status

Three tiers. **iCal** = a real `.ics` feed. **structured page** = an HTML page
that server-renders its events as machine-readable JSON (schema.org JSON-LD, or
Next.js `__NEXT_DATA__`) — one `<script>` lookup and a `json.loads`, no CSS
selectors to rot, so it is nearly as durable as a feed. **scrape** = a brittle
DOM scraper.

| Source | Tier | Status |
|---|---|---|
| Data Science & Generative AI HK (Meetup) | iCal | LIVE |
| vLLM Hong Kong (Meetup) | iCal | LIVE |
| Luma calendars (startupshk, lunatechs, moomeetup, codechella) | iCal | LIVE — hkweb3 deliberately off, see `sources.yaml` |
| AI Tinkerers Hong Kong | structured page | **LIVE 2026-09-01** — schema.org JSON-LD on the chapter homepage |
| Luma discovery (`lu.ma/hong-kong`) | structured page | **LIVE 2026-09-01** — catches STANDALONE Luma events |
| Cyberport | scrape | **disabled** — HTTP 403 on every fetch; adapter kept, commented out of `_source_tasks` |
| StartmeupHK | scrape | **disabled** — selectors never landed; may expose a `?ical=1` export → promote to iCal tier |

Two long-standing holes closed on 2026-09-01, both of which the repo had written
off in comments:

- **AI Tinkerers** was parked as "no feed exists, and the site 403s a bare
  fetch". The 403 is gone, and no feed is needed — the homepage publishes the
  chapter's events as schema.org `Event` objects.
- **Standalone Luma events** belong to no calendar, so no `.ics` feed can ever
  see them (this is why CodeChella Week never reached a digest). The old note
  said catching them needed Playwright. It does not: `lu.ma` is a Next.js app,
  so the city page's event list is in `<script id="__NEXT_DATA__">` in the
  initial HTML. `luma_discover` reads it with one `httpx` GET.

`luma_discover` and `luma` overlap on purpose — an event can be standalone today
and attached to a followed calendar tomorrow. `dedupe.collapse_cross_source`
merges the duplicates on the Luma `evt-…` api_id before the seen-set diff, so an
overlapping event is notified once, classified once, and calendared once.

All URLs live in `config/sources.yaml` (`ical_feeds` and `scrape_pages`). Entries
whose URL is empty or starts with `TODO` are treated as UNCONFIGURED — the source
is skipped and its health record pruned, which is deliberately different from
"fetched and found nothing". **Extension points** (add an adapter + config
entry): HKTDC, HKSTP, AWS Summit HK.

## systemd (NOT enabled — install manually)

Units live in `hk-events/systemd/`. They mirror job-sift's pattern. Daily fire
at **09:30 HKT** (after job-sift's 09:00). To install:

```bash
# Symlink into the user unit dir (or copy into ../systemd/ alongside job-sift):
ln -s ~/Documents/Programming/claude-telegram/hk-events/systemd/hk-events.service ~/.config/systemd/user/
ln -s ~/Documents/Programming/claude-telegram/hk-events/systemd/hk-events.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hk-events.timer   # only when you're ready
```

## Notes / recommendations

- `telegram_client.py` is now the **third** verbatim copy (signal-brief,
  job-sift, hk-events). A shared `claude_telegram_push` package would be the
  right refactor — deliberately NOT done here (additive-only constraint).
- StartmeupHK likely runs The-Events-Calendar (WordPress), which usually exposes
  an iCal export. If confirmed, move it from the scrape tier to `ical_feeds`.
