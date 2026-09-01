"""Tests for the per-source staleness alarm.

Mirrors job-sift/tests/test_source_health.py, plus the one hazard hk-events has
and job-sift does not: `HK_EVENTS_PUSH_EMPTY=0` suppresses the Telegram push
entirely on an empty digest. That gate is reasonable most days and catastrophic
on the day a feed has been dead for three runs — it is precisely how "nothing
found" and "I could not look" come to look identical to a reader. The alarm
must override it.

What is pinned here:
  * the counter increments on failure and resets on success;
  * the alarm fires at EXACTLY 3 consecutive failed runs and not at 2;
  * the three DISABLED adapters can never accrue a counter or alarm;
  * a budget timeout counts as a failure;
  * the alarm overrides HK_EVENTS_PUSH_EMPTY=0 and reaches Telegram;
  * --dry-run persists nothing.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from hk_events import config, orchestrator, source_health
from hk_events.render import render, render_vault_archive
from hk_events.schema import Event, RelevanceResult

_DAY = date(2026, 9, 1)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Never touch the operator's real .data/state/."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    return tmp_path


def _event(source: str = "meetup", external_id: str = "1") -> Event:
    return Event(
        source=source,
        external_id=external_id,
        title=f"{source} event {external_id}",
        url=f"https://example.com/{source}/{external_id}",
        start=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )


def _run_once(health, *, errors, attempted=("meetup", "luma"), today=_DAY):
    return source_health.update_health(
        health, attempted=attempted, errors=errors, today=today
    )


class TestCounter:
    def test_failure_increments_and_success_resets(self):
        health: dict = {}
        for expected in (1, 2, 3, 4):
            health = _run_once(health, errors={"meetup": "fetch failed: connection reset"})
            assert health["meetup"]["consecutive_failures"] == expected
            assert health["luma"]["consecutive_failures"] == 0

        health = _run_once(health, errors={})
        assert health["meetup"]["consecutive_failures"] == 0

    def test_last_success_survives_the_failure_streak(self):
        health = _run_once({}, errors={}, today=date(2026, 8, 29))
        for _ in range(6):
            health = _run_once(health, errors={"meetup": "boom"})
        assert health["meetup"]["last_success"] == "2026-08-29"
        assert health["meetup"]["consecutive_failures"] == 6

    def test_streak_is_broken_by_a_single_success(self):
        health: dict = {}
        health = _run_once(health, errors={"meetup": "boom"})
        health = _run_once(health, errors={"meetup": "boom"})
        health = _run_once(health, errors={})
        health = _run_once(health, errors={"meetup": "boom"})
        assert health["meetup"]["consecutive_failures"] == 1
        assert source_health.render_alarm(health) is None

    def test_error_text_is_bounded(self):
        health = _run_once({}, errors={"meetup": "x" * 5000})
        assert len(health["meetup"]["last_error"]) == 200

    def test_an_absurd_stored_value_is_not_fatal(self):
        health = _run_once({"meetup": {"consecutive_failures": None}}, errors={"meetup": "boom"})
        assert health["meetup"]["consecutive_failures"] == 1


class TestThresholdBoundary:
    """The threshold is >= 3. Test the boundary precisely."""

    def _streak(self, n: int) -> dict:
        health: dict = {}
        for _ in range(n):
            health = _run_once(health, errors={"meetup": "fetch failed: connection reset"})
        assert health["meetup"]["consecutive_failures"] == n
        return health

    def test_two_consecutive_failures_do_not_alarm(self):
        health = self._streak(2)
        assert source_health.stale_sources(health) == []
        assert source_health.render_alarm(health) is None

    def test_exactly_three_consecutive_failures_alarm(self):
        health = self._streak(3)
        assert [n for n, _ in source_health.stale_sources(health)] == ["meetup"]
        alarm = source_health.render_alarm(health)
        assert alarm is not None
        assert "meetup" in alarm
        assert "3 consecutive failed runs" in alarm

    def test_alarm_names_the_last_success_not_a_guessed_day_count(self):
        """We count RUNS. The date is the only wall-clock fact we actually hold."""
        health = _run_once({}, errors={}, today=date(2026, 8, 29))
        for _ in range(3):
            health = _run_once(health, errors={"meetup": "boom"})
        assert "last successful fetch: 2026-08-29" in source_health.render_alarm(health)

    def test_never_succeeded_says_never(self):
        assert "last successful fetch: never" in source_health.render_alarm(self._streak(3))


class TestAbsenceIsNotFailure:
    def test_enabled_sources_is_derived_from_the_real_fetch_list(self):
        assert orchestrator.enabled_sources() == [
            name for name, _ in orchestrator._source_tasks()
        ]

    def test_the_disabled_adapters_are_not_attempted(self):
        """aitinkerers/cyberport/startmeuphk are commented out of the fetch
        list. They must never accrue a counter, let alone alarm."""
        enabled = orchestrator.enabled_sources()
        for dead in ("aitinkerers", "cyberport", "startmeuphk"):
            assert dead not in enabled

        health: dict = {}
        for _ in range(10):
            health = _run_once(health, errors={}, attempted=enabled)
        assert set(health) == set(enabled)
        assert source_health.render_alarm(health) is None

    def test_a_removed_source_is_pruned_and_cannot_alarm(self):
        health: dict = {}
        for _ in range(5):
            health = _run_once(health, errors={"cyberport": "403"}, attempted=("meetup", "cyberport"))
        assert source_health.render_alarm(health) is not None

        health = _run_once(health, errors={}, attempted=("meetup",))
        assert "cyberport" not in health
        assert source_health.render_alarm(health) is None


class TestBudgetTimeoutCountsAsFailure:
    def test_over_budget_error_increments_like_any_other(self):
        health: dict = {}
        for _ in range(3):
            health = _run_once(
                health, errors={"meetup": "fetch failed: exceeded the 240s fetch budget"}
            )
        alarm = source_health.render_alarm(health)
        assert alarm is not None
        assert "budget" in alarm


class TestPersistence:
    def test_round_trips_through_disk(self, _isolated_state):
        health = _run_once({}, errors={"meetup": "boom"})
        source_health.save_health(health)
        assert (_isolated_state / "source_health.json").exists()
        assert source_health.load_health() == health

    def test_missing_file_is_empty_not_an_error(self):
        assert source_health.load_health() == {}

    def test_corrupt_file_starts_fresh_instead_of_killing_the_run(self, _isolated_state):
        (_isolated_state / "source_health.json").write_text("{not json")
        assert source_health.load_health() == {}

    def test_non_object_file_starts_fresh(self, _isolated_state):
        (_isolated_state / "source_health.json").write_text(json.dumps(["meetup"]))
        assert source_health.load_health() == {}


class TestRenderPlacement:
    def test_alarm_leads_the_quiet_digest(self):
        alarm = "🚨 alarm"
        messages = render(
            surfaced=[],
            total_new=0,
            total_processed=0,
            calendar_stats=None,
            today=_DAY,
            staleness_alarm=alarm,
        )
        assert messages[0] == alarm
        assert "No new relevant events today" in messages[1]

    def test_alarm_leads_a_populated_digest(self):
        alarm = "🚨 alarm"
        messages = render(
            surfaced=[(_event(), RelevanceResult("founder_ai", "ok"), "new")],
            total_new=1,
            total_processed=1,
            calendar_stats=None,
            today=_DAY,
            staleness_alarm=alarm,
        )
        assert messages[0] == alarm

    def test_no_alarm_means_no_extra_bubble(self):
        messages = render(
            surfaced=[], total_new=0, total_processed=0, calendar_stats=None, today=_DAY
        )
        assert not messages[0].startswith("🚨")

    def test_archive_records_the_alarm(self):
        md = render_vault_archive(
            surfaced=[], dropped=[], today=_DAY, staleness_alarm="🚨 meetup is dead"
        )
        assert "## 🚨 Stale source alarm" in md
        assert "meetup is dead" in md


class _Harness:
    """Patch everything run() touches except the bit under test."""

    def __init__(self, monkeypatch, *, events, errors, surface: bool = False):
        self.pushed: list[list[str]] = []
        monkeypatch.setattr(orchestrator, "_fetch_all_sources", lambda: (events, errors))
        monkeypatch.setattr(orchestrator, "push_messages", lambda msgs: self.pushed.append(msgs))
        monkeypatch.setattr(orchestrator, "write_archive", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "save_seen", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "log_classification", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "record_verdict", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "sync_events", lambda *a, **k: None)
        monkeypatch.setattr(
            orchestrator,
            "filter_due",
            lambda evs: ([(e, orchestrator.STAGE_NEW, None) for e in evs], {}),
        )
        tag = "founder_ai" if surface else "drop"
        monkeypatch.setattr(
            orchestrator, "classify", lambda e: RelevanceResult(tag, "test verdict")
        )
        monkeypatch.setattr(config, "assert_required", lambda: None)


def _seed_two_failures(source="meetup"):
    source_health.save_health(
        {
            source: {
                "consecutive_failures": 2,
                "last_success": "2026-08-29",
                "last_failure": "2026-08-31",
                "last_error": "fetch failed: connection reset",
            }
        }
    )


class TestPushEmptyGateOverride:
    """HK_EVENTS_PUSH_EMPTY=0 must not silence a staleness alarm."""

    def test_gate_still_silences_a_genuinely_quiet_day(self, monkeypatch):
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        h = _Harness(monkeypatch, events=[_event()], errors={})

        assert orchestrator.run() == 0

        assert h.pushed == [], "a quiet, healthy day should still stay silent"

    def test_alarm_overrides_the_gate_on_an_empty_digest(self, monkeypatch):
        """The load-bearing case: nothing surfaced, a source dead for 3 runs,
        and the gate that would normally suppress the push."""
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        _seed_two_failures()
        h = _Harness(monkeypatch, events=[_event("luma")], errors={"meetup": "fetch failed: connection reset"})

        assert orchestrator.run() == 0

        assert len(h.pushed) == 1, "the alarm did not reach Telegram"
        assert h.pushed[0][0].startswith("🚨")
        assert "meetup" in h.pushed[0][0]
        assert "3 consecutive failed runs" in h.pushed[0][0]

    def test_below_threshold_the_gate_still_wins(self, monkeypatch):
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        source_health.save_health({"meetup": {"consecutive_failures": 1}})
        h = _Harness(monkeypatch, events=[_event("luma")], errors={"meetup": "fetch failed: connection reset"})

        assert orchestrator.run() == 0

        assert h.pushed == []

    def test_zero_events_path_also_carries_the_alarm(self, monkeypatch):
        """The `no events fetched at all` early return is a separate render
        call site and must carry the alarm too."""
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        _seed_two_failures()
        h = _Harness(monkeypatch, events=[], errors={"meetup": "fetch failed: connection reset"})

        assert orchestrator.run() == 0

        assert len(h.pushed) == 1
        assert h.pushed[0][0].startswith("🚨")


class TestOrchestratorPersistence:
    def test_counters_are_persisted_on_a_real_run(self, monkeypatch, _isolated_state):
        _seed_two_failures()
        _Harness(monkeypatch, events=[], errors={"meetup": "fetch failed: connection reset"})

        orchestrator.run()

        stored = source_health.load_health()
        assert stored["meetup"]["consecutive_failures"] == 3
        assert stored["luma"]["consecutive_failures"] == 0

    def test_dry_run_writes_no_counter_state(self, monkeypatch, _isolated_state):
        _seed_two_failures()
        h = _Harness(monkeypatch, events=[], errors={"meetup": "fetch failed: connection reset"})
        before = (_isolated_state / "source_health.json").read_text()

        assert orchestrator.run(dry_run=True) == 0

        assert (_isolated_state / "source_health.json").read_text() == before
        assert source_health.load_health()["meetup"]["consecutive_failures"] == 2
        assert h.pushed == []

    def test_dry_run_creates_no_state_file_at_all_from_scratch(
        self, monkeypatch, _isolated_state
    ):
        _Harness(monkeypatch, events=[], errors={"meetup": "boom"})

        orchestrator.run(dry_run=True)

        assert not (_isolated_state / "source_health.json").exists()

    def test_healthy_run_resets_a_standing_alarm(self, monkeypatch, _isolated_state):
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        source_health.save_health({"meetup": {"consecutive_failures": 9}})
        h = _Harness(monkeypatch, events=[_event()], errors={})

        orchestrator.run()

        assert source_health.load_health()["meetup"]["consecutive_failures"] == 0
        assert h.pushed == []
