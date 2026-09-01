"""Per-source consecutive-failure counters, and the staleness alarm they drive.

Why this module exists
----------------------
The CEDARS scraper's auth died silently on 2026-07-05. For the next fifty
consecutive runs the digest printed "No new prestige matches today" — when what
it actually meant was "I could not look". The operator kept reading those
digests and believed there were no jobs.

`render._fmt_source_health` already makes a SINGLE bad run visible. This module
makes a PERSISTENT one impossible to scroll past: it remembers, across runs,
how long each source has been failing, and escalates a source that has not
returned anything for `ALARM_THRESHOLD` consecutive runs into a loud line at
the TOP of the push — including on a run that surfaced nothing, where the
alarm matters most.

Counting semantics: RUNS, not days
----------------------------------
`consecutive_failures` counts consecutive *runs* in which the source landed in
the error map. It is deliberately not a wall-clock day count: the state records
one integer per source, and a manual re-run, a skipped day, or a systemd
`Persistent=true` catch-up all move that integer without a day passing. So the
alarm says "consecutive failed runs" and quotes `last_success` as a real date,
rather than dressing a run count up as a day count. The runs happen to be
daily, but the counter does not know that and does not pretend to.

Absence is not failure
----------------------
Only sources that were actually ATTEMPTED this run get a record. A source that
is commented out of the fetch list (hk-events has three) or removed entirely is
pruned from the state rather than accruing failures forever — you cannot be
stale if nobody asked you for anything.

State shape (`.data/state/source_health.json`):

    {"cedars": {"consecutive_failures": 3,
                "last_success": "2026-07-04",
                "last_failure": "2026-09-01",
                "last_error": "session expired — re-export cookies"}}
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path

from job_sift import config

log = logging.getLogger(__name__)

# Escalate at three consecutive failed runs. One failure is noise (a flaky
# feed, a transient 502) and already shows up in the per-run health line; three
# in a row is a broken source.
ALARM_THRESHOLD = 3

# Error strings are echoed into Telegram and the vault, so keep them short and
# bounded — an adapter that stringifies a whole response body must not paste it
# into the state file or the digest.
_MAX_ERROR_CHARS = 200

STATE_FILENAME = "source_health.json"


def _path() -> Path:
    # Resolved through the config MODULE (not a from-import) so a test can
    # point STATE_DIR at a tmp_path without touching the real state.
    return config.STATE_DIR / STATE_FILENAME


def load_health() -> dict[str, dict]:
    """Read the counters. A missing or corrupt file is not fatal — a broken
    state file must never be the thing that kills the daily run."""
    p = _path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except Exception as exc:
        log.warning("failed to load source health: %s — starting fresh", exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("source health file is not an object — starting fresh")
        return {}
    out: dict[str, dict] = {}
    for name, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        out[str(name)] = _normalize(rec)
    return out


def _normalize(rec: Mapping) -> dict:
    try:
        failures = int(rec.get("consecutive_failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    return {
        "consecutive_failures": max(0, failures),
        "last_success": rec.get("last_success"),
        "last_failure": rec.get("last_failure"),
        "last_error": rec.get("last_error"),
    }


def save_health(health: Mapping[str, dict]) -> None:
    _path().write_text(json.dumps(health, indent=2, sort_keys=True))


def update_health(
    health: Mapping[str, dict],
    *,
    attempted: Iterable[str],
    errors: Mapping[str, str],
    today: date,
) -> dict[str, dict]:
    """Fold one run's outcome into the counters. PURE — persists nothing.

    Being pure is what makes `--dry-run` honest: the caller computes the
    would-be state, renders the alarm from it, and only writes it on a real run.

    A source in `errors` increments; a source that was attempted and is not in
    `errors` resets to 0. Anything not attempted is dropped from the returned
    map (see "Absence is not failure" above).
    """
    # Union rather than plain `attempted`: a name in the error map ran by
    # definition, so it can never be treated as "not attempted".
    names = list(dict.fromkeys([*attempted, *errors]))
    out: dict[str, dict] = {}
    for name in names:
        prev = _normalize(health.get(name) or {})
        if name in errors:
            out[name] = {
                "consecutive_failures": prev["consecutive_failures"] + 1,
                "last_success": prev["last_success"],
                "last_failure": today.isoformat(),
                "last_error": str(errors[name])[:_MAX_ERROR_CHARS],
            }
        else:
            out[name] = {
                "consecutive_failures": 0,
                "last_success": today.isoformat(),
                "last_failure": prev["last_failure"],
                "last_error": None,
            }
    return out


def stale_sources(
    health: Mapping[str, dict], *, threshold: int = ALARM_THRESHOLD
) -> list[tuple[str, dict]]:
    """Sources at or past the threshold, worst streak first then alphabetical."""
    hits = [
        (name, rec)
        for name, rec in health.items()
        if _normalize(rec)["consecutive_failures"] >= threshold
    ]
    return sorted(hits, key=lambda kv: (-_normalize(kv[1])["consecutive_failures"], kv[0]))


def _short(msg) -> str:
    """First clause of an error message — the health line's convention."""
    return str(msg or "").split(" — ")[0].split(". ")[0].strip() or "no error recorded"


def render_alarm(
    health: Mapping[str, dict], *, threshold: int = ALARM_THRESHOLD
) -> str | None:
    """The loud bubble prepended to the push. None when nothing is stale.

    Deliberately blunt about what the rest of the digest does NOT mean, because
    the failure this exists to prevent was a reader trusting "none today".
    """
    stale = stale_sources(health, threshold=threshold)
    if not stale:
        return None
    lines = ["🚨 *SOURCE DEAD — this digest is INCOMPLETE*"]
    for name, rec in stale:
        rec = _normalize(rec)
        runs = rec["consecutive_failures"]
        last_ok = rec["last_success"] or "never"
        lines.append(
            f"• *{name}* — {runs} consecutive failed runs "
            f"(last successful fetch: {last_ok}) — {_short(rec['last_error'])}"
        )
    lines.append(
        "_Nothing from these sources reached this digest. "
        '"None today" below is NOT authoritative until they are fixed._'
    )
    return "\n".join(lines)
