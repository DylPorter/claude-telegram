# Plan — Keep the CEDARS session alive, and stop clobbering it

## Context

`job-sift` scrapes HKU CEDARS NETJobs using a `PHPSESSID` pulled from a browser
the operator is already logged into. There is no scriptable login: CEDARS auth
is HKU Portal SSO and — **confirmed by the operator on 2026-09-03** — it
intermittently demands a second factor. An intermittent challenge is worse than
a constant one for automation, because a scripted login would pass in testing
and fail silently whenever the portal decides to challenge. So credentials are
off the table; the goal is to stop the session dying in the first place.

**Evidence that expiry is inactivity-based, not an absolute cap:**
- Server is Apache 2.4.6 / **PHP 5.4.16**. PHP's default `session.gc_maxlifetime`
  is 1440s (24 min) of *inactivity*, with probabilistic GC, so sessions routinely
  outlive that by hours.
- Responses carry **no `Set-Cookie`** — the server never re-issues the value.
- Every observed death followed an idle period (one cookie died after ~36h
  untouched; `job-sift/sift`'s own comment says "a few hours").

A probe is currently running every 10 minutes to confirm this. The design below
is correct either way — if an absolute cap exists, the keep-alive extends the
session's useful life rather than making it eternal.

**The bug this creates if not handled:** `job-sift/sift` refreshes the cookie
from the browser on EVERY run (unconditional loop over firefox/chrome/chromium/
brave). With a keep-alive running, that would clobber a healthy stored cookie
with Firefox's stale one. Yesterday it did exactly that — reported
"cookie refreshed from firefox" and handed over an expired session.

## Global Constraints

- Python 3.11+, stdlib + httpx + BeautifulSoup + browser_cookie3. No new deps.
- **Never log, print, or write a `PHPSESSID` value.** Presence and length only.
  This is a hard rule; the operator has burned credentials this way before.
- **A transport failure is NOT evidence the session is dead.** Distinguish
  "bounced to login.php/main.php" (dead) from "could not reach the server"
  (unknown). Reading a network error as "dead" and triggering a browser refresh
  is the same silent-zero mistake this codebase spent four branches removing.
- The cookie file is written by two processes now (keep-alive + daily run) —
  write atomically (tmp + `os.replace`), as `source_health.save_health` does.
- `--dry-run` writes no state.
- Tests fail before, pass after, and must not hit the network.
- Do not touch `signal-brief/` or `hk-events/`.
- Baseline: job-sift 470 passed. It should go UP.

## Task 1 — `ensure_session()`: test first, refresh only if genuinely dead

Add a session module (or extend `refresh_cookie.py`) exposing:

- `check_stored_session() -> Alive | Dead | Unknown` — one cheap GET with the
  stored cookie. Landing on `login.php`/`main.php` is Dead. A 200 carrying the
  results table is Alive. Anything else (transport error, timeout, 5xx, a page
  that is not the portal) is **Unknown**, never Dead.
- `ensure_session()` — the ordering fix:
  1. stored cookie Alive → done, touch nothing
  2. stored cookie Unknown → do NOT refresh; report and let the caller proceed
     with what it has (a network blip must not cost the stored session)
  3. stored cookie Dead → walk the browsers as today; if a pulled cookie tests
     Alive, write it; if none does, report dead and let the caller proceed so
     the other sources still run

Report presence/length only, never a value.

## Task 2 — the keep-alive timer

A `keepalive` entry point that runs `check_stored_session()` and, only on Dead,
attempts the browser refresh. Cheap: one GET, no parsing beyond the liveness
check. Plus a systemd user timer at **every 10 minutes** (`OnUnitActiveSec`),
`Type=oneshot`, with the same hardening the other units got — a `TimeoutStartSec`
well above the request timeout, and NO `Restart=` (the timer is the retry; see
`systemd/job-sift.service`'s comment for why restarts were removed there).

It must be quiet on the happy path — this runs 144×/day and its journal output
should not drown the daily run's. Log at INFO on a state change (alive→dead,
dead→recovered) and DEBUG otherwise.

Persist a small state file (`last_alive` ISO date, `consecutive_dead`) in
`.data/state/` so the daily run and a human can see how long the session has
been up without reading 144 journal lines. Atomic write.

## Task 3 — wire it into `sift`

Replace the unconditional browser loop in `job-sift/sift` with `ensure_session()`.
Keep the loud banner for the genuinely-dead case, and keep the existing behaviour
that the orchestrator still runs so the other sources are unaffected. The current
message tells the reader to log into CEDARS in **Firefox** and why — preserve
that.

## Task 4 — port to the standalone scraper

Port the same capability into the standalone `hku-cedars-scraper` checkout
(a separate git repo, one commit, about to be shared with another student):

- the same test-first `ensure_session()` ordering,
- a `keepalive` CLI subcommand alongside the existing `fetch`/`refresh`,
- and a documented systemd-user-timer snippet **plus** a plain `cron` line in
  the README, since that repo's reader may not use systemd. Do NOT install
  anything there — it is a library-plus-CLI, and the reader chooses.

⚠️ That repo is about to be handed over: nothing personal (no operator name, no
absolute home-directory paths, no vault paths), and no real CEDARS job postings — its
fixtures deliberately keep the portal's structure and substitute the postings.
