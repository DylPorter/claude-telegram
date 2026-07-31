# job-sift

Daily job-sift digest for Dylan. Scrapes HKU CEDARS NETJobs, LLM-classifies each new listing for prestige + scope (internships / short-term contracts only), surfaces matches to Telegram via the same `/push` endpoint the signal-brief module uses. Per-day archive lands in the vault under `Inbox/Job Sift/`.

LinkedIn email-alert parsing is planned for v1.1 — not in v0.

## Architecture

```
CEDARS portal (httpx + cookies)
       │
       ▼
parse listings → diff vs seen-set → LLM classifier (Claude CLI)
       │                                   │
       │                                   ▼
       ▼                              JSONL classifier log
new + prestige + in-scope ─────────────────┐
                                           ▼
                                  /push to claude-telegram bot
                                           +
                                  daily Markdown archive to vault
```

## Rolling "Open Roles" register

The daily archive is a snapshot — a role surfaced on day N vanishes on day N+1
even if its deadline is weeks out. `job_sift/open_roles.py` keeps a persistent,
deduplicated, deadline-sorted register in `.data/state/open_roles.json`, rendered
each run to `Areas/Work/Open Roles.md` (override with
`JOB_SIFT_OPEN_ROLES_PATH`).

Roles age out automatically: past deadline → `expired`, no deadline and unseen
for 30 days → `stale`. Non-open records are pruned 60 days after `last_seen`
(except `applied`, kept as history).

Mark a role applied/dismissed by editing its hidden marker in the note —
`<!-- status:open cedars:123 -->` → `<!-- status:applied cedars:123 -->`. Those
two statuses are sticky: no later run reverts them to open.

`--dry-run` writes neither the JSON state nor the note; it logs the deltas only.

## Setup

```bash
cd ~/Documents/Programming/claude-telegram/job-sift
python3.11 -m venv .venv
.venv/bin/pip install -e .
```

## Env vars (add to `claude-telegram/.env`)

```
# CEDARS portal — the URL of the filtered listings page you actually browse
CEDARS_PORTAL_URL=https://...

# Override the default classifier model (haiku is sufficient)
JOB_SIFT_MODEL=haiku

# Vault archive subfolder (defaults to "Inbox/Job Sift")
# JOB_SIFT_ARCHIVE_DIR=Inbox/Job Sift
```

## Cookie export (one-time, repeat when session expires)

The CEDARS portal needs HKU SSO. We avoid handling credentials by reusing the
session cookies from your already-logged-in Chrome:

1. Log into the CEDARS NETJobs portal in Chrome.
2. Install the "EditThisCookie" extension (or any cookie exporter).
3. Export cookies for the portal's domain as a JSON file.
4. Save it to `.data/cookies/cedars.json` inside this project.

The scraper reads that file each run. When the session expires (usually 1–2 weeks
for HKU SSO), re-export. Future: a Chrome-cookies-DB direct reader could automate
the refresh, but manual is fine for v0.

## Run

```bash
# One-shot manual test
.venv/bin/job-sift

# Scheduled (see ../systemd/job-sift.timer)
systemctl --user start job-sift.timer
```

## Phasing

- **v0** (this version): CEDARS only, classifier-driven prestige+scope, Telegram + vault archive
- **v1.1**: LinkedIn job-alert email parsing via gws CLI
- **v2**: derive a hardcoded prestige whitelist from ~30 days of classifier_log.jsonl; classifier becomes fallback for ambiguous cases
