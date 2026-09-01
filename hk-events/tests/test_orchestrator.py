"""Tests for the per-source error map in `_fetch_all_sources`.

Mirrors the behaviour job-sift already has: one dead source must never abort
the run, and its failure must be visible (not silently swallowed) so the
digest + vault archive can surface a source-health line.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hk_events import orchestrator
from hk_events.schema import Event


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
