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

## Two admission lanes

A listing can be surfaced by either of two independent lanes. It carries exactly
ONE lane, and the digest, the archive and the register all render them under
separate headings.

| Lane | Question it asks | Typical catch |
|---|---|---|
| **prestige** | Would this employer move the resume bullet? | Anthropic, Jane Street, Google internships |
| **floor** | Is this technical, reachable and short-term — whoever is hiring? | agency-posted contract engineering, part-time RA, rolling contracts |

The prestige lane is the original strict-brand heuristic and is unchanged. The
floor lane was added because a brand filter was discarding paid technical work
that was already in scope — over 87 digests, 269 listings were `in_scope` and
dropped on employer name alone, including contract roles with the monthly rate
printed in the title.

**Overlap.** The lanes genuinely overlap: a prestige employer offering a contract
role satisfies both. `ClassifierResult.lane` is a single value assigned by
precedence — **prestige wins** — so "appears exactly once" is a property of the
data rather than something each renderer has to remember. A listing the prestige
lane admits is never offered to the floor lane.

The floor lane is deliberately looser than the prestige lane: a false positive
costs one line under a clearly-labelled heading, a false negative costs a job
that could actually have been taken. It is **inert unless configured** — with no
`locations` it admits nothing, because a floor lane with no geography is a
firehose. See `floor_lane:` in `config/profile.yaml.example`; if the key is
absent it falls back to `location_allowlist` in `config/companies.yaml`.

## The scope guard, and why the keyword path does not admit

`_scope_quick_classify` resolves obvious titles without an LLM call. The two
directions are deliberately **not** symmetric:

- **Rejecting for free is safe.** Seniority markers (senior/staff/principal/…)
  and non-technical business functions (sales, BD, talent acquisition, an
  unqualified "Analyst") resolve `out_of_scope` with no LLM call.
- **Admitting for free is not.** An intern/summer/contract keyword is a
  *candidate*: it still has to pass the scope classifier.

That asymmetry is the fix for a real defect. The keyword match used to be an
admission, so any title containing "Summer" at a boosted employer was surfaced
with nothing ever asking whether the role was technical — 20 of 35 entries in the
live register were finance, BD and sales roles admitted exactly that way.

This does cost more LLM calls on the titles the fix is aimed at, and that is
worth stating plainly rather than asserting away. No `classifier_log.jsonl`
exists in a fresh checkout to replay against, so the number below is measured
against the 22 titles committed in `tests/test_classifier_lanes.py`
(deliberately edge-case-heavy — intern/summer/contract titles are
over-represented relative to a real day's source mix, so treat this as a
directional check, not a production estimate). On that corpus the
free-resolution rate (no LLM call) moved **59% → 45%** — down, not up.
Several previously-free admits (`Software Engineer Intern`, `Software
Engineering Intern, Summer 2027`, `Summer Technology Programme`) now correctly
fall through to the LLM because they carry no negative-title term to reject
on. The new free rejections (`Business Development Manager`, `Sales
Executive`, `Talent Acquisition Intern`, `Marketing Analyst`, `Graduate
Trainee Programme`) claw some of that back but did not fully offset it on this
sample. Anthropic alone lists ~389 roles, so the extra LLM calls are real
operating cost — accepted here because a false admit reaching the register is
worse than an extra API call, but the tradeoff should be monitored against the
real `classifier_log.jsonl` once one accumulates, not assumed to net to zero.

The term lists behind both features live in `config/profile.yaml` (gitignored),
not in `classifier.py`. The matcher is the mechanism; the terms are who the
digest is for.

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
