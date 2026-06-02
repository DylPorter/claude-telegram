# hk-events

Daily Hong Kong **events** digest for Dylan — sibling to `job-sift`, same
architecture. Aggregates HK tech/startup/AI events **plus the right SME-buyer
events**, dedupes, LLM-classifies each for relevance (precision-biased: uncertain
→ drop), pushes a chunked Telegram digest via the same `/push` endpoint
signal-brief + job-sift use, and **idempotently creates Google Calendar events**
(registration link in the body) via the `gws` CLI.

## Architecture

```
 iCal feeds (clean)            scrape sources (brittle, degrade per-source)
 ├─ Meetup .ics                ├─ Cyberport (cyberport.hk)
 ├─ Luma .ics                  └─ StartmeupHK (startmeup.hk)
 └─ AI Tinkerers feed
        │                              │
        └──────────────┬───────────────┘
                       ▼
        parse → dedupe (per-source seen-set) → LLM relevance classifier
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

| Source | Tier | Status |
|---|---|---|
| Data Science & Generative AI HK (Meetup) | iCal | slug CONFIRMED; `/events/ical/` URL needs a one-time `curl` sanity-check |
| vLLM Hong Kong (Meetup) | iCal | **TODO** — slug unverified |
| Luma calendars (startupshk, hkweb3) | iCal | **TODO** — exact `.ics` URL shape unverified |
| AI Tinkerers Hong Kong | feed | **TODO** — feed URL unverified (site 403s bare fetch) |
| Cyberport | scrape | **stub** — selectors are placeholders, gated by `HK_EVENTS_STUB` |
| StartmeupHK | scrape | **stub** — selectors placeholders; may expose a `?ical=1` export → promote to iCal tier |

All feed URLs live in `config/sources.yaml`. Entries whose URL starts with
`TODO` are skipped automatically, so the pipeline runs end-to-end with whatever
is confirmed. **Extension points** (add an adapter + config entry): HKTDC,
HKSTP, AWS Summit HK.

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
