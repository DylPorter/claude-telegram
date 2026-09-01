# Plan — Harden the scraper cron fleet (job-sift + hk-events)

## Context

`claude-telegram` is a monorepo of three sibling bots (`signal-brief/`,
`job-sift/`, `hk-events/`) that each POST to the TS bot at
`http://127.0.0.1:7421/push`. Runtime coupling is HTTP only.

On 2026-09-01 BOTH `job-sift.service` and `hk-events.service` entered
`failed`. Root cause, from the journals:

- A transient DNS outage (VPN) made `getaddrinfo` against systemd-resolved
  (`127.0.0.53`) block for ~135s per feed.
- `hk-events/_ical_common.py` sets `_TIMEOUT = 25.0`, but the observed
  failure was 5.4x that. **httpx's timeout does not bound DNS resolution** —
  the block precedes httpx's clock. Adding/lowering an httpx timeout does
  NOT fix this.
- Sources are fetched SERIALLY, so total runtime is `sum(t)` over sources.
  5 sources x ~135s blew `TimeoutStartSec=600`; systemd SIGTERM'd the run
  BEFORE push and state-save.

Secondary defects found while diagnosing:

- `job-sift.service` `ExecStart` calls `orchestrator` directly, bypassing
  `job-sift/sift`, so the daily run NEVER refreshes the CEDARS cookie.
  `refresh_cookie.py` already exists and works on the manual path.
- `hk_events.orchestrator._fetch_all_sources()` returns bare `list[Event]`
  and swallows every exception. There is no per-source error map, so no
  source-health data exists for hk-events at all. job-sift already returns
  `(listings, errors: dict[str, str])`.
- Source failures degrade silently to the human. CEDARS auth died 5 Jul 2026
  and 50 consecutive digests printed "Surfaced: none today" when they meant
  "I could not look."

## Global Constraints

- Python 3.11+, stdlib + httpx. No new runtime dependencies.
- NEVER log, print, or write a cookie/`PHPSESSID` value. Length only.
- Preserve existing behaviour: per-source failures must still degrade to a
  partial run, never kill the whole run.
- `--dry-run` must continue to write NO state and push NOTHING.
- Match each module's existing conventions; job-sift and hk-events are
  siblings and should end up structurally similar, not identical.
- Every task ships tests that fail before the change and pass after.
- Do not touch `signal-brief/`.

## Task 1 — hk-events: add a per-source error map

`hk_events/orchestrator.py::_fetch_all_sources` currently returns
`list[Event]` and logs-and-drops exceptions.

Change it to return `tuple[list[Event], dict[str, str]]`, mirroring
`job_sift/orchestrator.py::_fetch_all_sources` exactly (same error-string
shape: `f"fetch failed: {exc}"`). Thread the error map through `run()` into
whatever renders the digest and the vault archive, so hk-events surfaces a
source-health line the way job-sift already does.

Read `job_sift/orchestrator.py::_fetch_all_sources` first and follow it.

Tests: a source that raises is recorded in the map and does not abort the
run; a healthy source still returns its events; the map is empty when all
sources succeed.

## Task 2 — Concurrent source fetching with a hard wall-clock budget

In BOTH `job_sift/orchestrator.py` and `hk_events/orchestrator.py`, replace
the serial source loop with a `concurrent.futures.ThreadPoolExecutor` so
sources run in parallel. Runtime becomes `max(t)` not `sum(t)`.

Add a total wall-clock budget, env-overridable, default 240 seconds:
`JOB_SIFT_FETCH_BUDGET_S` / `HK_EVENTS_FETCH_BUDGET_S`. Any source not
finished when the budget expires is recorded in the error map as a timeout
and its partial result discarded; the run CONTINUES with whatever landed.
This is the guard that survives a DNS block, since httpx timeouts do not
bound `getaddrinfo`.

Preserve CEDARS's existing greedy pagination and its pre-loaded seen-set.

Tests: sources run concurrently (a slow source does not delay a fast one's
completion); a source exceeding the budget lands in the error map; the run
still returns the results of sources that DID finish.

## Task 3 — systemd hardening + wire the cookie refresh

In `systemd/job-sift.service` and `systemd/hk-events.service`:
- Raise `TimeoutStartSec` to 900 as a backstop behind Task 2's budget.
- Add `Restart=on-failure`, `RestartSec=300`, and cap retries at 3 so a
  transient VPN/DNS blip self-heals instead of skipping the day. Type is
  `oneshot`; use the correct directives for that type and verify with
  `systemd-analyze verify`.
- Point job-sift's `ExecStart` at `job-sift/sift` (which already walks
  firefox -> chrome -> chromium -> brave) so the daily run refreshes the
  CEDARS cookie. Keep `WorkingDirectory`.

THEN VERIFY EMPIRICALLY, and report the result plainly:
`refresh_cookie.py` is documented as interactive-only on the assumption that
cookie-DB decryption needs an unlocked keyring. That is true for Chromium.
Firefox on Linux stores cookies in a PLAINTEXT `cookies.sqlite`, and
browser_cookie3's AES/SecretService/KWallet code paths live only in the
Chromium classes. So the Firefox path SHOULD work headless — but this is
source-inference, NOT verified at runtime.

Run the refresh under a stripped environment (no `DBUS_SESSION_BUS_ADDRESS`)
to emulate the systemd context and report whether it succeeds. If it does
NOT work headless, do NOT force it: revert `ExecStart` and report that a
separate user-session-scoped refresh timer is needed instead. Report the
finding either way. Never print a cookie value — report presence and length.

Tests: `systemd-analyze verify` passes on both units.

## Task 4 — Source staleness alarm (escalate >3 consecutive failing days)

The load-bearing fix. Both bots must escalate a persistently dead source into
the Telegram push, not just the markdown archive.

Persist a per-source consecutive-failure counter in each module's existing
`.data/state/` directory. On each run: a source in the error map increments
its counter; a source that succeeds resets it to 0.

When any source's counter is `>= 3`, prepend a loud line to the Telegram
push, naming the source and the consecutive-day count, e.g.
`WARNING cedars: no successful fetch in 7 days - auth may be dead`.
This must fire even on an otherwise-empty digest, overriding hk-events'
`HK_EVENTS_PUSH_EMPTY=0` silence, because "nothing today" and "I could not
look" must never look the same to the reader.

`--dry-run` must not persist counters.

Tests: counter increments on failure and resets on success; the alarm fires
at exactly 3 and not at 2; the alarm reaches the push on an empty digest;
`--dry-run` writes no counter state.
