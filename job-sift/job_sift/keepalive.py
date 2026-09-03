"""Keep the CEDARS PHPSESSID from idling out. Runs every 10 minutes.

WHY THIS EXISTS AT ALL. CEDARS auth is HKU Portal SSO and — confirmed by the
operator on 2026-09-03 — it INTERMITTENTLY demands a second factor. That is
worse for automation than a constant challenge, because a scripted login would
pass every time it was tested and fail silently on the runs where the portal
decided to ask. So credentials are off the table and the only lever left is to
stop the session dying in the first place.

The evidence says the death is INACTIVITY, not an absolute cap:

  * the server is Apache 2.4.6 / PHP 5.4.16, whose `session.gc_maxlifetime`
    default is 1440 seconds of IDLE time, garbage-collected probabilistically —
    which is why sessions routinely outlive it by hours;
  * no response ever carries a `Set-Cookie`, so the server is not re-issuing the
    value on a schedule of its own;
  * every observed death followed an idle period (one cookie died after ~36h
    untouched).

One request every 10 minutes therefore keeps the idle timer permanently reset.
If an absolute cap does turn out to exist, this design is still correct — it
extends the session's useful life rather than making it eternal, and the daily
run's banner still tells the operator when to log back in.

IT MUST BE QUIET. 144 runs a day share a journal with the daily sift, so a
routine "still alive" cannot be a printed line. INFO is reserved for a STATE
CHANGE (alive -> dead, dead -> recovered, and the first time the portal becomes
unreachable); everything else is DEBUG.

THE STATE FILE is the read surface, not the journal — `.data/state/
cedars_session.json` says how long the session has been up without anyone
grepping 144 log lines. Written atomically, same as `source_health`.

UNKNOWN CHANGES NOTHING ABOUT THE SESSION. A transport failure leaves `state`,
`last_alive` and `consecutive_dead` exactly as they were; only the separate
reachability fields move. A network blip must not be able to record a death,
and it must not be able to trigger a browser refresh — `ensure_session` owns
that half, and this module simply never asks it to do otherwise.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from job_sift import config
from job_sift.session import ALIVE, DEAD, UNKNOWN, ensure_session

log = logging.getLogger(__name__)

STATE_FILENAME = "cedars_session.json"

_FIELDS = (
    "state",
    "last_alive",
    "last_dead",
    "consecutive_dead",
    "last_unknown",
    "consecutive_unknown",
)


def _path() -> Path:
    # Resolved through the config MODULE, not a from-import, so a test can point
    # STATE_DIR at a tmp_path without touching the real state.
    return config.STATE_DIR / STATE_FILENAME


def _blank() -> dict:
    return {
        "state": None,
        "last_alive": None,
        "last_dead": None,
        "consecutive_dead": 0,
        "last_unknown": None,
        "consecutive_unknown": 0,
    }


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def load_state() -> dict:
    """Read the state file. A missing or corrupt one is not fatal.

    It is also not evidence: a reset reads as "we have never seen this session
    alive", which is exactly what an unwritten file means, and nothing here
    escalates off that. The cookie is the thing that matters and it lives
    elsewhere.
    """
    p = _path()
    if not p.exists():
        return _blank()
    try:
        raw = json.loads(p.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("cedars session state unreadable (%s) — starting fresh", exc)
        return _blank()
    if not isinstance(raw, dict):
        log.warning("cedars session state is not an object — starting fresh")
        return _blank()
    out = _blank()
    for key in _FIELDS:
        if key in raw:
            out[key] = raw[key]
    out["consecutive_dead"] = _as_int(out["consecutive_dead"])
    out["consecutive_unknown"] = _as_int(out["consecutive_unknown"])
    if out["state"] not in (ALIVE, DEAD, None):
        out["state"] = None
    return out


def save_state(state: Mapping) -> None:
    """Atomic write (tmp + `os.replace`), same construction as `source_health`."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(dict(state), f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def next_state(prior: Mapping, verdict: str, *, now: datetime) -> dict:
    """Fold one probe's verdict into the state. PURE — persists nothing.

    Being pure is what makes `--dry-run` honest and what makes the UNKNOWN rule
    testable as a property of a function rather than as an absence of a call:
    given ANY prior and an UNKNOWN verdict, `state`, `last_alive`, `last_dead`
    and `consecutive_dead` come out byte-identical to what went in.
    """
    out = dict(_blank())
    out.update({k: prior.get(k, out[k]) for k in _FIELDS})
    out["consecutive_dead"] = _as_int(out["consecutive_dead"])
    out["consecutive_unknown"] = _as_int(out["consecutive_unknown"])
    stamp = now.isoformat(timespec="seconds")

    if verdict == ALIVE:
        out["state"] = ALIVE
        out["last_alive"] = stamp
        out["consecutive_dead"] = 0
        out["consecutive_unknown"] = 0
    elif verdict == DEAD:
        out["state"] = DEAD
        out["last_dead"] = stamp
        out["consecutive_dead"] += 1
        out["consecutive_unknown"] = 0
    else:
        # UNKNOWN. The session fields are NOT touched — see the module
        # docstring. Only reachability is recorded, and it is recorded under
        # names that cannot be mistaken for a death.
        out["last_unknown"] = stamp
        out["consecutive_unknown"] += 1
    return out


def _log_verdict(prior: Mapping, verdict: str, report) -> None:
    """INFO only on a state change. This runs 144x/day; see the docstring."""
    was = prior.get("state")
    if verdict == ALIVE:
        if report.refreshed_from:
            log.info(
                "CEDARS session recovered — refreshed from %s after %d dead check(s)",
                report.refreshed_from,
                _as_int(prior.get("consecutive_dead")),
            )
        elif was != ALIVE:
            log.info("CEDARS session is alive (was: %s)", was or "unknown")
        else:
            log.debug("CEDARS session still alive")
    elif verdict == DEAD:
        if was != DEAD:
            # `rejected` is called out separately because it is a different
            # problem with a different fix: a browser that HAS a CEDARS cookie
            # the portal refuses means the browser's own login has lapsed, not
            # that no cookie could be found. `ensure_session` logs neither at
            # INFO any more, so this line is where they surface.
            stale = (
                f" {', '.join(report.rejected)} had a cookie CEDARS refused."
                if report.rejected
                else ""
            )
            log.info(
                "CEDARS session is DEAD and no browser had a working one (tried: %s).%s "
                "Log into https://web2.cedars.hku.hk/jobs/ in Firefox — the daily "
                "sift will keep running the other sources meanwhile.",
                ", ".join(report.tried) or "none",
                stale,
            )
        else:
            log.debug(
                "CEDARS session still dead (%d consecutive)",
                _as_int(prior.get("consecutive_dead")) + 1,
            )
    else:
        if _as_int(prior.get("consecutive_unknown")) == 0:
            log.info(
                "CEDARS session state unknown — could not reach the portal. "
                "Stored cookie left alone; nothing recorded against the session."
            )
        else:
            log.debug(
                "CEDARS session still unreachable (%d consecutive)",
                _as_int(prior.get("consecutive_unknown")) + 1,
            )


def run_once(*, dry_run: bool = False, first_browser: str | None = None, now: datetime | None = None):
    """One keep-alive cycle. Returns `(verdict, report, new_state)`.

    `ensure_session` is what does the probing, so the ordering guarantee — test
    the stored cookie first, refresh only on a genuine DEAD, never on UNKNOWN —
    is inherited rather than re-implemented here.
    """
    now = now or datetime.now().astimezone()
    prior = load_state()
    report = ensure_session(first_browser=first_browser, dry_run=dry_run)
    verdict = report.state
    _log_verdict(prior, verdict, report)
    new_state = next_state(prior, verdict, now=now)
    if not dry_run:
        save_state(new_state)
    return verdict, report, new_state


#: Third-party loggers that are chatty at INFO. httpx emits one line PER
#: REQUEST carrying the full URL; httpcore is worse at DEBUG.
_NOISY_LIBRARIES = ("httpx", "httpcore")


def configure_logging(*, verbose: bool) -> None:
    """Set up logging for a keep-alive run. Extracted so a test can assert on it.

    TWO THINGS HAPPEN HERE, and both were bugs before they were features.

    1. THE ROOT LEVEL IS SET EXPLICITLY, not left to `basicConfig`.
       `basicConfig` is a NO-OP when the root logger already has a handler —
       which is true under pytest, under an embedding host, and under anything
       that configured logging first. So `-v` silently did nothing in exactly
       the situations where you would reach for it, and a test could not
       observe the real thresholds at all.

    2. THE NOISY LIBRARIES ARE PINNED TO WARNING unless `-v`.
       Our own code is disciplined about staying quiet (see `session.py`'s
       module docstring), but a third-party logger under the root logger is not,
       and `level=INFO` hands it the same threshold. httpx alone is one line per
       probe: 144 a day on the happy path, and a steady dead session multiplies
       that by the browser walk — measured at 15 lines a run, ~2160 a day. That
       drowns the daily sift's journal, which is the thing this unit shares.

       Set on the LOGGER, not on a handler, so the record is never constructed —
       which is also what lets a test see production's real output by just
       reading `caplog.records`.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger().setLevel(level)
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.NOTSET if verbose else logging.WARNING)


EXIT_ALIVE = 0
EXIT_DEAD = 1
EXIT_UNKNOWN = 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="job_sift.keepalive",
        description="Poke the CEDARS session so it does not idle out.",
    )
    parser.add_argument("--browser", help="browser to try first when the session is dead")
    parser.add_argument(
        "--dry-run", action="store_true", help="probe only — write no cookie and no state"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    args = parser.parse_args(argv)

    configure_logging(verbose=args.verbose)

    try:
        verdict, _report, _state = run_once(dry_run=args.dry_run, first_browser=args.browser)
    except Exception:  # noqa: BLE001
        # Same reasoning as `session.main`: exit 1 means "the session is dead",
        # and an unexpected internal failure is not that. `exception` rather
        # than `error` because unlike the 144-runs-a-day happy path, this is
        # genuinely unexpected and the traceback is the whole value.
        log.exception("keep-alive failed unexpectedly")
        return EXIT_UNKNOWN
    return {ALIVE: EXIT_ALIVE, DEAD: EXIT_DEAD}.get(verdict, EXIT_UNKNOWN)


if __name__ == "__main__":
    sys.exit(main())
