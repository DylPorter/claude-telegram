# job-sift

Daily job-sift digest for the operator. Pulls listings from HKU CEDARS NETJobs, LinkedIn job-alert emails and three standardised ATS boards (Greenhouse, Lever, Ashby); LLM-classifies each new listing for prestige + scope (internships / short-term contracts only); surfaces matches to Telegram via the same `/push` endpoint the signal-brief module uses. A rolling register keeps roles visible after the day they were found. Per-day archive lands in the vault under `Inbox/Job Sift/`.

## Architecture

```
 CEDARS portal        LinkedIn alert emails      Greenhouse / Lever / Ashby
 (httpx + cookies)    (gws CLI → Gmail)          (public JSON APIs)
        └──────────────────┬─────────────────────────────┘
                           ▼
        fetched in parallel under a wall-clock budget
        (a source that cannot look RAISES; it never returns [])
                           ▼
         collapse same-posting duplicates (within a run)
                           ▼
              diff vs per-source seen-set
                           ▼
        classifier — cheap heuristics first, then batched
        Claude CLI calls for what is left. A listing the
        classifier could not judge gets NO verdict: it is
        held back out of the seen-set and retried next run.
                    │                    │
                    │                    ▼
                    │            JSONL classifier log
                    ▼
     surfaced (prestige lane OR floor lane)
                    ▼
        rolling open-roles register
        · collapse duplicates across runs
        · LinkedIn liveness re-check (throttled)
        · age → expire/stale → prune
                    ▼
   /push to claude-telegram bot   +   daily Markdown archive to vault
   + ⚠️ health lines for any source or stage that did NOT run
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
with nothing ever asking whether the role was technical — 20 of 35 entries in a
register snapshot were finance, BD and sales roles admitted exactly that way.

A note on that "35", because a "45" appears a few paragraphs down and the two are
not reconcilable from anything in this repo. Both are counts of the operator's
own register, taken while diagnosing this issue, and neither is checked in — it
is personal application data. They are reported as the separate, unreproducible
snapshots they are; do not read either as a stable denominator, and do not try
to derive one from the other.

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

## "I could not look" is never "there was nothing"

One rule, applied at every stage that can fail, because one signal meaning both
things is what let a dead CEDARS session cookie print *"Surfaced: none today"*
for fifty consecutive days in 2026.

| Stage | Cannot look | Result |
|---|---|---|
| A source fetch | `SourceAuthError` / `SourceFetchError` / budget abandonment | ⚠️ health line in the digest and the archive; failure streak advances; 3 runs → a standing alarm above the digest |
| A source with no config | `SourceNotConfiguredError` | scored as neither success nor failure — nobody asked it anything |
| LinkedIn email parsing | emails carried job cards, none parsed | `SourceFetchError` — a template change is a failure, not a quiet day |
| The classifier | CLI timed out, exited non-zero, or returned no array | **no verdict**: those listings are held OUT of the seen-set, retried next run, and counted in a ⚠️ line |
| A LinkedIn liveness probe | any unreadable response | `UNKNOWN`, which changes nothing at all — not the status, not `last_checked` |
| The open-roles register | `open_roles.json` unparseable | raises; the run dies with the file **intact** rather than overwriting it |

The classifier row is the newest and was the widest hole. `_batch_llm` used to
return a real `skip / out_of_scope` verdict for every listing in a chunk whose
CLI call never completed — a judgement recorded for a call that never happened.
Because `assign_lane` short-circuits on scope, that fabricated verdict also
vetoed the *floor* lane, which is pure string matching and needs no LLM; and
because the ids were already in the seen-set, the listings never came back. A
classifier outage was indistinguishable from a genuinely quiet day, and it
silently consumed the backlog.

Unclassified listings are **not** run through the floor lane, even though it
would work without an LLM. Surfacing one would mean either pushing it again next
run when a real verdict arrives, or consuming it now on a lane that never asked
the scope question it was routed to the LLM for. The invariant is worth more
than the recall: **a listing is either judged or retried — never both, never
neither.**

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

Two phases run in parallel, on the same `run_with_budget` machinery, with
deliberately different concurrency:

- **Fetch** — every source at once, unbounded. The phase costs `max(t)` rather
  than `sum(t)`, and the tasks are one-per-host so there is nothing to throttle.
- **LinkedIn liveness** — at most **2 probes in flight**. Every probe targets the
  same host, and ten concurrent GETs at linkedin.com from one IP invites a 429 or
  an interstitial. That degrades fail-safe (nothing gets retired), but a
  rate-limited run and a run where every role is still open would then look
  identical — so the cap is what keeps the pass answerable, not politeness.

The budget is the ceiling for both: an httpx timeout does **not** bound a
`getaddrinfo` block (on 2026-09-01 a DNS outage produced 135s fetches against a
configured 25s timeout), so it has to be enforced from outside the fetch call.

## The CEDARS session: keeping it alive, and not clobbering it

The CEDARS portal needs HKU SSO, and — confirmed 2026-09-03 — the portal
**intermittently** demands a second factor. An intermittent challenge is worse
than a constant one for automation: a scripted login would pass every time you
tested it and fail silently on the runs where the portal decided to ask. So
credentials stay off the table. We pull the session cookie out of a browser
you're already logged into (`job_sift/refresh_cookie.py`, via
`browser_cookie3`), and then work to keep that session from dying.

### Three states, not two

`job_sift/session.py` answers one question — *does CEDARS still accept this
cookie?* — with **three** answers:

| verdict | what we saw | what we do |
|---|---|---|
| `alive` | 200, no bounce, the results table is on the page | nothing |
| `dead` | redirected to `login.php` / `main.php`, **or** no stored PHPSESSID | walk the browsers |
| `unknown` | *everything else* — transport error, DNS failure, timeout, 5xx, 403, a WAF interstitial, a CEDARS maintenance page | **nothing at all** |

`unknown` is the load-bearing one. A transport failure is evidence about the
*network*, not about the *cookie*, and reading one as the other is the same
silent-zero confusion this codebase spent four branches removing — except in the
expensive direction: it triggers a browser refresh that overwrites a perfectly
good stored cookie. On `unknown` nothing is refreshed, nothing is written, and
no death is recorded.

### Test first, refresh only if genuinely dead

`ensure_session()` runs in that order:

1. stored cookie `alive` → touch nothing
2. stored cookie `unknown` → touch nothing, say so, carry on
3. stored cookie `dead` → walk firefox → chrome → chromium → brave, and keep a
   pulled cookie **only if it then probes `alive`**

That second clause matters as much as the ordering. A successful browser pull
proves a cookie *exists*, not that CEDARS still honours it. The old `./sift`
wrapper pulled unconditionally on every run and wrote whatever it found: on
2026-09-02 it printed *"cookie refreshed from firefox"* and handed the scraper
an expired session, because Firefox's copy was **older** than the one already on
disk.

```bash
.venv/bin/python -m job_sift.session              # probe; refresh only if dead
.venv/bin/python -m job_sift.session --dry-run    # probe only, write nothing
```

Exit codes are the contract `./sift` branches on:

| code | meaning | what `sift` does |
|---|---|---|
| `0` | alive | nothing |
| `1` | CEDARS rejected the session **and** no browser had a working one | the loud log-back-in banner |
| `2` | could not verify it — portal unreachable, unreadable response, `CEDARS_PORTAL_URL` unset, **or the check itself failed** | one quiet stderr line, then carries on |
| anything else | the probe did not run at all (bad venv → `127`, `^C` → `130`) | says so, then carries on |

An unset `CEDARS_PORTAL_URL` is reported as its own thing — *"NOT CHECKED …
CEDARS_PORTAL_URL is unset"* — never as "could not reach the portal". Nothing was
reached because nothing was requested, and unlike an outage it will never fix
itself, so the message names the file to edit.

`1` means one thing only, and that is load-bearing. `write_cookies` can raise
(disk full, read-only mount) *after* a session has been verified alive and
recovered; left uncaught that exits 1, and `sift` would print "SESSION DEAD"
about a session that was fine. So any unexpected failure maps to `2` — the code
that already means "we cannot vouch for this" — never to `1`.

### The keep-alive timer (every 10 minutes)

Evidence says the session dies of **inactivity**, not an absolute cap: the
server is Apache 2.4.6 / PHP 5.4.16, whose `session.gc_maxlifetime` default is
1440s (24 min) of *idle* time with probabilistic GC; no response ever carries a
`Set-Cookie`, so the server never re-issues the value; and every observed death
followed an idle period (one cookie died after ~36h untouched). One request
every 10 minutes keeps that idle timer permanently reset.

```bash
.venv/bin/python -m job_sift.keepalive            # one poke
.venv/bin/python -m job_sift.keepalive --dry-run  # probe only, no cookie, no state
systemctl --user enable --now job-sift-keepalive.timer
```

It runs 144×/day, so it is **quiet by construction** — a steady alive, a steady
dead, and a sustained outage all emit *nothing*. Two rules get it there:

* `job_sift/session.py` logs **only at DEBUG**. It has two callers with opposite
  noise budgets, and a library that picks its own levels serves the once-a-day
  one and drowns the 144-a-day one. The verdict is data; announcing it is the
  caller's job.
* `keepalive` owns the announcement, `INFO` **only on a state change**
  (alive→dead, dead→recovered, the first run of an outage), and pins `httpx` and
  `httpcore` to `WARNING`. httpx logs one INFO line per request carrying the full
  URL; at `basicConfig(level=INFO)` that alone is 144 lines a day, and a dead
  session whose browsers hold a stale cookie measured **15 lines per run, ~2160
  a day**. `-v` restores all of it.

The read surface is `.data/state/cedars_session.json`, not the journal:

```json
{"state": "alive", "last_alive": "2026-09-03T15:41:02+08:00",
 "consecutive_dead": 0, "last_unknown": null, "consecutive_unknown": 0}
```

An `unknown` verdict moves only `last_unknown` / `consecutive_unknown`; `state`,
`last_alive` and `consecutive_dead` come out byte-identical to what went in.

If it turns out an absolute cap *does* exist, none of this is wrong — the
keep-alive extends the session's useful life rather than making it eternal, and
the daily run's banner still tells you when to log back in.

### Two writers, one file

`.data/cookies/cedars.json` now has two writers — the keep-alive timer and the
daily run — so every write goes through `session.write_cookies`: tmp file +
`os.replace` + `chmod 0600`, the same construction as `source_health.save_health`
and for the same reason.

The mode is *also* asserted on every **read** (`session.harden_cookie_file`),
because the write path only runs when a cookie is replaced and the whole point of
this design is that the alive path replaces nothing — a file that arrived at 0644
would otherwise sit there world-readable indefinitely, holding a live credential. A reader sees the old file or the new one, never a half
one; a truncated read would look like "no PHPSESSID", which is a `dead` verdict,
which is the one that reaches for a browser and overwrites things.

### Manual refresh

```bash
.venv/bin/python -m job_sift.refresh_cookie            # pull from Firefox (default)
.venv/bin/python -m job_sift.refresh_cookie --browser chrome
```

Unconditional and unverified by design — this is the "I have just logged in,
take what is in my browser" escape hatch. The scheduled paths do not use it.

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

# Session keep-alive, every 10 min (see ../systemd/job-sift-keepalive.timer)
systemctl --user enable --now job-sift-keepalive.timer
systemctl --user list-timers 'job-sift*'
```

## Phasing

- **v0**: CEDARS only, classifier-driven prestige+scope, Telegram + vault archive — *shipped*
- **v1.1**: LinkedIn job-alert email parsing via gws CLI (`job_sift/sources/linkedin.py`) — *shipped*
- **v1.2**: Greenhouse / Lever / Ashby adapters, cross-run duplicate collapse, the rolling open-roles register, the brand-agnostic floor lane, and LinkedIn liveness re-checks — *shipped*
- **v2**: derive a hardcoded prestige whitelist from ~30 days of classifier_log.jsonl; classifier becomes fallback for ambiguous cases — *not started*
