"""Concurrent source fetching under a hard wall-clock budget.

Mirrored in `hk_events/concurrency.py` — the two bots are deliberate
copies of each other and neither depends on the other.

Sources are fetched in parallel and the whole phase is bounded by a hard
wall-clock budget, so the fetch step costs max(t) with a ceiling, instead of
sum(t) with none.

WHY THIS EXISTS
---------------
2026-09-01: a transient systemd-resolved outage made every feed fetch block
~135 seconds. Sources were fetched serially, so the fetch phase cost sum(t)
and blew the unit's TimeoutStartSec=600 — systemd SIGTERM'd the run before it
pushed or saved state, and both bots' units went to `failed`.

Two things were wrong, and only fixing both helps:

1. Serial fetching. Running sources concurrently turns sum(t) into max(t).

2. No ceiling. `hk_events/sources/_ical_common.py` already set `_TIMEOUT =
   25.0`, and the observed failure still took 135s — 5.4x the configured
   timeout — because the block was in `getaddrinfo()` against systemd-resolved
   (127.0.0.53), which happens BEFORE httpx starts its clock. **No httpx
   timeout bounds this failure.** The ceiling therefore has to be a wall-clock
   budget enforced from OUTSIDE the fetch call.

WHY DAEMON THREADS AND NOT ThreadPoolExecutor
---------------------------------------------
A thread blocked in `getaddrinfo` cannot be stopped. `Future.cancel()` only
cancels work that has not started yet, and there is no way to interrupt a
blocking resolver call from another thread. So "enforcing" the budget can only
mean abandoning the straggler and moving on.

That rules out `ThreadPoolExecutor`, whose workers are non-daemon:
`concurrent.futures`' own atexit hook joins them, and on top of that CPython's
interpreter shutdown waits for every non-daemon thread (3.14 moved that wait
into C — there is no longer even a private table to unregister from). Spending
240s enforcing a budget and then blocking another two minutes at exit would
leave the unit in exactly the state this is trying to get out of. Daemon
threads are dropped for free when the process exits, so the budget is a real
ceiling on the process, not just on this function.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, wait

log = logging.getLogger(__name__)


def _settle(fut: "Future", fn: Callable[[], object]) -> None:
    """Run `fn` and park its outcome on `fut`. Never raises into the thread."""
    if not fut.set_running_or_notify_cancel():
        return
    try:
        fut.set_result(fn())
    except BaseException as exc:  # noqa: BLE001 — re-raised to the caller via fut.result()
        fut.set_exception(exc)


def run_with_budget(
    tasks: Sequence[tuple[str, Callable[[], object]]],
    budget_s: float,
    *,
    thread_name_prefix: str = "fetch",
) -> tuple[list[tuple[str, "Future"]], list[str]]:
    """Run every `(name, fn)` concurrently, bounded by `budget_s` wall-clock seconds.

    Returns `(settled, abandoned)`:

    - `settled`   — `(name, future)` pairs in the SAME order as `tasks`, for
      every task that finished inside the budget. `future.result()` re-raises
      whatever the callable raised, so each caller keeps its own per-source
      exception handling (job-sift, for instance, still distinguishes
      `SourceAuthError` from a generic failure).
    - `abandoned` — names of tasks still running when the budget expired.
      Their partial results are discarded and the caller must record them as
      failed sources. Their threads keep running — a thread blocked in
      `getaddrinfo` cannot be stopped — but they are daemon threads, so
      nothing waits for them, here or at process exit.

    A task that finishes in the gap between the budget expiring and the
    bookkeeping below is counted as settled. That race only ever converts a
    would-be timeout into a real result, so it is left as-is.
    """
    if not tasks:
        return [], []

    pending: list[tuple[str, Future]] = []
    for name, fn in tasks:
        fut: Future = Future()
        threading.Thread(
            target=_settle,
            args=(fut, fn),
            name=f"{thread_name_prefix}-{name}",
            daemon=True,
        ).start()
        pending.append((name, fut))

    # The budget is enforced HERE, outside the fetch call — that is the whole
    # point. `wait` returns early once everything lands, so the happy path
    # costs max(t), not the budget.
    wait([fut for _, fut in pending], timeout=budget_s)

    settled: list[tuple[str, Future]] = []
    abandoned: list[str] = []
    for name, fut in pending:
        if fut.done():
            settled.append((name, fut))
        else:
            log.error(
                "%s: still running after the %.0fs fetch budget — abandoning it",
                name,
                budget_s,
            )
            abandoned.append(name)
    return settled, abandoned
