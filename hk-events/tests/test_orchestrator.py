"""Tests for `_fetch_all_sources`: the per-source error map, and the
concurrent fetch bounded by a hard wall-clock budget.

Error map — mirrors the behaviour job-sift already has: one dead source must
never abort the run, and its failure must be visible (not silently swallowed)
so the digest + vault archive can surface a source-health line.

Budget — before this change the sources were fetched serially and nothing
bounded an individual fetch, so the 2026-09-01 DNS outage cost sum(t) with no
ceiling and systemd SIGTERM'd the run before push + state-save. The tests pin
both halves of the fix: sources run in PARALLEL, and a source that blows the
budget is abandoned, recorded in the error map as a failure (not a quiet
zero), and does not stop the run from returning what the others brought back.

Timing assertions use a generous margin: they only need to tell max(t) from
sum(t), not to measure anything precisely.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timezone

import pytest

from hk_events.concurrency import run_with_budget

from hk_events import config, orchestrator
from hk_events.schema import Event

# Every fetcher sleeps this long in the concurrency test.
_SLOW = 0.3


def _event(source: str, external_id: str) -> Event:
    return Event(
        source=source,
        external_id=external_id,
        title=f"{source} event {external_id}",
        url=f"https://example.com/{source}/{external_id}",
        start=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )


def test_failing_source_recorded_in_error_map_and_does_not_abort_run(monkeypatch):
    def _boom():
        raise RuntimeError("connection reset")

    monkeypatch.setattr(orchestrator.meetup, "fetch_meetup_events", _boom)
    monkeypatch.setattr(
        orchestrator.luma, "fetch_luma_events", lambda: [_event("luma", "1")]
    )

    events, errors, _ok = orchestrator._fetch_all_sources()

    assert errors == {"meetup": "fetch failed: connection reset"}
    # The run continues — luma's events still come back despite meetup's crash.
    assert [e.external_id for e in events] == ["1"]


def test_healthy_source_still_returns_its_events(monkeypatch):
    monkeypatch.setattr(
        orchestrator.meetup, "fetch_meetup_events", lambda: [_event("meetup", "m1")]
    )
    monkeypatch.setattr(
        orchestrator.luma, "fetch_luma_events", lambda: [_event("luma", "l1")]
    )

    events, errors, _ok = orchestrator._fetch_all_sources()

    assert errors == {}
    assert {e.external_id for e in events} == {"m1", "l1"}


def test_error_map_empty_when_all_sources_succeed(monkeypatch):
    monkeypatch.setattr(orchestrator.meetup, "fetch_meetup_events", lambda: [])
    monkeypatch.setattr(orchestrator.luma, "fetch_luma_events", lambda: [])

    events, errors, _ok = orchestrator._fetch_all_sources()

    assert events == []
    assert errors == {}


def test_both_sources_failing_are_both_recorded(monkeypatch):
    def _meetup_boom():
        raise ValueError("bad feed xml")

    def _luma_boom():
        raise RuntimeError("timeout")

    monkeypatch.setattr(orchestrator.meetup, "fetch_meetup_events", _meetup_boom)
    monkeypatch.setattr(orchestrator.luma, "fetch_luma_events", _luma_boom)

    events, errors, _ok = orchestrator._fetch_all_sources()

    assert events == []
    assert errors == {
        "meetup": "fetch failed: bad feed xml",
        "luma": "fetch failed: timeout",
    }


def _sleeper(source: str, seconds: float):
    def _fetch():
        time.sleep(seconds)
        return [_event(source, "1")]

    return _fetch


def _patch_sources(monkeypatch, *, meetup=None, luma=None) -> None:
    monkeypatch.setattr(
        orchestrator.meetup, "fetch_meetup_events", meetup or (lambda: [])
    )
    monkeypatch.setattr(orchestrator.luma, "fetch_luma_events", luma or (lambda: []))


def test_sources_are_fetched_concurrently(monkeypatch):
    """Both sources sleeping _SLOW must finish in ~_SLOW, not 2x _SLOW."""
    _patch_sources(
        monkeypatch, meetup=_sleeper("meetup", _SLOW), luma=_sleeper("luma", _SLOW)
    )

    started = time.monotonic()
    events, errors, _ok = orchestrator._fetch_all_sources()
    elapsed = time.monotonic() - started

    assert errors == {}
    assert {e.source for e in events} == {"meetup", "luma"}
    assert elapsed < _SLOW * 1.8, f"sources look serial: {elapsed:.2f}s"


def test_a_slow_source_does_not_delay_a_fast_one(monkeypatch):
    """A fast source must FINISH early, not queue behind the slow one.

    meetup is first in the fetch order, so under the old serial loop luma
    could not even start until meetup had returned. Asserting on when luma
    finished (rather than on the total) is what distinguishes max(t) from
    sum(t) with only two sources.
    """
    finished_at: dict[str, float] = {}
    started = time.monotonic()

    def _timed(source: str, seconds: float):
        def _fetch():
            time.sleep(seconds)
            finished_at[source] = time.monotonic() - started
            return [_event(source, "1")]

        return _fetch

    _patch_sources(
        monkeypatch,
        meetup=_timed("meetup", _SLOW * 3),
        luma=_timed("luma", 0.0),
    )

    events, errors, _ok = orchestrator._fetch_all_sources()

    assert errors == {}
    assert {e.source for e in events} == {"meetup", "luma"}
    assert finished_at["luma"] < _SLOW, (
        "the fast source waited on the slow one: "
        f"luma finished at {finished_at['luma']:.2f}s"
    )


def test_source_over_budget_lands_in_the_error_map(monkeypatch):
    """A source still running at the budget is a FAILURE, not a quiet zero."""
    monkeypatch.setenv(config.FETCH_BUDGET_ENV, "0.3")
    _patch_sources(
        monkeypatch,
        meetup=_sleeper("meetup", 30),
        luma=lambda: [_event("luma", "l1")],
    )

    started = time.monotonic()
    events, errors, _ok = orchestrator._fetch_all_sources()
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"budget not enforced: {elapsed:.2f}s"
    assert set(errors) == {"meetup"}
    # Same shape as a crashed source, so the health line and any per-source
    # staleness counter built on this map count it as a failure.
    assert errors["meetup"].startswith("fetch failed: ")
    assert "budget" in errors["meetup"]
    # ...and the run still returns what the healthy source brought back.
    assert [e.external_id for e in events] == ["l1"]


def test_over_budget_partial_result_is_discarded(monkeypatch):
    monkeypatch.setenv(config.FETCH_BUDGET_ENV, "0.2")
    _patch_sources(monkeypatch, meetup=_sleeper("meetup", 30))

    events, errors, _ok = orchestrator._fetch_all_sources()

    assert events == []
    assert set(errors) == {"meetup"}


def test_abandoned_source_runs_on_a_daemon_thread(monkeypatch):
    """The abandoned worker must not hold the interpreter open at exit.

    A thread blocked in getaddrinfo cannot be cancelled or joined, so the only
    way the budget is a real ceiling on the PROCESS (and not just on
    _fetch_all_sources) is for the worker to be a daemon thread.
    """
    monkeypatch.setenv(config.FETCH_BUDGET_ENV, "0.2")
    release = threading.Event()
    seen: list[threading.Thread] = []

    def _blocked():
        seen.append(threading.current_thread())
        release.wait(30)
        return []

    _patch_sources(monkeypatch, meetup=_blocked)
    try:
        orchestrator._fetch_all_sources()
        assert seen, "the blocked fetcher never started"
        assert seen[0].is_alive()
        assert seen[0].daemon is True
    finally:
        release.set()


class TestFetchBudgetConfig:
    def test_defaults_to_240s(self, monkeypatch):
        monkeypatch.delenv(config.FETCH_BUDGET_ENV, raising=False)
        assert config.fetch_budget_s() == 240.0

    def test_env_override_is_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv(config.FETCH_BUDGET_ENV, "12.5")
        assert config.fetch_budget_s() == 12.5

    @pytest.mark.parametrize("raw", ["", "  ", "abc", "0", "-1"])
    def test_unusable_values_fall_back_to_the_default(self, monkeypatch, raw):
        monkeypatch.setenv(config.FETCH_BUDGET_ENV, raw)
        assert config.fetch_budget_s() == config.FETCH_BUDGET_DEFAULT_S


# ---------------------------------------------------------------------------
# Commit ordering: the seen-set is what makes the push non-repeatable, so it
# must be persisted as soon as delivery has settled — before anything that can
# still fail. Previously the archive write sat in between, so an OSError there
# exited non-zero AFTER a successful push and BEFORE the seen-set landed, and a
# re-run reclassified and re-pushed the same events.
# ---------------------------------------------------------------------------


class _OrderHarness:
    def __init__(self, monkeypatch, tmp_path, *, archive_raises=False, surface=True):
        from hk_events.schema import RelevanceResult

        self.calls: list[str] = []
        self.saved: dict[str, dict] = {}
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setenv("HK_EVENTS_STUB", "0")
        monkeypatch.setattr(config, "assert_required", lambda: None)

        event = _event("meetup", "m1")
        monkeypatch.setattr(
            orchestrator, "_fetch_all_sources", lambda: ([event], {}, ["meetup"])
        )
        monkeypatch.setattr(
            orchestrator,
            "filter_due",
            # The REAL seen-set shape: {source: {external_id: {"stages": [...],
            # "tag": ...}}}. It used to be faked as {"meetup": {"m1"}} — a set —
            # which passed only because nothing downstream read inside it. The
            # register does (it reads the cached room tag), so the fixture has
            # to tell the truth about the shape.
            lambda evs: (
                [(e, orchestrator.STAGE_NEW, None) for e in evs],
                {"meetup": {"m1": {"stages": [orchestrator.STAGE_NEW], "tag": None}}},
            ),
        )
        monkeypatch.setattr(orchestrator, "log_classification", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "record_verdict", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "sync_events", lambda *a, **k: None)
        monkeypatch.setattr(
            orchestrator,
            "classify",
            lambda e: RelevanceResult("founder_ai" if surface else "drop", "test verdict"),
        )
        monkeypatch.setattr(orchestrator, "push_messages", lambda msgs: self.calls.append("push"))

        def save_seen(source, seen):
            self.calls.append("save_seen")
            self.saved[source] = seen

        def write_archive(*a, **k):
            self.calls.append("write_archive")
            if archive_raises:
                raise OSError("vault volume is full")

        monkeypatch.setattr(orchestrator, "save_seen", save_seen)
        monkeypatch.setattr(orchestrator, "write_archive", write_archive)


def test_seen_set_is_committed_between_the_push_and_the_archive(monkeypatch, tmp_path):
    h = _OrderHarness(monkeypatch, tmp_path)
    assert orchestrator.run() == 0
    assert h.calls == ["push", "save_seen", "write_archive"]
    assert h.saved == {"meetup": {"m1": {"stages": [orchestrator.STAGE_NEW], "tag": None}}}


def test_an_archive_failure_can_no_longer_undeliver_the_seen_set(monkeypatch, tmp_path):
    """The re-push bug: the run still dies, but the events stay delivered."""
    h = _OrderHarness(monkeypatch, tmp_path, archive_raises=True)
    with pytest.raises(OSError):
        orchestrator.run()
    assert h.calls == ["push", "save_seen", "write_archive"]
    assert h.saved == {"meetup": {"m1": {"stages": [orchestrator.STAGE_NEW], "tag": None}}}


def test_a_deliberate_empty_day_silence_still_commits_the_seen_set(monkeypatch, tmp_path):
    """HK_EVENTS_PUSH_EMPTY=0 suppresses the push. That is a settled delivery
    decision, not a failure — the seen-set must still advance, or the same
    dropped events are reclassified every day forever."""
    monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
    h = _OrderHarness(monkeypatch, tmp_path, surface=False)
    assert orchestrator.run() == 0
    assert h.calls == ["save_seen", "write_archive"]
    assert h.saved == {"meetup": {"m1": {"stages": [orchestrator.STAGE_NEW], "tag": None}}}


def test_dry_run_still_writes_nothing(monkeypatch, tmp_path):
    h = _OrderHarness(monkeypatch, tmp_path)
    assert orchestrator.run(dry_run=True) == 0
    assert h.calls == []
    assert not list(tmp_path.iterdir())


# ---------------------------------------------------------------------------
# `succeeded` wiring: run() must forward what the fetch phase OBSERVED.
#
# `update_health` is only as honest as what run() hands it, and its unit tests
# call it directly — so re-introducing the original defect at the CALL SITE
# (`succeeded=enabled_sources()`, a static list) left every other test in both
# suites green. These read the state file back off disk after a real run(),
# which is the only place that inference shows up.
# ---------------------------------------------------------------------------


_SEEDED_HEALTH = {
    name: {
        "consecutive_failures": 12,
        "last_success": "2026-08-20",
        "last_failure": "2026-08-31",
        "last_error": "fetch failed: connection reset",
        "first_seen": "2026-06-01",
    }
    for name in ("meetup", "luma")
}


def _health_after_run(monkeypatch, tmp_path, *, fetched, seeded=None):
    """Run the real run() over a stubbed fetch phase; return the saved state."""
    from hk_events import source_health

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setenv("HK_EVENTS_STUB", "0")
    monkeypatch.setattr(config, "assert_required", lambda: None)
    state = tmp_path / source_health.STATE_FILENAME
    state.write_text(json.dumps(_SEEDED_HEALTH if seeded is None else seeded))

    monkeypatch.setattr(orchestrator, "_fetch_all_sources", lambda: fetched)
    monkeypatch.setattr(orchestrator, "push_messages", lambda msgs: None)
    monkeypatch.setattr(orchestrator, "write_archive", lambda *a, **k: None)

    assert orchestrator.run() == 0
    return json.loads(state.read_text())


class TestSucceededComesFromTheFetchPhase:
    """Not from `enabled_sources()`. The list of sources a run ATTEMPTS is a
    different fact from the list that REPORTED, and conflating them is the
    original bug: it manufactured a success for every source during a total
    outage. Since an unconfigured source now lands in neither returned set, the
    two lists differ on healthy runs too — a static list would fabricate there
    as well."""

    def test_a_run_that_observed_nothing_records_nothing(self, monkeypatch, tmp_path):
        """Both feeds unconfigured/unattempted: no successes, no errors."""
        saved = _health_after_run(monkeypatch, tmp_path, fetched=([], {}, []))

        assert saved == {}, "sources that reported nothing must be pruned, not scored"

    def test_no_source_is_stamped_successful_today_without_reporting(
        self, monkeypatch, tmp_path
    ):
        """The fabricated fact itself, named: today as `last_success`."""
        saved = _health_after_run(monkeypatch, tmp_path, fetched=([], {}, []))

        assert date.today().isoformat() not in json.dumps(saved)

    def test_only_the_sources_that_reported_get_a_record(self, monkeypatch, tmp_path):
        """A mixed run: one succeeded, one reported nothing at all."""
        saved = _health_after_run(monkeypatch, tmp_path, fetched=([], {}, ["luma"]))

        assert set(saved) == {"luma"}
        assert saved["luma"]["consecutive_failures"] == 0
        assert saved["luma"]["last_success"] == date.today().isoformat()

    def test_a_failing_source_still_increments(self, monkeypatch, tmp_path):
        saved = _health_after_run(
            monkeypatch, tmp_path, fetched=([], {"meetup": "fetch failed: 403"}, [])
        )

        assert set(saved) == {"meetup"}
        assert saved["meetup"]["consecutive_failures"] == 13
        assert saved["meetup"]["last_success"] == "2026-08-20"

    def test_dry_run_forwards_the_same_signal_but_persists_nothing(
        self, monkeypatch, tmp_path
    ):
        """--dry-run must not write state — including a pruning write."""
        from hk_events import source_health

        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setenv("HK_EVENTS_STUB", "0")
        monkeypatch.setattr(config, "assert_required", lambda: None)
        state = tmp_path / source_health.STATE_FILENAME
        state.write_text(json.dumps(_SEEDED_HEALTH))

        monkeypatch.setattr(orchestrator, "_fetch_all_sources", lambda: ([], {}, []))
        monkeypatch.setattr(
            orchestrator, "push_messages", lambda msgs: pytest.fail("dry-run pushed")
        )
        monkeypatch.setattr(
            orchestrator, "write_archive", lambda *a, **k: pytest.fail("dry-run wrote")
        )

        assert orchestrator.run(dry_run=True) == 0
        assert json.loads(state.read_text()) == _SEEDED_HEALTH


# ---------------------------------------------------------------------------
# `max_in_flight`: the same budget machinery, throttled.
#
# The fetch phase wants every source at once — one task per host, nothing to
# throttle. A phase whose tasks all hit the SAME host does not: a same-host re-check pass issues up
# to ten GETs at one origin, and firing them together invites a 429 or an
# interstitial. That degrades fail-safe, but "rate-limited" and "nothing
# changed" then produce identical output, which is the ambiguity this codebase
# keeps deleting. These pin the cap, and pin that `None` leaves the fetch phase
# exactly as it was.
# ---------------------------------------------------------------------------


class TestMaxInFlight:
    @staticmethod
    def _probe(peak, live, lock, gate):
        def fn():
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            gate.wait(1.0)
            with lock:
                live[0] -= 1
            return "ok"

        return fn

    def _measure(self, n, max_in_flight):
        import threading as _t

        peak, live, lock = [0], [0], _t.Lock()
        gate = _t.Event()
        tasks = [(f"t{i}", self._probe(peak, live, lock, gate)) for i in range(n)]

        # Let the first wave enter, sample the peak, then release everyone.
        def release():
            _t.Timer(0.2, gate.set).start()

        release()
        settled, abandoned = run_with_budget(
            tasks, 5.0, thread_name_prefix="test", max_in_flight=max_in_flight
        )
        return peak[0], settled, abandoned

    def test_ten_same_host_tasks_run_at_most_two_at_a_time(self):
        peak, settled, abandoned = self._measure(10, 2)
        assert peak <= 2, f"peak concurrency was {peak}"
        assert abandoned == []
        assert [f.result() for _, f in settled] == ["ok"] * 10

    def test_none_leaves_the_fetch_phase_unthrottled(self):
        """The regression guard for the other caller. All ten must overlap."""
        peak, settled, _ = self._measure(10, None)
        assert peak == 10
        assert len(settled) == 10

    def test_results_and_order_are_identical_either_way(self):
        def ok(v):
            return lambda: v

        tasks = [(f"t{i}", ok(i)) for i in range(6)]
        a, _ = run_with_budget(tasks, 5.0, max_in_flight=None)
        b, _ = run_with_budget(tasks, 5.0, max_in_flight=2)
        assert [(n, f.result()) for n, f in a] == [(n, f.result()) for n, f in b]

    def test_a_task_still_waiting_on_a_permit_is_abandoned_not_settled(self):
        """The semaphore must not be able to smuggle work past the budget.

        A blocked task is not `done()`, so it lands in `abandoned` — which every
        caller already treats as "no answer", the outcome a task that never ran
        should produce.
        """
        import threading as _t

        gate = _t.Event()
        tasks = [(f"t{i}", lambda: gate.wait(5.0)) for i in range(4)]
        try:
            settled, abandoned = run_with_budget(
                tasks, 0.2, thread_name_prefix="test", max_in_flight=1
            )
            assert len(abandoned) == 4
            assert settled == []
        finally:
            gate.set()

    def test_a_nonsense_cap_is_rejected_rather_than_deadlocking(self):
        with pytest.raises(ValueError):
            run_with_budget([("t", lambda: 1)], 1.0, max_in_flight=0)

    def test_an_exception_still_releases_the_permit(self):
        def boom():
            raise RuntimeError("nope")

        tasks = [("bad", boom)] + [(f"t{i}", lambda: "ok") for i in range(4)]
        settled, abandoned = run_with_budget(tasks, 5.0, max_in_flight=1)
        assert abandoned == []
        assert len(settled) == 5
        with pytest.raises(RuntimeError):
            settled[0][1].result()


def test_the_abandonment_log_does_not_name_a_phase(caplog):
    """It runs for the fetch phase AND a same-host re-check pass; naming one mislabels the other.

    The orchestrator names its own phase at the call site, so this line must not.
    """
    import threading as _t

    gate = _t.Event()
    try:
        with caplog.at_level("ERROR"):
            run_with_budget([("slow", lambda: gate.wait(5.0))], 0.1)
    finally:
        gate.set()
    msg = caplog.text
    assert "still running after the" in msg
    assert "budget" in msg
    assert "fetch budget" not in msg
