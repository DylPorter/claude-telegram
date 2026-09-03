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
