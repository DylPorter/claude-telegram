# job-sift

Daily job-sift digest for the operator. Scrapes HKU CEDARS NETJobs, LLM-classifies each new listing for prestige + scope (internships / short-term contracts only), surfaces matches to Telegram via the same `/push` endpoint the signal-brief module uses. Per-day archive lands in the vault under `Inbox/Job Sift/`.

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

# Hard wall-clock budget for the whole (concurrent) source-fetch phase, in
# seconds. Default 240. Anything still running when it expires is abandoned
# and recorded as a failed source; the run continues with what landed.
# Raise it only if you also raise the unit's TimeoutStartSec (currently 900).
# JOB_SIFT_FETCH_BUDGET_S=240
```

Sources are fetched in parallel, so the fetch phase costs `max(t)` rather than
`sum(t)`. The budget is the ceiling: an httpx timeout does **not** bound a
`getaddrinfo` block (on 2026-09-01 a DNS outage produced 135s fetches against a
configured 25s timeout), so it has to be enforced from outside the fetch call.

## Cookie refresh (automatic, runs before every scheduled sift)

The CEDARS portal needs HKU SSO. We avoid handling credentials by pulling the
session cookie straight out of a browser you're already logged into, via
`job_sift/refresh_cookie.py` (uses `browser_cookie3`):

```bash
.venv/bin/python -m job_sift.refresh_cookie            # pull from Firefox (default)
.venv/bin/python -m job_sift.refresh_cookie --browser chrome
```

It writes `.data/cookies/cedars.json`, which the scraper reads each run. The
`./sift` wrapper (and `job-sift.service`, which runs it daily) calls this
automatically before scraping, trying Firefox first and falling through
chrome/chromium/brave.

**Firefox is the default, not an arbitrary pick.** Chromium-family browsers
decrypt their cookie DB via an OS keyring (SecretService/KWallet), which needs
an unlocked session and does not work from a headless systemd unit. Firefox's
`cookies.sqlite` is plaintext SQLite — `browser_cookie3` reads it directly, no
keyring involved, so it's the only option that reliably works headless.

**Residual limit, stated plainly:** this only helps while Firefox itself still
holds a *live* CEDARS session. The refresh can report success and still hand
the scraper a cookie that's already dead — it copies whatever's in Firefox's
jar, it doesn't validate that CEDARS still accepts it. CEDARS sessions expire
in **hours, not weeks** — if you haven't touched CEDARS in Firefox recently,
re-log-in there (`https://web2.cedars.hku.hk/jobs/` via HKU Portal) before the
next run, or the daily sift will fail with a session-expired error regardless
of how recently the cookie file was refreshed.

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
