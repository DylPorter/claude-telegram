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
  * the two DISABLED adapters can never accrue a counter or alarm;
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
    # run(stub=True) sets this in os.environ and never unsets it. Letting
    # monkeypatch own the key means the teardown reverts it, so a stub test
    # cannot leak stub mode into whatever runs next.
    monkeypatch.setenv("HK_EVENTS_STUB", "0")
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
    """One run's outcome, as the ORCHESTRATOR would report it.

    `attempted` names the sources the run fanned out over; the ones absent from
    `errors` are the ones that came back with a result, so they are what the
    orchestrator passes as `succeeded`. Deriving it here is fine — this helper
    is standing in for the fetch phase's observations. What is NOT fine, and is
    what `update_health` no longer does, is making that same inference INSIDE
    the counter from a static enabled-list that never watched anything run.
    """
    return source_health.update_health(
        health,
        succeeded=[s for s in attempted if s not in errors],
        errors=errors,
        today=today,
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

    def test_no_recorded_success_is_bounded_by_first_seen_not_called_never(self):
        """"never" would be a fabricated absolute — the state only knows what
        it has observed since it started tracking the source."""
        alarm = source_health.render_alarm(self._streak(3))
        assert "no successful fetch since tracking began 2026-09-01" in alarm
        assert "never" not in alarm

    def test_unknown_when_not_even_first_seen_is_known(self):
        alarm = source_health.render_alarm({"meetup": {"consecutive_failures": 3}})
        assert "last successful fetch: unknown" in alarm


class TestAbsenceIsNotFailure:
    def test_enabled_sources_is_derived_from_the_real_fetch_list(self):
        assert orchestrator.enabled_sources() == [
            name for name, _ in orchestrator._source_tasks()
        ]

    def test_the_disabled_adapters_are_not_attempted(self):
        """cyberport/startmeuphk are commented out of the fetch list. They must
        never accrue a counter, let alone alarm.

        aitinkerers used to be on this list. It was un-disabled 2026-09-01: the
        403 that justified parking it is gone, and its events are readable as
        schema.org JSON-LD, so it is now a live source that SHOULD accrue
        counters. Asserted below rather than just dropped from the tuple, so
        nobody quietly re-parks it."""
        enabled = orchestrator.enabled_sources()
        for dead in ("cyberport", "startmeuphk"):
            assert dead not in enabled
        for live in ("aitinkerers", "luma_discover"):
            assert live in enabled

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

    def test_first_seen_is_set_once_and_carried_forward(self):
        health = _run_once({}, errors={"meetup": "boom"}, today=date(2026, 8, 1))
        assert health["meetup"]["first_seen"] == "2026-08-01"
        for day in (2, 3, 4):
            health = _run_once(health, errors={"meetup": "boom"}, today=date(2026, 8, day))
        assert health["meetup"]["first_seen"] == "2026-08-01"

    def test_save_is_atomic_and_leaves_no_tmp_files(self, _isolated_state):
        source_health.save_health(_run_once({}, errors={"meetup": "boom"}))
        source_health.save_health(_run_once({}, errors={}))
        assert [p.name for p in _isolated_state.iterdir()] == ["source_health.json"]

    def test_a_failed_save_leaves_the_previous_state_intact(self, _isolated_state, monkeypatch):
        """A SIGTERM mid-write must not truncate the file into 'corrupt' —
        that would reset a dead source's streak and buy it three more runs of
        silence, which is the exact failure this module exists to prevent."""
        good = _run_once({}, errors={"meetup": "boom"})
        source_health.save_health(good)

        def _die(*a, **k):
            raise OSError("SIGTERM mid-write")

        monkeypatch.setattr(source_health.os, "replace", _die)
        with pytest.raises(OSError):
            source_health.save_health(_run_once(good, errors={}))

        assert source_health.load_health() == good
        assert [p.name for p in _isolated_state.iterdir()] == ["source_health.json"]

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

    def __init__(self, monkeypatch, *, events, errors, surface: bool = False, succeeded=None):
        self.pushed: list[list[str]] = []
        if succeeded is None:
            succeeded = [s for s in orchestrator.enabled_sources() if s not in errors]
        monkeypatch.setattr(
            orchestrator, "_fetch_all_sources", lambda: (events, errors, list(succeeded))
        )
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

    def test_stub_run_does_not_wipe_a_standing_streak(self, monkeypatch, _isolated_state):
        """hk-events is not vulnerable today (its two stub adapters are both
        disabled), but re-enabling either is a documented one-line change, so
        the guard is pinned here too rather than left to be rediscovered."""
        source_health.save_health(
            {"meetup": {"consecutive_failures": 49, "last_success": "2026-07-04"}}
        )
        _Harness(monkeypatch, events=[], errors={})

        assert orchestrator.run(dry_run=False, stub=True) == 0

        assert source_health.load_health()["meetup"]["consecutive_failures"] == 49

    def test_a_real_run_with_the_same_inputs_does_reset(self, monkeypatch, _isolated_state):
        """Control: it must be --stub doing the protecting, not the harness."""
        source_health.save_health(
            {"meetup": {"consecutive_failures": 49, "last_success": "2026-07-04"}}
        )
        _Harness(monkeypatch, events=[], errors={})

        assert orchestrator.run(dry_run=False, stub=False) == 0

        assert source_health.load_health()["meetup"]["consecutive_failures"] == 0

    def test_healthy_run_resets_a_standing_alarm(self, monkeypatch, _isolated_state):
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        source_health.save_health({"meetup": {"consecutive_failures": 9}})
        h = _Harness(monkeypatch, events=[_event()], errors={})

        orchestrator.run()

        assert source_health.load_health()["meetup"]["consecutive_failures"] == 0
        assert h.pushed == []


class TestDropNoticeUnit:
    """Pruning is right, but it is also the one way to make a LIVE alarm vanish
    without fixing anything: drop the source's config key and the record goes
    with it. The digest cannot show that — the ⚠️ health line is driven by the
    error map and a pruned source is in neither map — and the drop resets the
    re-arm clock, so the source comes back looking brand new and needs another
    ALARM_THRESHOLD runs before it can shout again. One line in the push, no
    schema change, no accrual."""

    _STALE = {"meetup": {"consecutive_failures": 12, "last_success": "2026-07-04"}}

    def test_a_pruned_source_at_the_threshold_produces_the_line(self):
        current = source_health.update_health(
            dict(self._STALE), succeeded=[], errors={}, today=_DAY
        )
        assert current == {}, "premise: the record really was pruned"

        notice = source_health.render_drop_notice(self._STALE, current)

        assert notice is not None
        assert "meetup" in notice
        assert "12-run failure streak" in notice
        assert "dropped from health tracking" in notice

    def test_exactly_at_the_threshold_counts(self):
        prior = {"meetup": {"consecutive_failures": source_health.ALARM_THRESHOLD}}
        assert source_health.render_drop_notice(prior, {}) is not None

    def test_one_below_the_threshold_says_nothing(self):
        """A source pruned while merely wobbling is not news — it never had a
        standing alarm to lose. The two DISABLED adapters are pruned every
        run and must stay silent."""
        prior = {"meetup": {"consecutive_failures": source_health.ALARM_THRESHOLD - 1}}
        assert source_health.render_drop_notice(prior, {}) is None

    def test_the_disabled_adapters_never_produce_a_notice(self):
        """cyberport/startmeuphk are commented out of _source_tasks and carry
        no record at all — pruning nothing says nothing. (aitinkerers was in
        that list until 2026-09-01; it is live now and DOES accrue failures.)"""
        assert source_health.render_drop_notice({}, {}) is None

    def test_a_source_that_is_still_tracked_is_not_a_drop(self):
        current = source_health.update_health(
            dict(self._STALE), succeeded=[], errors={"meetup": "still dead"}, today=_DAY
        )
        assert source_health.render_drop_notice(self._STALE, current) is None
        assert source_health.render_alarm(current) is not None

    def test_it_claims_neither_failure_nor_success(self):
        notice = source_health.render_drop_notice(self._STALE, {})
        assert "🚨" not in notice
        assert _DAY.isoformat() not in notice

    def test_worst_streak_leads(self):
        prior = {
            "luma": {"consecutive_failures": 4},
            "meetup": {"consecutive_failures": 20},
            "cyberport": {"consecutive_failures": 1},
        }
        lines = source_health.render_drop_notice(prior, {}).splitlines()
        assert len(lines) == 2, "the sub-threshold source must not get a line"
        assert "meetup" in lines[0]
        assert "luma" in lines[1]


class TestDropNoticeOverridesThePushEmptyGate:
    """HK_EVENTS_PUSH_EMPTY=0 must not silence this either.

    Same argument as the staleness alarm one step removed: the gate exists so a
    daily "nothing today" doesn't train the reader to stop opening the digest,
    but on the day a source carrying a live alarm leaves the counters because
    its config vanished, that silence is how the alarm gets deleted by a YAML
    edit nobody meant to make.
    """

    def test_the_notice_reaches_the_push_on_an_empty_digest(self, monkeypatch):
        """THE gate case: events were fetched, none surfaced, so
        HK_EVENTS_PUSH_EMPTY=0 would normally suppress the push entirely.

        `test_a_sub_threshold_drop_lets_the_gate_win` below is the control — it
        is the same setup with a smaller streak, and it stays silent, so the
        push here is the notice overriding the gate and not the harness pushing
        unconditionally.
        """
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        source_health.save_health(
            {"meetup": {"consecutive_failures": 12, "last_success": "2026-07-04"}}
        )
        # meetup in NEITHER set — exactly what an unconfigured source produces.
        h = _Harness(monkeypatch, events=[_event("luma")], errors={}, succeeded=["luma"])

        assert orchestrator.run() == 0

        assert len(h.pushed) == 1, "the drop notice did not reach Telegram"
        assert any("dropped from health tracking" in m for m in h.pushed[0])
        assert any("12-run failure streak" in m for m in h.pushed[0])

    def test_the_zero_events_path_carries_it_too(self, monkeypatch):
        """`no events fetched at all` is a separate render call site, and it
        pushes its heartbeat regardless of the gate. The notice must ride it."""
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        source_health.save_health({"meetup": {"consecutive_failures": 12}})
        h = _Harness(monkeypatch, events=[], errors={}, succeeded=["luma"])

        assert orchestrator.run() == 0

        assert len(h.pushed) == 1
        assert any("dropped from health tracking" in m for m in h.pushed[0])

    def test_a_sub_threshold_drop_lets_the_gate_win(self, monkeypatch):
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        source_health.save_health({"meetup": {"consecutive_failures": 1}})
        h = _Harness(monkeypatch, events=[_event("luma")], errors={}, succeeded=["luma"])

        assert orchestrator.run() == 0

        assert h.pushed == [], "a wobble that got pruned is not worth breaking silence"

    def test_the_notice_is_self_clearing(self, monkeypatch):
        """A deliberate disable costs one line, ONCE. The record is gone from
        the state file after run one, so run two is silent again."""
        monkeypatch.setattr(config, "HK_EVENTS_PUSH_EMPTY", False)
        source_health.save_health({"meetup": {"consecutive_failures": 12}})

        first = _Harness(monkeypatch, events=[_event("luma")], errors={}, succeeded=["luma"])
        assert orchestrator.run() == 0
        assert len(first.pushed) == 1

        second = _Harness(monkeypatch, events=[_event("luma")], errors={}, succeeded=["luma"])
        assert orchestrator.run() == 0
        assert second.pushed == [], "the notice must not nag"

    def test_dry_run_writes_nothing_and_pushes_nothing(self, monkeypatch, _isolated_state):
        source_health.save_health({"meetup": {"consecutive_failures": 12}})
        before = (_isolated_state / "source_health.json").read_text()
        h = _Harness(monkeypatch, events=[], errors={}, succeeded=["luma"])

        assert orchestrator.run(dry_run=True) == 0

        assert (_isolated_state / "source_health.json").read_text() == before
        assert h.pushed == []

    def test_the_notice_also_lands_in_the_vault_archive(self):
        md = render_vault_archive(
            surfaced=[],
            dropped=[],
            today=_DAY,
            source_errors=None,
            drop_notice="ℹ️ *meetup* — dropped from health tracking",
        )
        assert "## ℹ️ Dropped from health tracking" in md
        assert "> ℹ️ *meetup* — dropped from health tracking" in md
