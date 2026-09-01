"""Tests for the per-source staleness alarm.

The failure this guards against is the one that actually happened: CEDARS'
auth died on 2026-07-05 and for FIFTY consecutive runs the digest printed
"No new prestige matches today" — meaning, in fact, "I could not look". The
per-run health line already made one bad run visible; these tests pin the part
that makes a PERSISTENT one impossible to scroll past.

What is pinned here:
  * the counter increments on failure and resets on success;
  * the alarm fires at EXACTLY 3 consecutive failed runs and not at 2;
  * a source that was never attempted cannot accrue a counter or alarm;
  * a budget timeout counts as a failure (a source timing out every day is
    exactly as dead as one erroring every day);
  * the alarm reaches the Telegram push on an otherwise-empty digest;
  * --dry-run persists nothing.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from job_sift import config, orchestrator, source_health
from job_sift.render import render, render_vault_archive
from job_sift.schema import ClassifierResult, JobListing

_DAY = date(2026, 9, 1)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Never touch the operator's real .data/state/."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    # run(stub=True) sets this in os.environ and never unsets it. Letting
    # monkeypatch own the key means the teardown reverts it, so a stub test
    # cannot leak stub mode into whatever runs next.
    monkeypatch.setenv("JOB_SIFT_STUB", "0")
    return tmp_path


def _run_once(health, *, errors, attempted=("cedars", "greenhouse"), today=_DAY):
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


def _listing(source: str = "greenhouse", external_id: str = "1") -> JobListing:
    return JobListing(
        source=source,
        external_id=external_id,
        employer=f"{source} co",
        title="Software Engineer Intern",
        apply_url=f"https://example.com/{source}/{external_id}",
    )


class TestCounter:
    def test_failure_increments_and_success_resets(self):
        health: dict = {}
        for expected in (1, 2, 3, 4):
            health = _run_once(health, errors={"cedars": "session expired"})
            assert health["cedars"]["consecutive_failures"] == expected
            # The healthy source never leaves zero.
            assert health["greenhouse"]["consecutive_failures"] == 0

        health = _run_once(health, errors={})
        assert health["cedars"]["consecutive_failures"] == 0

    def test_success_records_the_date_and_clears_the_error(self):
        health = _run_once({}, errors={"cedars": "session expired"})
        assert health["cedars"]["last_error"] == "session expired"
        assert health["cedars"]["last_success"] is None

        health = _run_once(health, errors={}, today=date(2026, 9, 2))
        assert health["cedars"]["last_success"] == "2026-09-02"
        assert health["cedars"]["last_error"] is None

    def test_last_success_survives_the_failure_streak(self):
        """The alarm quotes this date, so a streak must not overwrite it."""
        health = _run_once({}, errors={}, today=date(2026, 7, 4))
        for _ in range(9):
            health = _run_once(health, errors={"cedars": "boom"})
        assert health["cedars"]["last_success"] == "2026-07-04"
        assert health["cedars"]["consecutive_failures"] == 9

    def test_streak_is_broken_by_a_single_success(self):
        health: dict = {}
        health = _run_once(health, errors={"cedars": "boom"})
        health = _run_once(health, errors={"cedars": "boom"})
        health = _run_once(health, errors={})
        health = _run_once(health, errors={"cedars": "boom"})
        assert health["cedars"]["consecutive_failures"] == 1
        assert source_health.render_alarm(health) is None

    def test_an_absurd_stored_value_is_not_fatal(self):
        """A hand-edited or truncated state file must not kill the run."""
        health = _run_once({"cedars": {"consecutive_failures": "banana"}}, errors={"cedars": "boom"})
        assert health["cedars"]["consecutive_failures"] == 1

    def test_error_text_is_bounded(self):
        """An adapter that stringifies a whole response body must not paste it
        into the state file (and from there into Telegram)."""
        health = _run_once({}, errors={"cedars": "x" * 5000})
        assert len(health["cedars"]["last_error"]) == 200


class TestThresholdBoundary:
    """The threshold is >= 3. Test the boundary precisely."""

    def _streak(self, n: int) -> dict:
        health: dict = {}
        for _ in range(n):
            health = _run_once(health, errors={"cedars": "session expired"})
        assert health["cedars"]["consecutive_failures"] == n
        return health

    def test_two_consecutive_failures_do_not_alarm(self):
        health = self._streak(2)
        assert source_health.stale_sources(health) == []
        assert source_health.render_alarm(health) is None

    def test_exactly_three_consecutive_failures_alarm(self):
        health = self._streak(3)
        assert [n for n, _ in source_health.stale_sources(health)] == ["cedars"]
        alarm = source_health.render_alarm(health)
        assert alarm is not None
        assert "cedars" in alarm
        assert "3 consecutive failed runs" in alarm

    def test_four_still_alarms_with_the_higher_count(self):
        alarm = source_health.render_alarm(self._streak(4))
        assert "4 consecutive failed runs" in alarm

    def test_alarm_names_the_last_success_not_a_guessed_day_count(self):
        """We count RUNS. The date is the only wall-clock fact we actually hold."""
        health = _run_once({}, errors={}, today=date(2026, 7, 4))
        for _ in range(3):
            health = _run_once(health, errors={"cedars": "session expired"})
        alarm = source_health.render_alarm(health)
        assert "last successful fetch: 2026-07-04" in alarm

    def test_no_recorded_success_is_bounded_by_first_seen_not_called_never(self):
        """"never" would be a fabricated absolute — the state only knows what
        it has observed since it started tracking the source."""
        alarm = source_health.render_alarm(self._streak(3))
        assert "no successful fetch since tracking began 2026-09-01" in alarm
        assert "never" not in alarm

    def test_unknown_when_not_even_first_seen_is_known(self):
        """A legacy or hand-edited record cannot support either claim."""
        alarm = source_health.render_alarm({"cedars": {"consecutive_failures": 3}})
        assert "last successful fetch: unknown" in alarm

    def test_worst_offender_is_listed_first(self):
        health: dict = {}
        for _ in range(3):
            health = _run_once(health, errors={"cedars": "a", "greenhouse": "b"})
        health = _run_once(health, errors={"cedars": "a", "greenhouse": "b"})
        alarm = source_health.render_alarm(health)
        assert alarm.index("cedars") < alarm.index("greenhouse")


class TestAbsenceIsNotFailure:
    def test_a_source_not_attempted_gets_no_counter(self):
        health = _run_once({}, errors={}, attempted=("cedars",))
        assert set(health) == {"cedars"}

    def test_a_removed_source_is_pruned_and_cannot_alarm(self):
        """A source dropped from the fetch list must not alarm forever."""
        health: dict = {}
        for _ in range(5):
            health = _run_once(health, errors={"linkedin": "boom"}, attempted=("cedars", "linkedin"))
        assert source_health.render_alarm(health) is not None

        health = _run_once(health, errors={}, attempted=("cedars",))
        assert "linkedin" not in health
        assert source_health.render_alarm(health) is None

    def test_enabled_sources_is_derived_from_the_real_fetch_list(self):
        """If these two ever drift, a disabled source could alarm."""
        assert orchestrator.enabled_sources() == [
            name for name, _ in orchestrator._source_tasks(set())
        ]
        assert orchestrator.enabled_sources() == [
            "cedars",
            "greenhouse",
            "lever",
            "ashby",
            "linkedin",
        ]


class TestBudgetTimeoutCountsAsFailure:
    def test_over_budget_error_increments_like_any_other(self):
        """Task 2 records a budget blow-out with a `fetch failed: ` prefix in
        the SAME error map. A source timing out every day is exactly as dead as
        one erroring every day."""
        health: dict = {}
        for _ in range(3):
            health = _run_once(
                health,
                errors={"cedars": "fetch failed: exceeded the 240s fetch budget"},
            )
        alarm = source_health.render_alarm(health)
        assert alarm is not None
        assert "budget" in alarm


class TestPersistence:
    def test_round_trips_through_disk(self, _isolated_state):
        health = _run_once({}, errors={"cedars": "boom"})
        source_health.save_health(health)
        assert (_isolated_state / "source_health.json").exists()
        assert source_health.load_health() == health

    def test_missing_file_is_empty_not_an_error(self):
        assert source_health.load_health() == {}

    def test_first_seen_is_set_once_and_carried_forward(self):
        health = _run_once({}, errors={"cedars": "boom"}, today=date(2026, 8, 1))
        assert health["cedars"]["first_seen"] == "2026-08-01"
        for day in (2, 3, 4):
            health = _run_once(health, errors={"cedars": "boom"}, today=date(2026, 8, day))
        assert health["cedars"]["first_seen"] == "2026-08-01"

    def test_save_is_atomic_and_leaves_no_tmp_files(self, _isolated_state):
        source_health.save_health(_run_once({}, errors={"cedars": "boom"}))
        source_health.save_health(_run_once({}, errors={}))
        assert [p.name for p in _isolated_state.iterdir()] == ["source_health.json"]

    def test_a_failed_save_leaves_the_previous_state_intact(self, _isolated_state, monkeypatch):
        """A SIGTERM mid-write must not truncate the file into 'corrupt' —
        that would reset a dead source's streak and buy it three more runs of
        silence, which is the exact failure this module exists to prevent."""
        good = _run_once({}, errors={"cedars": "boom"})
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
        (_isolated_state / "source_health.json").write_text(json.dumps(["cedars"]))
        assert source_health.load_health() == {}


class TestRenderPlacement:
    def test_alarm_leads_the_quiet_digest(self):
        alarm = "🚨 alarm"
        messages = render(
            surfaced=[],
            skipped=[],
            total_new=0,
            total_processed=0,
            today=_DAY,
            staleness_alarm=alarm,
        )
        assert messages[0] == alarm
        # ...and the misleading line is still there, but now second.
        assert "No new prestige matches today" in messages[1]

    def test_alarm_leads_a_populated_digest(self):
        alarm = "🚨 alarm"
        messages = render(
            surfaced=[(_listing(), ClassifierResult("prestige", "in_scope", "ok"))],
            skipped=[],
            total_new=1,
            total_processed=1,
            today=_DAY,
            staleness_alarm=alarm,
        )
        assert messages[0] == alarm

    def test_no_alarm_means_no_extra_bubble(self):
        without = render(surfaced=[], skipped=[], total_new=0, total_processed=0, today=_DAY)
        assert not without[0].startswith("🚨")

    def test_archive_records_the_alarm(self):
        md = render_vault_archive(
            surfaced=[], skipped=[], today=_DAY, staleness_alarm="🚨 cedars is dead"
        )
        assert "## 🚨 Stale source alarm" in md
        assert "cedars is dead" in md


class _Harness:
    """Patch everything run() touches except the bit under test."""

    def __init__(self, monkeypatch, *, listings, errors, succeeded=None):
        self.pushed: list[list[str]] = []
        if succeeded is None:
            succeeded = [s for s in orchestrator.enabled_sources() if s not in errors]
        monkeypatch.setattr(
            orchestrator, "_fetch_all_sources", lambda: (listings, errors, list(succeeded))
        )
        monkeypatch.setattr(orchestrator, "push_messages", lambda msgs: self.pushed.append(msgs))
        monkeypatch.setattr(orchestrator, "write_archive", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "save_seen", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "log_classification", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "_update_open_roles", lambda *a, **k: [])
        monkeypatch.setattr(
            orchestrator,
            "filter_new",
            lambda ls: (ls, {}),
        )
        monkeypatch.setattr(
            orchestrator,
            "classify_batch",
            lambda ls: [ClassifierResult("skip", "out_of_scope", "nope") for _ in ls],
        )
        monkeypatch.setattr(config, "assert_required", lambda: None)


def _seed_two_failures(state_dir, source="cedars"):
    """Two prior failed runs — this run's failure is the third, the boundary."""
    source_health.save_health(
        {
            source: {
                "consecutive_failures": 2,
                "last_success": "2026-07-04",
                "last_failure": "2026-08-31",
                "last_error": "session expired",
            }
        }
    )


class TestOrchestratorIntegration:
    def test_alarm_reaches_the_push_on_an_empty_digest(self, monkeypatch, _isolated_state):
        """The whole point: zero listings + a dead source must NOT read as a
        quiet day."""
        _seed_two_failures(_isolated_state)
        h = _Harness(monkeypatch, listings=[], errors={"cedars": "session expired"})

        assert orchestrator.run() == 0

        assert len(h.pushed) == 1
        assert h.pushed[0][0].startswith("🚨")
        assert "cedars" in h.pushed[0][0]
        assert "3 consecutive failed runs" in h.pushed[0][0]

    def test_no_alarm_below_the_threshold_on_the_same_empty_digest(
        self, monkeypatch, _isolated_state
    ):
        source_health.save_health({"cedars": {"consecutive_failures": 1}})
        h = _Harness(monkeypatch, listings=[], errors={"cedars": "session expired"})

        assert orchestrator.run() == 0

        assert len(h.pushed) == 1
        assert not h.pushed[0][0].startswith("🚨")

    def test_counters_are_persisted_on_a_real_run(self, monkeypatch, _isolated_state):
        _seed_two_failures(_isolated_state)
        _Harness(monkeypatch, listings=[], errors={"cedars": "session expired"})

        orchestrator.run()

        stored = source_health.load_health()
        assert stored["cedars"]["consecutive_failures"] == 3
        # The healthy sources were attempted, so they are recorded at zero.
        assert stored["greenhouse"]["consecutive_failures"] == 0

    def test_dry_run_writes_no_counter_state(self, monkeypatch, _isolated_state):
        _seed_two_failures(_isolated_state)
        h = _Harness(monkeypatch, listings=[], errors={"cedars": "session expired"})
        before = (_isolated_state / "source_health.json").read_text()

        assert orchestrator.run(dry_run=True) == 0

        assert (_isolated_state / "source_health.json").read_text() == before
        assert source_health.load_health()["cedars"]["consecutive_failures"] == 2
        assert h.pushed == []

    def test_dry_run_creates_no_state_file_at_all_from_scratch(
        self, monkeypatch, _isolated_state
    ):
        _Harness(monkeypatch, listings=[], errors={"cedars": "session expired"})

        orchestrator.run(dry_run=True)

        assert not (_isolated_state / "source_health.json").exists()

    def test_stub_run_does_not_wipe_a_standing_streak(self, monkeypatch, _isolated_state):
        """--stub makes cedars return canned listings and never fail, so it
        leaves the error map and resets to 0. Writing that zero would let a
        debug run on the live box erase the evidence of the very outage the
        alarm exists to catch. A stub run is not evidence about the real world.
        """
        source_health.save_health(
            {"cedars": {"consecutive_failures": 49, "last_success": "2026-07-04"}}
        )
        # No error map — exactly what stub mode produces for cedars.
        _Harness(monkeypatch, listings=[], errors={})

        assert orchestrator.run(dry_run=False, stub=True) == 0

        assert source_health.load_health()["cedars"]["consecutive_failures"] == 49

    def test_a_real_run_with_the_same_inputs_does_reset(self, monkeypatch, _isolated_state):
        """Control for the test above: it must be --stub doing the protecting,
        not the harness accidentally suppressing the write."""
        source_health.save_health(
            {"cedars": {"consecutive_failures": 49, "last_success": "2026-07-04"}}
        )
        _Harness(monkeypatch, listings=[], errors={})

        assert orchestrator.run(dry_run=False, stub=False) == 0

        assert source_health.load_health()["cedars"]["consecutive_failures"] == 0

    def test_healthy_run_resets_a_standing_alarm(self, monkeypatch, _isolated_state):
        source_health.save_health({"cedars": {"consecutive_failures": 9}})
        h = _Harness(monkeypatch, listings=[_listing("cedars")], errors={})

        orchestrator.run()

        assert source_health.load_health()["cedars"]["consecutive_failures"] == 0
        assert not h.pushed[0][0].startswith("🚨")


class TestDropNoticeUnit:
    """Pruning is right, but it is also the one way to make a LIVE alarm vanish
    without fixing anything: drop the source's config key and the record goes
    with it. The digest cannot show that — the ⚠️ health line is driven by the
    error map and a pruned source is in neither map — and the drop resets the
    re-arm clock, so the source comes back looking brand new and needs another
    ALARM_THRESHOLD runs before it can shout again. One line in the push, no
    schema change, no accrual."""

    _STALE = {"cedars": {"consecutive_failures": 12, "last_success": "2026-07-04"}}

    def test_a_pruned_source_at_the_threshold_produces_the_line(self):
        current = source_health.update_health(
            dict(self._STALE), succeeded=[], errors={}, today=_DAY
        )
        assert current == {}, "premise: the record really was pruned"

        notice = source_health.render_drop_notice(self._STALE, current)

        assert notice is not None
        assert "cedars" in notice
        assert "12-run failure streak" in notice
        assert "dropped from health tracking" in notice

    def test_exactly_at_the_threshold_counts(self):
        prior = {"cedars": {"consecutive_failures": source_health.ALARM_THRESHOLD}}
        assert source_health.render_drop_notice(prior, {}) is not None

    def test_one_below_the_threshold_says_nothing(self):
        """A source pruned while merely wobbling is not news — it never had a
        standing alarm to lose."""
        prior = {"cedars": {"consecutive_failures": source_health.ALARM_THRESHOLD - 1}}
        assert source_health.render_drop_notice(prior, {}) is None

    def test_a_source_that_is_still_tracked_is_not_a_drop(self):
        """Still failing, still in the map, still alarming — not a drop."""
        current = source_health.update_health(
            dict(self._STALE), succeeded=[], errors={"cedars": "still dead"}, today=_DAY
        )
        assert source_health.render_drop_notice(self._STALE, current) is None
        assert source_health.render_alarm(current) is not None

    def test_it_claims_neither_failure_nor_success(self):
        notice = source_health.render_drop_notice(self._STALE, {})
        assert "🚨" not in notice
        assert _DAY.isoformat() not in notice

    def test_worst_streak_leads(self):
        prior = {
            "lever": {"consecutive_failures": 4},
            "cedars": {"consecutive_failures": 20},
            "ashby": {"consecutive_failures": 1},
        }
        lines = source_health.render_drop_notice(prior, {}).splitlines()
        assert len(lines) == 2, "the sub-threshold source must not get a line"
        assert "cedars" in lines[0]
        assert "lever" in lines[1]


class TestDropNoticeThroughTheOrchestrator:
    def test_the_notice_reaches_the_push_on_an_empty_digest(
        self, monkeypatch, _isolated_state
    ):
        """Telegram is the frontend; a journal warning is not a signal here.
        This is the run where the reader is most likely to believe "none today"."""
        source_health.save_health(
            {"cedars": {"consecutive_failures": 12, "last_success": "2026-07-04"}}
        )
        # cedars in NEITHER set — exactly what an unconfigured source produces.
        h = _Harness(
            monkeypatch,
            listings=[],
            errors={},
            succeeded=[s for s in orchestrator.enabled_sources() if s != "cedars"],
        )

        assert orchestrator.run() == 0

        assert len(h.pushed) == 1
        assert any("dropped from health tracking" in m for m in h.pushed[0])
        assert any("12-run failure streak" in m for m in h.pushed[0])

    def test_a_sub_threshold_drop_adds_no_bubble(self, monkeypatch, _isolated_state):
        source_health.save_health({"cedars": {"consecutive_failures": 1}})
        h = _Harness(
            monkeypatch,
            listings=[],
            errors={},
            succeeded=[s for s in orchestrator.enabled_sources() if s != "cedars"],
        )

        assert orchestrator.run() == 0

        assert not any("dropped from health tracking" in m for m in h.pushed[0])

    def test_the_notice_is_self_clearing(self, monkeypatch, _isolated_state):
        """A deliberate disable costs one line, ONCE. The record is gone from
        the state file after run one, so run two has nothing to report."""
        source_health.save_health({"cedars": {"consecutive_failures": 12}})
        others = [s for s in orchestrator.enabled_sources() if s != "cedars"]

        first = _Harness(monkeypatch, listings=[], errors={}, succeeded=others)
        assert orchestrator.run() == 0
        assert any("dropped from health tracking" in m for m in first.pushed[0])

        second = _Harness(monkeypatch, listings=[], errors={}, succeeded=others)
        assert orchestrator.run() == 0
        assert not any("dropped from health tracking" in m for m in second.pushed[0])

    def test_dry_run_writes_nothing_and_pushes_nothing(self, monkeypatch, _isolated_state):
        source_health.save_health({"cedars": {"consecutive_failures": 12}})
        before = (_isolated_state / "source_health.json").read_text()
        h = _Harness(
            monkeypatch,
            listings=[],
            errors={},
            succeeded=[s for s in orchestrator.enabled_sources() if s != "cedars"],
        )

        assert orchestrator.run(dry_run=True) == 0

        assert (_isolated_state / "source_health.json").read_text() == before
        assert h.pushed == []

    def test_the_notice_also_lands_in_the_vault_archive(self):
        md = render_vault_archive(
            surfaced=[],
            skipped=[],
            today=_DAY,
            source_errors=None,
            drop_notice="ℹ️ *cedars* — dropped from health tracking",
        )
        assert "## ℹ️ Dropped from health tracking" in md
        assert "> ℹ️ *cedars* — dropped from health tracking" in md
