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
firehose. See `floor_lane:` in `config/profile.yaml.example`; the `locations`
key ships commented out there on purpose (see the comment on it), so both a
fresh clone and an existing deployment fall back to `location_allowlist` in
`config/companies.yaml` without either one having to edit a second file. If
that fallback also comes up empty, `floor_lane_config()` logs a warning
rather than rendering a silently empty section forever.

**Genuinely brand-agnostic.** The prestige lane's hard-skip / hard-marginal
employer checks (domain-wrong companies, crypto exchanges) are a judgment
about the EMPLOYER, not the role — so they no longer force `scope` to
`out_of_scope` the way they used to. A hard-skip or hard-marginal employer's
listing is still never `prestige`, but it is now evaluated by the floor lane
on its own technical / reachable / engagement merits, same as anyone else's.
Issue #2 asked for "regardless of employer brand"; that now includes the
employers the prestige lane actively excludes, not just the ones it's merely
silent on.

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

No `classifier_log.jsonl` exists in a fresh checkout to replay against, so
this was measured two ways instead — a small mixed corpus and a real
register — and this time they agree, which they did not the first time this
number was reported (an earlier draft measured the small corpus through
`_scope_quick_classify` in isolation, as if every title came from a boosted
employer; run correctly through `_route` instead, the disagreement was a
measurement artifact, not a real one).

`_scope_quick_classify` only runs at all for a boosted or already-prestige
employer (`_route` decides that first); a listing from a non-boosted employer
takes a completely different path (`full`, an LLM call either way, before or
after this change) where the fix's only effect is the NEW free rejection at
the end of `_route` (the non-technical-title guard, which this change adds
unconditionally). That branch is where most of the saving actually comes
from, because most of a real day's titles are NOT from a boosted employer.

- **8-title mixed corpus** (5 non-boosted, 3 boosted — `tests/test_classifier_lanes.py`,
  run through the real `_route`): free-resolution goes **3/8 → 6/8, up**.
- **The operator's real 45-entry register** (real employers, real titles,
  real source split across CEDARS and LinkedIn — not reproduced here,
  personal application data): free-resolution goes **40.0% → 46.7%, up**.
  Most of that register is non-boosted employers that were paying for a full
  LLM call before this change regardless; the new negative-title guard at the
  end of `_route` now catches several of them for free.

Anthropic alone lists ~389 roles, so neither number should be read as "the"
production figure — the real mix shifts over time as sources and boost-list
membership change. Both measurements are committed here as evidence, not
because either one is definitive; the honest answer is "measure against
`classifier_log.jsonl` once one exists."

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

### Duplicate collapse

One posting can reach the register under two ids — a LinkedIn repost gets a new
job id, so the seen-set (keyed on the id) cannot tell it is the same role.
Duplicates are collapsed in two places: `dedupe.collapse_duplicates`, before the
seen-set diff, for two rows in one run; and `open_roles.collapse_register`, for
the ones that arrived days apart and never met in a single fetch. A collapse
merges history (earliest `first_seen`, latest `last_seen`) and a hand-set
`applied`/`dismissed` always survives onto the surviving row.

The collision key is `JobListing.identity_key` — source, employer, title,
location, exact after case/punctuation normalisation. (Location discriminates
nothing on today's two sources — CEDARS hardcodes `"Hong Kong"` on every row and
LinkedIn's rarely reaches a register row — but it only ever makes the key
stricter, so it stays.) It is **source-scoped on purpose**: CEDARS and LinkedIn share no id and appear in no payload of each
other's, so merging them would mean matching on prose, and a wrong merge
silently deletes a real job while a missed one merely shows it twice. The same
posting listed by two sources is therefore knowingly left as two rows.

### LinkedIn liveness re-check

LinkedIn alert emails carry no deadline and list a posting exactly once, so
`last_seen` never moves and the 30-day `stale` rule was the only thing that
could ever close one — a role that shut two days after it was mailed sat open
for a month. Each run re-checks a bounded slice of the undated LinkedIn rows
against the posting page and retires the ones that say *No longer accepting
applications*.

It fails safe by construction: a row is retired only when an HTTP 200 **served
from a `linkedin.com/jobs/view/` URL** carries that banner. Every other
outcome — transport error, timeout, 403, 404, 429, 5xx, an unreadably short
body, or a redirect that ends up anywhere other than a posting page — is
`unknown`, which changes nothing, not even the `last_checked` date. A failed
request is not evidence about a job.

Two of those guards are not theoretical. LinkedIn 301s an unknown job id onto a
company jobs-index page on a different host (`/jobs/view/3500000000/` →
`br.linkedin.com/jobs/escale-vagas?trk=expired_jd_redirect`) whose body carries
expiry-flavoured prose, so the terminal URL is checked before the body is read.
And the marker list is deliberately a list of one: broader strings like "no
longer available" match ordinary LinkedIn error pages ("This page is no longer
available") while adding no true positives, and a wrong `expired` is
unrecoverable — the row drops out of the register, is pruned after 60 days, and
LinkedIn never re-lists it. 404, incidentally, is **not** the expiry signal: a
closed posting answers 200 with the banner, while 404 is what a nonexistent job
id returns.

The pass is skipped under `--dry-run` and `--stub`, bounded by its own
wall-clock budget (`JOB_SIFT_LIVENESS_BUDGET_S`, default 60s — httpx's
per-socket-operation timeout bounds neither a redirect chain nor a slow-drip
body, so the ceiling has to come from outside the request, exactly as with the
fetch budget), and a crash inside it is caught and logged rather than allowed to
kill a run that has already fetched and classified.

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

# LinkedIn liveness re-check (see "Rolling Open Roles register" above).
# How many undated LinkedIn rows to re-check per run. 0 disables the pass.
# JOB_SIFT_LIVENESS_MAX=10
# Per-row cooldown in days before the same row is re-checked.
# JOB_SIFT_LIVENESS_INTERVAL_DAYS=7
# Hard wall-clock budget for the whole liveness pass, in seconds. Default 60.
# JOB_SIFT_LIVENESS_BUDGET_S=60
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
