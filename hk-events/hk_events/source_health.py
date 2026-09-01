"""Per-source consecutive-failure counters, and the staleness alarm they drive.

Mirrors job-sift/job_sift/source_health.py exactly — same state shape, same
threshold, same counting semantics. Kept as a sibling copy rather than a shared
package because the two bots have no shared runtime library and each owns its
own `.data/state/` directory.

Why this module exists
----------------------
Its sibling bot's CEDARS scraper lost its auth silently on 2026-07-05 and, for
fifty consecutive runs, printed "no matches today" when what it meant was "I
could not look". hk-events has the same failure mode and one extra hazard:
`HK_EVENTS_PUSH_EMPTY=0` suppresses the Telegram push entirely on an empty
digest, which is exactly the run where a dead feed most needs to be shouted
about. The alarm this module produces overrides that gate (see
`orchestrator.run`).

`render._fmt_source_health` already makes a SINGLE bad run visible. This module
makes a PERSISTENT one impossible to scroll past: it remembers, across runs,
how long each source has been failing, and escalates a source that has not
returned anything for `ALARM_THRESHOLD` consecutive runs into a loud line at
the TOP of the push.

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
Only sources that were actually ATTEMPTED this run get a record. The three
adapters commented out of `orchestrator._source_tasks` (aitinkerers, cyberport,
startmeuphk) are never attempted, so they can never accrue failures or alarm —
you cannot be stale if nobody asked you for anything.

State shape (`.data/state/source_health.json`):

    {"meetup": {"consecutive_failures": 3,
                "last_success": "2026-08-29",
                "last_failure": "2026-09-01",
                "last_error": "fetch failed: connection reset",
                "first_seen": "2026-06-01"}}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path

from hk_events import config

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
        # Date this source's record was created. It bounds what the state can
        # honestly claim: with no `last_success`, all we know is "not since
        # `first_seen`" — NOT "never". See _fmt_last_success.
        "first_seen": rec.get("first_seen"),
    }


def save_health(health: Mapping[str, dict]) -> None:
    """Write the counters ATOMICALLY (tmp file + os.replace).

    `dedupe.save_seen` gets away with a plain write_text; this file does not.
    The whole plan exists because systemd SIGTERM'd a run mid-flight, and a
    truncated state file here reads as corrupt, resets to empty, and silently
    buys a dead source three more runs of silence — the exact failure being
    fixed. A reader either sees the old file or the new one, never a half one.
    """
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(health, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave a stray tmp file behind; the old state stays intact.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        first_seen = prev["first_seen"] or today.isoformat()
        if name in errors:
            out[name] = {
                "consecutive_failures": prev["consecutive_failures"] + 1,
                "last_success": prev["last_success"],
                "last_failure": today.isoformat(),
                "last_error": str(errors[name])[:_MAX_ERROR_CHARS],
                "first_seen": first_seen,
            }
        else:
            out[name] = {
                "consecutive_failures": 0,
                "last_success": today.isoformat(),
                "last_failure": prev["last_failure"],
                "last_error": None,
                "first_seen": first_seen,
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


def _fmt_last_success(rec: Mapping) -> str:
    """Say only what the state can support about the last good fetch.

    A bare "never" is a fabricated absolute: a source that worked for months
    renders as never-succeeded after a corrupt-state reset, or after being
    pruned and re-added. In the one message whose entire job is to be scrupulous
    about the difference between "nothing found" and "I could not look", that is
    the same error in miniature.

    So: a real date when we have one; "not since <first_seen>" when the record
    is continuous back to its creation and holds no success (true, and bounded
    by what we actually observed); "unknown" when we cannot even establish that.
    """
    if rec.get("last_success"):
        return f"last successful fetch: {rec['last_success']}"
    if rec.get("first_seen"):
        return f"no successful fetch since tracking began {rec['first_seen']}"
    return "last successful fetch: unknown"


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
        lines.append(
            f"• *{name}* — {runs} consecutive failed runs "
            f"({_fmt_last_success(rec)}) — {_short(rec['last_error'])}"
        )
    lines.append(
        "_Nothing from these sources reached this digest. "
        '"None today" below is NOT authoritative until they are fixed._'
    )
    return "\n".join(lines)
