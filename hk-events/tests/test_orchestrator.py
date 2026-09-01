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

import threading
import time
from datetime import datetime, timezone

import pytest

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

    events, errors = orchestrator._fetch_all_sources()

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

    events, errors = orchestrator._fetch_all_sources()

    assert errors == {}
    assert {e.external_id for e in events} == {"m1", "l1"}


def test_error_map_empty_when_all_sources_succeed(monkeypatch):
    monkeypatch.setattr(orchestrator.meetup, "fetch_meetup_events", lambda: [])
    monkeypatch.setattr(orchestrator.luma, "fetch_luma_events", lambda: [])

    events, errors = orchestrator._fetch_all_sources()

    assert events == []
    assert errors == {}


def test_both_sources_failing_are_both_recorded(monkeypatch):
    def _meetup_boom():
        raise ValueError("bad feed xml")

    def _luma_boom():
        raise RuntimeError("timeout")

    monkeypatch.setattr(orchestrator.meetup, "fetch_meetup_events", _meetup_boom)
    monkeypatch.setattr(orchestrator.luma, "fetch_luma_events", _luma_boom)

    events, errors = orchestrator._fetch_all_sources()

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
    events, errors = orchestrator._fetch_all_sources()
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

    events, errors = orchestrator._fetch_all_sources()

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
    events, errors = orchestrator._fetch_all_sources()
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

    events, errors = orchestrator._fetch_all_sources()

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
