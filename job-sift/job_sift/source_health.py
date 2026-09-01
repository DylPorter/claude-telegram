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
Only sources that REPORTED AN OUTCOME this run — a completed fetch or a
recorded error — get a record. A source that
is commented out of the fetch list (hk-events has three) or removed entirely is
pruned from the state rather than accruing failures forever — you cannot be
stale if nobody asked you for anything.

State shape (`.data/state/source_health.json`):

    {"cedars": {"consecutive_failures": 3,
                "last_success": "2026-07-04",
                "last_failure": "2026-09-01",
                "last_error": "session expired — re-export cookies",
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
        # Date this source's record was created. It bounds what the state can
        # honestly claim: with no `last_success`, all we know is "not since
        # `first_seen`" — NOT "never". See _fmt_last_success.
        "first_seen": rec.get("first_seen"),
    }


def save_health(health: Mapping[str, dict]) -> None:
    """Write the counters ATOMICALLY (tmp file + os.replace).

    `dedupe.save_seen` still uses a plain `write_text`, and that asymmetry is
    deliberate rather than an oversight either file gets away with. Both can be
    truncated by a SIGTERM mid-write (TimeoutStartSec, or a machine shutdown),
    but the two truncations fail in opposite directions:

      * a half-written seen-set reads as fewer seen ids, so the next run
        re-notifies listings the reader already got. LOUD, self-correcting, and
        the reader can see it happened;
      * a half-written health file reads as corrupt, `load_health` resets to
        empty, and a dead source's streak silently restarts from zero — buying
        it another `ALARM_THRESHOLD` runs of looking healthy. That is the exact
        failure this whole module exists to make impossible.

    Silence is the failure mode worth paying for; a duplicate digest is not.
    So this file gets tmp-file + `os.replace` — a reader sees the old file or
    the new one, never a half one — and `save_seen` is left alone. (Ordering
    helps too: the orchestrator commits the seen-set immediately after a
    successful push, so its exposure window is as narrow as it can be.)
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
    succeeded: Iterable[str],
    errors: Mapping[str, str],
    today: date,
) -> dict[str, dict]:
    """Fold one run's outcome into the counters. PURE — persists nothing.

    Being pure is what makes `--dry-run` honest: the caller computes the
    would-be state, renders the alarm from it, and only writes it on a real run.

    Success must be POSITIVE, never inferred
    ----------------------------------------
    `succeeded` is the set of sources that actually completed a fetch this run.
    It is emphatically NOT "the enabled list minus the error map". This function
    used to take `attempted=enabled_sources()` — a static list — and treat
    "attempted and absent from `errors`" as proof of success. That inference is
    only as good as the adapters' willingness to raise, and they were not
    willing: `httpx` wraps `socket.gaierror` in `ConnectError`, the feed and ATS
    adapters caught it per-endpoint and returned `[]`, and a total network
    outage therefore produced an EMPTY error map. Every source was then scored a
    success — a 12-run failure streak reset to 0 and today stamped as
    `last_success`. A fabricated fact, written to disk, later rendered to a human.

    So the two signals are now both explicit and both come from what the fetch
    phase actually observed: `succeeded` resets to 0 and records the date;
    `errors` increments. A source in NEITHER set was not attempted this run and
    is dropped from the returned map — the "Absence is not failure" pruning
    above. Crucially, it is dropped, not reset: this function can no longer
    manufacture a success for a source that never reported one.

    (`succeeded` and `errors` overlapping would be a caller bug; `errors` wins,
    because claiming a failure we saw beats claiming a success we inferred.)
    """
    # `errors` first: a name in both sets must land on the failure branch.
    names = list(dict.fromkeys([*errors, *succeeded]))
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


def dropped_while_stale(
    prior: Mapping[str, dict],
    current: Mapping[str, dict],
    *,
    threshold: int = ALARM_THRESHOLD,
) -> list[tuple[str, dict]]:
    """Sources pruned this run that were carrying a standing alarm.

    Pruning is right — you cannot be stale if nobody asked you anything — but it
    is also the one way to make a live alarm disappear without fixing anything:
    delete the source's config key and the record goes with it, silently.

    Two things make that worse than it sounds. The digest cannot show it, because
    `render._fmt_source_health` is driven by the error map and a pruned source is
    in neither map. And the prune RESETS THE RE-ARM CLOCK: restoring the config
    gives `update_health` no `prev`, so `first_seen` becomes today and
    `last_success` is None — a source that was twelve runs dead comes back
    looking brand new and needs another `threshold` runs before it can shout
    again. That is the exact "buying it another ALARM_THRESHOLD runs of looking
    healthy" failure `save_health` was hardened against, reached through a YAML
    edit instead of a truncated file.

    So the drop gets one line in the push. Worst streak first, then alphabetical
    — same ordering as `stale_sources`.
    """
    hits = [
        (name, _normalize(rec))
        for name, rec in prior.items()
        if name not in current and _normalize(rec)["consecutive_failures"] >= threshold
    ]
    return sorted(hits, key=lambda kv: (-kv[1]["consecutive_failures"], kv[0]))


def render_drop_notice(
    prior: Mapping[str, dict],
    current: Mapping[str, dict],
    *,
    threshold: int = ALARM_THRESHOLD,
) -> str | None:
    """The bubble for those drops. None when nothing alarming was pruned.

    Deliberately NOT an alarm: nothing failed this run, and dressing a
    configuration change up as a failure is the same fabrication in the other
    direction. It states what was dropped, what streak it was carrying, and what
    would explain it — and leaves the call to the reader.

    Per-run and self-clearing by construction. The record is gone from the state
    file after this run, so `prior` no longer holds it and the next run says
    nothing. A deliberate disable costs exactly one line, once; it cannot nag,
    and it accrues nothing, so no schema change is needed to carry it.
    """
    gone = dropped_while_stale(prior, current, threshold=threshold)
    if not gone:
        return None
    return "\n".join(
        f"ℹ️ *{name}* — dropped from health tracking (no config this run) "
        f"while carrying a {rec['consecutive_failures']}-run failure streak. "
        "If that wasn't deliberate, its config is missing."
        for name, rec in gone
    )


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
