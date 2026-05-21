# signal-brief

Daily industry-signal digest for an Obsidian-style knowledge vault. Fetches from
RSS / newsletters / conferences / X-via-RSSHub, LLM-filters against your vault
memory with anti-bubble enforcement, pushes chunked Telegram messages, and writes
a daily-note audit trail.

Lives as a sibling to the [`claude-telegram`](../) bot — they share `.env`
(Telegram chat_id, push secret) and reuse the bot's outbound `/push` HTTP endpoint.

## What it gives you

- A chunked-bubble daily digest pushed to your phone via Telegram at 07:00 every
  day, calibrated to whatever's in your `.claude-memory/` index — not a generic
  tech-news feed
- An evening vault sweep at 22:00 — inbox processing, orphan-note linking,
  Friction Log pattern surfacing, Teaching Queue re-sort
- A weekly review on Sunday evening — Friction Log clustering, idea-status
  audit, graph health, written to `Reviews/YYYY-WXX.md`
- Telegram-first: the human deliverable is the chat bubbles you'll actually
  read on your phone; the vault note is the searchable audit trail

The intelligence layer is `claude -p` running with full vault tool access. It
reads your `MEMORY.md` and project notes on demand and uses *them* as the
filter spec — you never maintain a separate "interests" config.

## Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│ systemd timers (07:00 / 22:00 / Sun 20:00 in your timezone)       │
└───────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────────────────────┐
│ Python orchestrator                                                │
│ 1. Sources  (RSS, conferences, newsletters via gws, X via RSSHub) │
│ 2. LLM filter (claude -p, opus/high, with memory + exposure log)  │
│ 3. Render   (chunked Telegram bubbles + daily-note markdown)      │
│ 4. Push     (POST to bot's /push endpoint)                        │
│ 5. Audit    (write daily note + update exposure log)              │
└───────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────────────────────┐
│ claude-telegram bot (extended with /push endpoint on 127.0.0.1)   │
└───────────────────────────────────────────────────────────────────┘
                          │
                          ▼
                       Your phone
```

The LLM filter pass has full vault tool access via `claude -p --permission-mode
bypassPermissions`. Personalization isn't config — it's the graph itself.

## Prerequisites

- The [`claude-telegram`](../) bot, configured and running as a systemd user service
- Claude Code CLI authenticated (Max plan or API key, whatever the bot uses)
- An Obsidian-style vault somewhere on disk
- Python 3.11+
- (optional) `gws` CLI for Gmail newsletter parsing — <https://github.com/yourorg/gws>
- (optional) systemd user-level timers (Linux). On macOS use `launchd` or `cron`.

## Setup

```bash
# 1. Sibling to the bot
cd ~/Documents/Programming/claude-telegram   # wherever your bot lives
# (signal-brief/ already exists here as a subdirectory)
cd signal-brief

# 2. Venv + install
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# 3. Configure
#    The bot's .env is shared. Add to claude-telegram/.env:
#       PUSH_PORT=7421
#       PUSH_SECRET=<generate with: openssl rand -hex 32>
#    Then either set DEFAULT_CWD to your vault root in claude-telegram/.env
#    (the bot will use it too), OR set SIGNAL_BRIEF_VAULT_ROOT just for this:
echo 'SIGNAL_BRIEF_VAULT_ROOT=/absolute/path/to/your/vault' > .env

# 4. Restart the bot to pick up the new /push endpoint
systemctl --user restart claude-telegram.service

# 5. Smoke test (render path only, no LLM call)
.venv/bin/python tests/smoke_pipeline.py            # print only
.venv/bin/python tests/smoke_pipeline.py --push     # actually push to your Telegram

# 6. First real run (dry-run prints, doesn't push)
.venv/bin/python -m signal_brief.orchestrators.morning --dry-run

# 7. First live run
.venv/bin/python -m signal_brief.orchestrators.morning

# 8. Install systemd timers
cp systemd/signal-brief-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now signal-brief-morning.timer \
                              signal-brief-evening.timer \
                              signal-brief-weekly.timer
systemctl --user list-timers signal-brief-*
```

## Configuration

Edit these YAML / JSON files to tune what gets fetched. No code changes needed.

| File | Purpose |
|---|---|
| `config/feeds.yaml`              | RSS feed list (HN, AI labs, platform blogs, bubble-breakers) |
| `config/conferences.json`        | Tech conference calendar — Google I/O / WWDC / re:Invent / NeurIPS etc. |
| `config/newsletters.yaml`        | Gmail queries (uses the `gws` CLI) |
| `config/twitter_accounts.yaml`   | X handles to monitor via RSSHub |
| `../claude-telegram/.env`        | Shared with the bot (PUSH_SECRET, TELEGRAM_*) |
| `signal-brief/.env`              | Optional local overrides (e.g. `SIGNAL_BRIEF_VAULT_ROOT`) |

### Adjusting the timezone

The included timer files use `Asia/Hong_Kong`. Change it to your local zone
(e.g. `America/Los_Angeles`, `Europe/London`) in the `OnCalendar=` line of each
`signal-brief-*.timer` file before installing them. `timedatectl list-timezones`
lists valid values.

### Adjusting the schedule

Same timer files — edit `OnCalendar=` to whatever time of day suits you.

## Anti-bubble enforcement

Personalization without bubble formation is a structural feature, not a flavor
note. Every digest reserves a "Bubble Breaker" slot for content from a domain
the user has been underexposed to over the past 7 days. Implementation:

- `signal_brief/exposure.py` tracks domain counts per surfaced item
- `config/feeds.yaml` marks Aeon / Quanta / Marginal Revolution / Dezeen as
  `bubble_breaker: true`
- The filter prompt enforces a mandatory "Bubble Breaker" section
- The LLM can override and pull outside-set content from the main feed instead
  if the bubble_breaker feeds yield nothing fresh

If you don't want this, edit the prompt in `signal_brief/filter.py`. But
consider whether you actually want that.

## Manual usage

```bash
cd ~/Documents/Programming/claude-telegram/signal-brief

# Dry-run — print Telegram messages + daily note section to stdout, no push
.venv/bin/python -m signal_brief.orchestrators.morning --dry-run

# Live run — push to Telegram + write daily note
.venv/bin/python -m signal_brief.orchestrators.morning

# Backfill / replay — ignore the seen-urls cache
.venv/bin/python -m signal_brief.orchestrators.morning --no-cache

# Evening / weekly (dry-run passes a no-write flag to the vault agent so
# inspection happens without modifying anything)
.venv/bin/python -m signal_brief.orchestrators.evening --dry-run
.venv/bin/python -m signal_brief.orchestrators.weekly --dry-run
```

## State files (`.data/`)

| File | What it tracks |
|---|---|
| `cache/rss_seen.json`            | URLs already surfaced — dedupes across runs |
| `cache/exposure_log.json`        | Domain exposure history for anti-bubble enforcement |
| `logs/YYYY-MM-DD-morning.log`    | Per-run log (also journaled by systemd) |
| `logs/YYYY-MM-DD-evening.log`    | Per-run log |
| `logs/YYYY-MM-DD-weekly.log`     | Per-run log |

## Known limitations

- **`gws` Gmail auth expires** roughly weekly. If the newsletter source returns 0
  items, run `gws auth login`. The orchestrator degrades cleanly — other sources
  still produce a brief.
- **RSSHub public instances are flaky** — X/Twitter source frequently returns
  empty. This is expected; the filter still produces a quality digest from
  other sources. If you self-host RSSHub, point `RSSHUB_INSTANCES` in
  `signal_brief/sources/twitter.py` at your instance first.
- **Some feeds emit malformed XML** intermittently (Anthropic, Mistral as of
  May 2026) — the fetcher logs and skips.
- **Telegram parse_mode=Markdown** can fail on certain characters (underscores
  in URLs, etc.). The `/push` endpoint retries without parse_mode automatically.

## systemd cookbook

```bash
# Status
systemctl --user list-timers signal-brief-*

# Inspect a recent run
journalctl --user -u signal-brief-morning.service -n 200 --no-pager

# Manually trigger
systemctl --user start signal-brief-morning.service

# Disable a timer
systemctl --user disable --now signal-brief-evening.timer
```

## Extending

- **Add an RSS source**: append to `config/feeds.yaml`. Set `bubble_breaker: true`
  if it's deliberately outside your usual interest set.
- **Add a newsletter**: append to `config/newsletters.yaml` with a Gmail search
  query (uses the `gws` CLI's `gmail users messages list` under the hood).
- **Add a conference**: append to `config/conferences.json`.
- **Add an X account**: append to `config/twitter_accounts.yaml`.
- **Tune the filter**: edit `SYSTEM_PROMPT_HEADER` in `signal_brief/filter.py`.
- **Tune the evening/weekly tasks**: edit the prompt template in the orchestrator
  (`signal_brief/orchestrators/{evening,weekly}.py`).

## File layout

```
signal-brief/
├── pyproject.toml
├── README.md                       (this file)
├── config/
│   ├── feeds.yaml
│   ├── conferences.json
│   ├── newsletters.yaml
│   └── twitter_accounts.yaml
├── signal_brief/
│   ├── __init__.py
│   ├── config.py                   env + paths
│   ├── schema.py                   Item / Digest / DigestSection
│   ├── sources/
│   │   ├── rss.py
│   │   ├── conferences.py
│   │   ├── newsletters.py
│   │   └── twitter.py
│   ├── exposure.py                 anti-bubble state
│   ├── filter.py                   LLM filter (claude -p)
│   ├── render.py                   Telegram chunking + daily-note markdown
│   ├── telegram_client.py          POST to bot's /push endpoint
│   ├── daily_note.py               vault note upsert
│   ├── vault_agent.py              shared evening/weekly subagent helper
│   └── orchestrators/
│       ├── morning.py
│       ├── evening.py
│       └── weekly.py
└── tests/
    └── smoke_pipeline.py           non-LLM render/push smoke test
```

## License

Same as the parent `claude-telegram` project.
