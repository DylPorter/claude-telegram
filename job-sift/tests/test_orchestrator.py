"""Tests for concurrent source fetching under a hard wall-clock budget.

Before this change the five sources were fetched serially and nothing bounded
an individual fetch, so the 2026-09-01 DNS outage cost sum(t) with no ceiling
and systemd SIGTERM'd the run before push + state-save. These tests pin both
halves of the fix:

  * sources run in PARALLEL, so a slow one no longer delays the others;
  * the budget is a REAL ceiling — a source that blows it is abandoned, is
    recorded in the error map as a failure (not a quiet zero), and does not
    stop the run from returning what the other sources brought back.

Timing assertions use a generous margin: they only need to tell max(t) from
sum(t), not to measure anything precisely.
"""

from __future__ import annotations

import threading
import time

import pytest

from job_sift import config, orchestrator
from job_sift.errors import SourceAuthError
from job_sift.schema import JobListing

_SOURCES = ("cedars", "greenhouse", "lever", "ashby", "linkedin")

# Every fetcher sleeps this long in the concurrency test. Serial would cost
# 5x this; concurrent costs roughly 1x.
_SLOW = 0.3


def _listing(source: str, external_id: str = "1") -> JobListing:
    return JobListing(
        source=source,
        external_id=external_id,
        employer=f"{source} co",
        title="Software Engineer Intern",
        apply_url=f"https://example.com/{source}/{external_id}",
    )


def _patch_sources(monkeypatch, **fetchers) -> None:
    """Replace every source fetcher. Unnamed sources return nothing."""
    targets = {
        "cedars": (orchestrator.cedars, "fetch_cedars_listings"),
        "greenhouse": (orchestrator.greenhouse, "fetch_greenhouse_listings"),
        "lever": (orchestrator.lever, "fetch_lever_listings"),
        "ashby": (orchestrator.ashby, "fetch_ashby_listings"),
        "linkedin": (orchestrator.linkedin, "fetch_linkedin_listings"),
    }
    for name, (module, attr) in targets.items():
        fn = fetchers.get(name, lambda *a, **k: [])
        monkeypatch.setattr(module, attr, fn)
    # The seen-set load happens on the main thread before the fan-out; keep it
    # off the real state files.
    monkeypatch.setattr(orchestrator, "load_seen", lambda source: set())


def _sleeper(source: str, seconds: float):
    def _fetch(*args, **kwargs):
        time.sleep(seconds)
        return [_listing(source)]

    return _fetch


def test_sources_are_fetched_concurrently(monkeypatch):
    """Five sources each sleeping _SLOW must finish in ~_SLOW, not 5x _SLOW."""
    _patch_sources(monkeypatch, **{s: _sleeper(s, _SLOW) for s in _SOURCES})

    started = time.monotonic()
    listings, errors = orchestrator._fetch_all_sources()
    elapsed = time.monotonic() - started

    assert errors == {}
    assert {l.source for l in listings} == set(_SOURCES)
    # Serial would be 5 * _SLOW = 1.5s. Half of that still proves parallelism
    # while leaving plenty of slack for a loaded machine.
    assert elapsed < _SLOW * 2.5, f"sources look serial: {elapsed:.2f}s"


def test_a_slow_source_does_not_delay_a_fast_one(monkeypatch):
    """A fast source must FINISH early, not queue behind the slow one.

    cedars is first in the fetch order, so under the old serial loop
    greenhouse could not even start until cedars had returned. Asserting on
    when greenhouse finished (rather than on the total) is what distinguishes
    max(t) from sum(t) no matter how many sources there are.
    """
    finished_at: dict[str, float] = {}
    started = time.monotonic()

    def _timed(source: str, seconds: float):
        def _fetch(*args, **kwargs):
            time.sleep(seconds)
            finished_at[source] = time.monotonic() - started
            return [_listing(source)]

        return _fetch

    _patch_sources(
        monkeypatch,
        cedars=_timed("cedars", _SLOW * 3),
        greenhouse=_timed("greenhouse", 0.0),
    )

    listings, errors = orchestrator._fetch_all_sources()

    assert errors == {}
    assert {l.source for l in listings} == {"cedars", "greenhouse"}
    assert finished_at["greenhouse"] < _SLOW, (
        "the fast source waited on the slow one: "
        f"greenhouse finished at {finished_at['greenhouse']:.2f}s"
    )


def test_source_over_budget_lands_in_the_error_map(monkeypatch):
    """A source still running at the budget is a FAILURE, not a quiet zero."""
    monkeypatch.setenv(config.FETCH_BUDGET_ENV, "0.3")
    _patch_sources(
        monkeypatch,
        cedars=_sleeper("cedars", 30),
        greenhouse=lambda *a, **k: [_listing("greenhouse")],
        lever=lambda *a, **k: [_listing("lever")],
    )

    started = time.monotonic()
    listings, errors = orchestrator._fetch_all_sources()
    elapsed = time.monotonic() - started

    # The run is bounded by the budget, not by the straggler.
    assert elapsed < 5, f"budget not enforced: {elapsed:.2f}s"
    assert set(errors) == {"cedars"}
    # Same shape as a crashed source, so the health line and any per-source
    # staleness counter built on this map count it as a failure.
    assert errors["cedars"].startswith("fetch failed: ")
    assert "budget" in errors["cedars"]
    # ...and the run still returns what the healthy sources brought back.
    assert {l.source for l in listings} == {"greenhouse", "lever"}


def test_over_budget_partial_result_is_discarded(monkeypatch):
    """A straggler's listings must not leak in after the budget expires."""
    monkeypatch.setenv(config.FETCH_BUDGET_ENV, "0.2")
    _patch_sources(monkeypatch, cedars=_sleeper("cedars", 30))

    listings, errors = orchestrator._fetch_all_sources()

    assert listings == []
    assert set(errors) == {"cedars"}


def test_abandoned_source_runs_on_a_daemon_thread(monkeypatch):
    """The abandoned worker must not hold the interpreter open at exit.

    A thread blocked in getaddrinfo cannot be cancelled or joined, so the only
    way the budget is a real ceiling on the PROCESS (and not just on
    _fetch_all_sources) is for the worker to be a daemon thread.
    """
    monkeypatch.setenv(config.FETCH_BUDGET_ENV, "0.2")
    release = threading.Event()
    seen: list[threading.Thread] = []

    def _blocked(*args, **kwargs):
        seen.append(threading.current_thread())
        release.wait(30)
        return []

    _patch_sources(monkeypatch, cedars=_blocked)
    try:
        orchestrator._fetch_all_sources()
        assert seen, "the blocked fetcher never started"
        assert seen[0].is_alive()
        assert seen[0].daemon is True
    finally:
        release.set()


def test_cedars_keeps_its_preloaded_seen_set(monkeypatch):
    """Greedy pagination depends on the seen-set being loaded and handed in."""
    captured: dict[str, object] = {}
    preloaded = {"cedars:1", "cedars:2"}

    def _fetch_cedars(*, seen_ids=None, max_pages=None):
        captured["seen_ids"] = seen_ids
        captured["thread"] = threading.current_thread().name
        return []

    _patch_sources(monkeypatch, cedars=_fetch_cedars)
    loaded_on: list[str] = []

    def _load_seen(source):
        loaded_on.append(threading.current_thread().name)
        return set(preloaded) if source == "cedars" else set()

    monkeypatch.setattr(orchestrator, "load_seen", _load_seen)

    orchestrator._fetch_all_sources()

    assert captured["seen_ids"] == preloaded
    # Loaded on the main thread, BEFORE the fan-out — the workers must not
    # race each other on the state files.
    assert loaded_on == [threading.current_thread().name]


def test_auth_failure_still_distinguished_from_a_generic_crash(monkeypatch):
    def _expired(*args, **kwargs):
        raise SourceAuthError("cedars", "session expired — re-export cookies")

    def _boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    _patch_sources(monkeypatch, cedars=_expired, lever=_boom)

    listings, errors = orchestrator._fetch_all_sources()

    assert listings == []
    assert errors == {
        "cedars": "session expired — re-export cookies",
        "lever": "fetch failed: connection reset",
    }


def test_healthy_run_has_an_empty_error_map(monkeypatch):
    _patch_sources(monkeypatch, greenhouse=lambda *a, **k: [_listing("greenhouse")])

    listings, errors = orchestrator._fetch_all_sources()

    assert errors == {}
    assert [l.source for l in listings] == ["greenhouse"]


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
