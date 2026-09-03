# Report — Keep the CEDARS session alive, and stop clobbering it

**Status: complete.** Both repos implemented, tested, and committed. Nothing pushed.
No systemd unit was installed, enabled or started — unit *files* only.

| | commit | tests |
|---|---|---|
| worktree, branch `cedars-keepalive` | `6d4287e` (code) + docs commit | **470 → 524** |
| `hku-cedars-scraper` (separate repo) | `202ba52` | **138 → 195** |

---

## What was built

### Task 1 — `ensure_session()`: test first, refresh only if genuinely dead

`job-sift/job_sift/session.py`. Three verdicts, not two:

| verdict | evidence | action |
|---|---|---|
| `alive` | 200, not bounced, `table.tablesorter` parsed out of the body | nothing |
| `dead` | terminal path is `login.php` / `main.php`, **or** no stored `PHPSESSID` | walk the browsers |
| `unknown` | *everything else* | **nothing at all** |

`unknown` covers transport errors, DNS failures, timeouts, 5xx, 403, WAF
interstitials, and a CEDARS-served maintenance page (portal chrome, no table).
Liveness is decided by **parsing** for the table, not by substring — a stray
`.tablesorter` in a script tag cannot fake a session.

A missing cookie file is `dead`, not `unknown`, and the distinction is
deliberate: that is a positive *local* observation that there is no session to
test, not a transport failure being reinterpreted. The right response — go find
one — is the same as for an expired one.

`ensure_session()` orders it: `alive` → touch nothing; `unknown` → touch
nothing and say so; `dead` → walk firefox → chrome → chromium → brave, and keep
a pulled cookie **only if it then probes `alive`**.

### Task 2 — the keep-alive

`job_sift/keepalive.py` + `systemd/job-sift-keepalive.{service,timer}`.

* `OnUnitActiveSec=10min` (measured from the end of the last run — what the
  server times is the *gap*), `OnBootSec=2min`, no `Persistent=true` (a missed
  poke cannot be caught up; the point of the request is *when* it happens).
* `Type=oneshot`, `TimeoutStartSec=180` against a 20s probe timeout, and
  **no `Restart=`**, per `job-sift.service`'s comment. Two of its three
  arguments apply verbatim; the unit carries a third of its own — retrying a
  rejected cookie against an SSO portal several times a minute is a pattern
  someone blocks.
* `systemd-analyze verify` → clean on both files.
* **Quiet by construction.** 144 runs/day share a journal with the daily sift,
  so `INFO` is reserved for a state change (alive→dead, dead→recovered, first
  unreachable) and everything else is `DEBUG`. Pinned by a test that asserts
  `caplog.records == []` on the steady-state happy path *and* on a
  steady-state dead one.
* State lives in `.data/state/cedars_session.json` (atomic write), so "how long
  has the session been up" is a file read, not 144 journal lines.

### Task 3 — wired into `sift`

The unconditional firefox→chrome→chromium→brave loop is gone. `sift` now calls
`python -m job_sift.session` and branches on its exit code: `0` alive (quiet),
`2` unknown (one quiet stderr line — a network blip is not a call to action),
`1` dead (the loud banner, preserving the "log into CEDARS in **Firefox**, and
here is why" wording). All three branches were exercised under `set -euo
pipefail`; all three fall through to the orchestrator, so the other sources run
regardless.

### Task 4 — the standalone repo

`cedars/session.py`, a `keepalive` CLI subcommand alongside `fetch`/`refresh`,
and README docs covering **both** a systemd user timer and a plain `cron` line
(its reader may not use systemd). Both README unit snippets were extracted and
run through `systemd-analyze verify` — clean once the placeholder path is made
real; the only complaint against the snippet as written is that
`%h/src/hku-cedars-scraper` does not exist on this machine, which is the point
of a placeholder. Nothing was installed there.

`--no-refresh` is provided for a headless box, where the recovery walk is pure
noise and a cookie store is stale by construction. Exit `3` = dead (log back
in), exit `4` = unreachable.

**Cleanliness (grepped before committing):** no operator name, no `/home/...`
paths, no vault paths, no `job-sift` / `claude-telegram` / `signal-brief`
references in any tracked file. The fixtures' real-structure/synthetic-content
split is untouched and its own guard test still passes. **One residual:** the
`LICENSE` copyright line names the author. That is pre-existing, is what a MIT
licence requires, and stripping it would be wrong — flagged rather than edited.

---

## The two guarantees, and how they are pinned

### `unknown` never triggers a refresh or records a death

* `test_unknown_never_touches_a_browser_or_the_cookie` — a recording stub
  proves `pulls == []`, and the cookie file is asserted **byte-identical**.
* `test_unknown_moves_no_session_field_whatever_the_prior` — a **property**
  over three prior state shapes (blank, alive, 7-runs-dead), not one example:
  `state`, `last_alive`, `last_dead` and `consecutive_dead` all come out
  identical. Only `last_unknown` / `consecutive_unknown` move, and they are
  named so they cannot be mistaken for a death.
* `test_run_once_unknown_records_no_death_end_to_end` — the same through the
  *real* `ensure_session` and `check_session`, with only the socket layer
  replaced by a `ConnectError`.
* Four parametrised transport failures (`ConnectError`, `ReadTimeout`,
  `ConnectTimeout`, `RemoteProtocolError`) and six non-200 statuses all assert
  `unknown`, never `dead`.

It was written property-first on purpose: this bug only surfaces on the run
where the network happened to be down, so an example-based test would pass for
years while the guarantee rotted.

### Stored-cookie-first ordering, verified against the live session

Two probes against the real portal (not a loop):

```
check_stored_session -> alive
ensure_session       -> alive | refreshed_from: None | tried: [] | wrote: False
cookie file BYTE-IDENTICAL after ensure_session: True
```

`tried: []` is the assertion that matters — the browser walk was **not entered**
because the stored cookie tested alive. The old code would have pulled from
Firefox and overwritten at this exact point.

The `dead` path was confirmed live too, with a never-issued session id: CEDARS
bounced it to `login.php` and the classifier read `dead` off the real redirect,
not off a fixture. The standalone port was verified live as well
(`keepalive --dry-run --no-refresh` → `session alive`, exit 0, nothing written).

### No `PHPSESSID` value is ever logged, printed, or written to a report

Reports carry names and character counts only (`PHPSESSID (26 chars)`). Pinned
on the *rejection* path specifically, since that is the one branch that formats
a pulled cookie into a message — `assert stale not in caplog.text`.

### Two writers, one file

`session.write_cookies` is tmp-file + `os.replace` + `chmod 0600` *before* the
rename, following `source_health.save_health`. A truncated read would look like
"no `PHPSESSID`", which is the `dead` verdict, which is the one that reaches for
a browser — so the atomicity is load-bearing, not hygiene. Tested: no stray tmp
file, mode `0600`, and a failed write leaves the old file intact.

---

## Concerns

1. **The inactivity hypothesis is well-evidenced but not yet confirmed.** PHP
   5.4.16's 1440s idle default, the absent `Set-Cookie`, and every observed
   death following an idle period all point one way, but the 10-minute probe
   has not yet run long enough to prove it. If an absolute cap exists, the
   design still holds — it extends the session's life rather than making it
   eternal — but the timer will not be the fix it looks like, and the honest
   read is "unconfirmed" until a session survives well past 36h.

2. **The keep-alive can only ever be as good as Firefox.** Recovery still
   depends on Firefox holding a live CEDARS session. If the operator logs out
   there, `dead` becomes permanent until a human intervenes — the keep-alive
   makes that state *visible* quickly, it does not remove it.

3. **`consecutive_dead` has no alarm on it.** The state file records the streak
   and the daily run still shows the banner, but nothing escalates a session
   that has been dead for six hours. Deliberately out of scope, and
   `source_health` already alarms on CEDARS after three failed *runs* — but
   that is daily-run granularity, so a session dying at 09:05 is invisible
   until the next morning. Worth wiring the state file into the morning brief.

4. **The daily run and the keep-alive can race on the cookie file.** The write
   is atomic, so nobody reads a half file, but a `dead`-path recovery in both
   processes at once means two browser walks and a last-writer-wins. Harmless
   (both write a cookie they verified) and cheap to leave, but it is a real
   interleaving rather than an impossible one.

5. **`job-sift` has no venv in the worktree.** Tests ran against the parent
   repo's `.venv` with `PYTHONPATH` pinned to the worktree, verified by
   printing `job_sift.__file__` before every run. Nothing imported the parent
   copy, but this is an easy thing to get silently wrong on the next pass —
   clear `__pycache__` first, as this run did.

6. **Unrelated, fixed in passing:** the standalone README documented
   `cedars fetch -v`, which argparse rejected as a usage error — the very first
   example a new reader copies. Fixed with a shared parent parser using an
   `argparse.SUPPRESS` default, so `-v` works on either side of the subcommand
   without the subparser clobbering a top-level one back to `False`.

---

# Addendum — review fixes

All four Importants and all five Minors are fixed, and **each fix was confirmed
by deleting it and watching a test fail**. Three of the four Importants were
tests that did not test what they claimed; the report's earlier claim that
quietness was "pinned by a test" was false, and is retracted here.

| | commit | tests |
|---|---|---|
| worktree `cedars-keepalive` | see below | **524 → 535** |
| `hku-cedars-scraper` | `b6d7c01` | **195 → 202** |

## IMPORTANT 1 — the happy path is now actually quiet

Two rules, because one was not enough:

* **`session.py` logs only at DEBUG.** It has two callers with opposite noise
  budgets — `sift` once a day, `keepalive` 144 times — and a library that picks
  its own levels serves the first and drowns the second. The verdict is now
  *data*; announcing it is the caller's job.
* **`configure_logging` pins `httpx` and `httpcore` to WARNING** unless `-v`,
  and sets the root level **explicitly**. `basicConfig` is a no-op once the root
  logger has a handler, so `-v` had been silently doing nothing under pytest and
  under any embedding host — a latent bug found while making this testable.

Measured after, at production level, against the live portal: **0 lines.**

Mutations, each run separately against the full suite:

| mutation | result |
|---|---|
| drop the `httpx`/`httpcore` pinning | **4 failed** |
| `UNKNOWN` branch logs INFO again | **2 failed** |
| rejected-pull logs INFO again (the 15-line case) | **1 failed** |
| exhausted-walk summary logs WARNING again | **1 failed** |

The second one initially **survived** — the two rewritten tests covered steady
*alive* and steady *dead* but not a steady *outage*, which is a third steady
state and the one a weekend of downtime hits (432 identical lines). Added
`test_a_sustained_outage_emits_nothing_after_the_first_run` plus its converse,
`test_the_first_run_of_an_outage_says_so_exactly_once`, and it now fails.

## IMPORTANT 2 — the quiet tests now exercise the code that logs

Both previously patched `ensure_session` away. They now drive the real
`ensure_session` → `check_session` → real `httpx.Client`, with only the socket
layer mocked, and install production's real thresholds **on the loggers** — a
record below its logger's level is never constructed, so `caplog.records` holds
exactly what would reach the journal, httpx included. Each also asserts the
expensive path really ran (`report.tried == all four browsers`,
`report.rejected == all four`), so it cannot pass by doing nothing.

## IMPORTANT 3 — `dry_run` is pinned in both repos

The old test served the table page to *every* request, so the stored cookie
probed alive and the function returned at step 1. The transport now
distinguishes the two cookies (stored bounces, browser's is accepted), and the
test asserts `refreshed_from == "firefox"` and `tried == ["firefox"]` to prove
the branch was entered before asserting nothing was written.

Deleting `if not dry_run:` → **job-sift 1 failed**, **standalone 1 failed**.
Previously: 524/524 and 195/195 green.

## IMPORTANT 4 — exit 1 means one thing again

`session.main` catches any unexpected exception and returns **2**, never 1, with
the reasoning written at the catch site: exit 1 is what `sift` answers with the
log-back-in banner, and a failed cookie write happens *after* a session has been
verified alive. `sift`'s comment block and its exit-code table were updated to
match. `classify_response`'s BeautifulSoup parse is also wrapped — a parser that
chokes has said only that it could not read the page, which is UNKNOWN by
definition. `ensure_session`'s "Never raises" docstring is corrected to state
exactly what can still escape (the cookie write) and why that specific case is
*not* collapsible into a verdict: ALIVE would lie about what the next fetch does,
DEAD would lie about what we observed.

| mutation | result |
|---|---|
| map internal failure to `EXIT_DEAD` | **1 failed** |
| remove the `try/except` entirely | **1 failed** |
| standalone: route to `EXIT_AUTH` (3) instead of 1 | **1 failed** |

The standalone had no `except Exception` at all, so its documented exit 1
("unexpected internal error") was unreachable and a traceback escaped instead.
It is now real — and deliberately not 3, which would send the reader to log in
again to fix a full disk.

## Minors

* **Cookie mode re-asserted on every read** (`harden_cookie_file`, both repos).
  The write path only runs when a cookie is *replaced*, and the alive path
  replaces nothing, so a file that arrived at 0644 would stay world-readable for
  the life of the session. Logs INFO when it actually changes something — a
  one-shot state change — and is silent thereafter. *Note:* the live file read
  0644 at review time but 0600 by the time I re-checked, so something rewrote it
  in between; the on-read assertion is what makes the mode hold regardless of
  who wrote it last. Mutation: drop it → **2 failed**.
* **`./sift --dry-run` now forwards to the probe.** `"$@"` reached only the
  orchestrator, so a dry run could rewrite the cookie file. Verified by
  execution across three argument shapes.
* **`--no-refresh` no longer prints an empty list.** `describe()` returns "no
  browser was consulted (recovery disabled)" instead of a bare "session DEAD",
  and the `in any of: ` warning is suppressed when nothing was consulted — with
  a companion test proving a *genuine* exhausted walk still names all four
  browsers. Mutation: revert → **1 failed**.
* **Fragile log-substring assertions replaced** with structural ones: exactly
  one INFO record, from `keepalive`'s own logger, naming the browser and the
  streak it ended. A rephrase no longer breaks them; a lost state-change rule
  still does.
* **Timer comment added** noting the two timers free-run independently and
  collide roughly one day in ten, why that is harmless (atomic write, both
  processes write only a cookie they verified), and that it is deliberately not
  coordinated.

## Not changed, as directed

The race (correctly assessed harmless), `refresh_cookie.refresh()` writing
unverified (deliberate, documented, and no scheduled path calls it), and the
fixtures.

## Standing concerns

Concerns 1–3 and 5 from the original report are unchanged. **Concern 4 (the
race) is downgraded** — it is noted in the timer file now, so a future reader
will not have to rediscover it. One new item: `session.py`'s DEBUG-only rule is
a convention, not a mechanism. Nothing stops the next person adding a `log.info`
there, and the three steady-state tests would catch it only for paths they
happen to cover. A lint rule, or routing this module through a logger that
caps at DEBUG, would make it structural.

---

# Addendum 2 — final review pass

| | commit | tests |
|---|---|---|
| worktree `cedars-keepalive` | see below | **535 → 539** |
| `hku-cedars-scraper` | `d394bda` | **202 → 212** |

## 1. Standalone `ensure_session` — "Never raises" corrected

The claim sat at `cedars/session.py:256` while a test added in the same commit
asserted `pytest.raises(OSError)` from that exact function. Corrected the way
job-sift's was: it now names the one escape (the cookie write) and why that case
alone is not collapsible into a verdict — ALIVE would lie about what the next
fetch does (the stored cookie is still the dead one), DEAD would lie about what
we observed. The two remaining "Never raises" in that file, on `check_session`
and `harden_cookie_file`, are both true and were left.

**Exit codes, measured rather than asserted**, and the README table now says
this:

| situation | code |
|---|---|
| alive / recovered and verified | `0` |
| CEDARS rejected it, no browser had one | `3` |
| no cookie file at all | `3` |
| portal unreachable (DNS, timeout, 5xx) | `4` |
| portal answered something unparseable | `4` |
| cookie write failed (disk full) | `1` |

Your two measurements reproduce exactly. The behaviour was already right; only
the documentation was silent.

## 2. Unconfigured is no longer reported as unreachable

You were right that this was self-inflicted: demoting the unset-URL warning to
DEBUG left `keepalive`'s UNKNOWN line as the only surfacing, and it blamed the
network for something the network never saw.

`SessionReport` gains a `reason` — `UNREACHABLE` or `UNCONFIGURED`. Whether the
portal is *configured* is a local fact knowable without asking anything, so
`ensure_session` settles it directly rather than threading a reason back out of
the probe; `check_session`'s return type stays a plain verdict.

**Executed, three consecutive runs each, at production level:**

```
CEDARS_PORTAL_URL UNSET   exits=[2,2,2]   1 line over 3 runs
  INFO CEDARS session NOT CHECKED — CEDARS_PORTAL_URL is unset, so no request
       was made. Set it in job-sift/.env. The stored cookie is untouched and
       nothing has been recorded against the session.

PORTAL UNREACHABLE        exits=[2,2,2]   1 line over 3 runs
  INFO CEDARS session state unknown — could not reach the portal, or it
       answered with something unreadable. Stored cookie left alone; nothing
       recorded against the session.
```

Different messages; the unconfigured one no longer claims anything was reached,
and it names the file to edit, because unlike an outage it will never fix
itself. Still said once, then silence.

One thing fell out of this that was not asked for: a **change of reason** now
counts as a state change. Otherwise a portal that was merely unreachable and has
since become unconfigured (someone cleared `.env`) keeps climbing
`consecutive_unknown` in silence, never saying the one thing that explains it.
`last_unknown_reason` is persisted for that.

Mutations: collapse the two messages → **3 failed**; drop reason-change
detection → **1 failed**; stop classifying the reason → **4 failed**.

## 3. Standalone quieted, same class as I1

`cedars/session.py` now logs only at DEBUG, on the same rule as job-sift: the
verdict is data, announcing it belongs to the caller, and a library that picks
its own levels serves the once-a-day reader and drowns the 144-a-day one.

The stdout line needed a different answer. job-sift's state-change rule needs a
state file, which I deliberately kept out of this repo — and without one the CLI
genuinely *cannot* tell "changed" from "unchanged", only "is". So rather than
fake it: **`--quiet` prints nothing and lets the exit code carry the verdict**,
which is what a scheduler reads anyway. Both README scheduler snippets use it,
which also retires the `>/dev/null` that would have hidden more than the status
line. Errors still reach stderr — pinned by
`test_quiet_still_reports_an_unexpected_failure`, because a cron job that goes
silent on a real failure is the thing this project exists to prevent.

Steady state under `--quiet`: **0 lines, every state.** Mutations: library logs
WARNING again → **1 failed**; `--quiet` stops suppressing → **3 failed**;
`--quiet` swallows a real failure → **2 failed**.

## Minors

* **`sift`'s `*)` narrowed to `1)`.** You were right that the comment
  overclaimed. Driven across `0/1/2/3/127/130`: only `1` prints the banner; a
  `127` from a missing venv now says "the probe did not run" rather than sending
  the operator to re-log-into a portal that was never contacted — the same
  overloading, one level further out.
* **Third UNKNOWN cause added** to `summary()`; it enumerated two and there are
  three, and the unparseable-body one is what `sift`'s own comment block cites.
* **Tautology removed** — the assert above it was doing the work.
* **`log.error` → `log.exception`** in `session.main`, matching `keepalive.main`'s
  own argument that the traceback is the whole value. The standalone's blanket
  `except Exception` now prints the traceback under `-v`, which previously had no
  escape at all.

## Standing concerns

Unchanged from the first addendum. The one worth repeating: `session.py`'s
DEBUG-only rule is a **convention, not a mechanism**, in both repos now. Nothing
stops the next person adding a `log.info`, and the steady-state tests catch it
only on paths they happen to cover. A lint rule, or a module-scoped logger
capped at DEBUG, would make it structural — worth doing if a third caller ever
appears.
