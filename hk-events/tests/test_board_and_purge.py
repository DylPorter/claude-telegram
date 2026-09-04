"""The event register, its purge, and the board built off it.

Same property under test as the sibling suite in job-sift: a value meaning
"nothing there" must never be usable as a value meaning "exclude this". Here
that shows up twice — as a room tag nobody set, and as a `last_seen` that
decayed because our crawl moved on rather than because the event went away.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from hk_events import board, config
from hk_events.open_events import (
    OpenEvent,
    age_events,
    purge,
    upcoming,
    upsert_events,
)
from hk_events.render import render
from hk_events.schema import Event

TODAY = date(2026, 9, 4)


def _event(external_id="e1", start=datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc), **kw):
    base = dict(
        source="luma",
        external_id=external_id,
        title="Demo night",
        url="https://example.invalid/e1",
        start=start,
        location="Cyberport",
        organizer="Someone",
    )
    base.update(kw)
    return Event(**base)


def _record(key="luma:e1", **kw):
    base = dict(
        dedup_key=key,
        source="luma",
        title="Demo night",
        url="https://example.invalid/e1",
        starts="2026-09-20",
        starts_at="2026-09-20T18:00:00+00:00",
        location="Cyberport",
        organizer="Someone",
        first_seen="2026-09-01",
        last_seen="2026-09-01",
    )
    base.update(kw)
    return OpenEvent(**base)


class TestTheRegisterCapturesBroadly:
    def test_an_unclassified_event_is_still_recorded(self):
        merged = upsert_events([], [(_event(), None, None)], TODAY)
        assert len(merged) == 1 and merged[0].room is None

    def test_a_dropped_event_is_recorded_with_its_tag_not_deleted(self):
        """The precision-biased filter is a taste decision. It is reasonable to
        apply to a push notification and unreasonable to apply to an archive,
        so `drop` becomes a facet value rather than a delete."""
        merged = upsert_events([], [(_event(), "drop", "not relevant")], TODAY)
        assert merged[0].room == "drop"

    def test_a_later_silent_run_does_not_erase_an_earlier_tag(self):
        first = upsert_events([], [(_event(), "founder_ai", "why")], TODAY)
        second = upsert_events(first, [(_event(), None, None)], TODAY)
        assert second[0].room == "founder_ai"

    def test_re_sighting_bumps_last_seen_and_keeps_first_seen(self):
        first = upsert_events([], [(_event(), None, None)], date(2026, 9, 1))
        second = upsert_events(first, [(_event(), None, None)], TODAY)
        assert second[0].first_seen == "2026-09-01"
        assert second[0].last_seen == TODAY.isoformat()

    def test_a_garbage_room_in_the_state_file_reads_as_untagged(self):
        assert OpenEvent.from_dict({"dedup_key": "luma:e1", "room": 7}).room is None


class TestAgeing:
    def test_a_past_event_becomes_past(self):
        assert age_events([_record(starts="2026-09-01")], TODAY)[0].status == "past"

    def test_an_undated_event_is_never_guessed_to_have_happened(self):
        assert age_events([_record(starts=None)], TODAY)[0].status == "open"

    def test_upcoming_puts_undated_last(self):
        rows = [_record("luma:a", starts=None), _record("luma:b", starts="2026-09-10")]
        assert [r.dedup_key for r in upcoming(rows)] == ["luma:b", "luma:a"]


class TestThePurge:
    def test_an_event_that_already_happened_leaves(self):
        kept, dropped = purge([_record(starts="2026-08-01")], TODAY, past_after_days=3)
        assert kept == [] and "started" in dropped[0][1]

    def test_an_event_from_yesterday_stays_a_few_days(self):
        kept, dropped = purge([_record(starts="2026-09-03")], TODAY, past_after_days=3)
        assert len(kept) == 1 and dropped == []

    def test_a_future_start_vetoes_both_clocks(self):
        """`last_seen` is a fact about our crawl; the start date is a fact
        about the world. The luma_discover city page shows about a dozen
        events at a time, so anything further out silently stops being
        re-sighted long before it happens."""
        record = _record(starts="2026-12-01", first_seen="2026-01-01", last_seen="2026-01-01")
        kept, dropped = purge([record], TODAY, unseen_after_days=1, max_age_days=1)
        assert len(kept) == 1 and dropped == []

    def test_an_undated_event_nobody_lists_any_more_leaves(self):
        record = _record(starts=None, last_seen="2026-07-01")
        kept, dropped = purge([record], TODAY, unseen_after_days=30)
        assert kept == [] and "not listed" in dropped[0][1]

    def test_an_undated_event_older_than_two_months_leaves(self):
        """...unless a source listed it TODAY — see
        TestASightingTodayVetoesTheMaxAgeClock."""
        record = _record(starts=None, first_seen="2026-06-01", last_seen="2026-08-25")
        kept, dropped = purge([record], TODAY, unseen_after_days=30, max_age_days=60)
        assert kept == [] and "first seen" in dropped[0][1]

    @pytest.mark.parametrize("bad", ["", "not-a-date", "2026-13-45"])
    def test_an_unreadable_date_keeps_the_row(self, bad):
        kept, dropped = purge(
            [_record(starts=bad, last_seen=bad, first_seen=bad)], TODAY, unseen_after_days=1
        )
        assert len(kept) == 1 and dropped == []

    def test_every_drop_carries_a_reason(self):
        _, dropped = purge([_record(starts="2026-01-01")], TODAY)
        assert dropped[0][1], "a silent purge is indistinguishable from a capture failure"


class TestTheBoard:
    def test_no_external_resources_at_all(self):
        html = board.build_board([_record()], TODAY)
        for forbidden in ("<script src", '<link rel="stylesheet"', "cdn.", "fetch("):
            assert forbidden not in html

    def test_the_data_cannot_terminate_its_own_script_tag(self):
        html = board.build_board([_record(title="</script><script>x</script>")], TODAY)
        assert "</script><script>x" not in html

    def test_an_untagged_event_is_still_in_the_data(self):
        section = board.events_section([_record(room=None)])
        assert len(section.rows) == 1 and section.rows[0]["room"] is None

    def test_the_view_always_states_showing_n_of_m(self):
        html = board.build_board([_record()], TODAY)
        assert '"showing " + sorted.length + " of " + section.rows.length' in html

    def test_a_missing_jobs_feed_is_unavailable_not_empty(self, tmp_path):
        rows, note = board.read_jobs_feed(tmp_path / "nope.json")
        assert rows is None
        assert board.jobs_section(rows, note).available is False

    def test_an_empty_jobs_feed_is_available_and_empty(self, tmp_path):
        path = tmp_path / "jobs_feed.json"
        path.write_text(json.dumps({"generated": "2026-09-04", "jobs": []}))
        rows, note = board.read_jobs_feed(path)
        assert rows == [] and board.jobs_section(rows, note).available is True

    def test_an_unreadable_jobs_feed_is_unavailable(self, tmp_path):
        path = tmp_path / "jobs_feed.json"
        path.write_text("{not json")
        rows, note = board.read_jobs_feed(path)
        assert rows is None and "could not be read" in note

    def test_the_feed_round_trips(self, tmp_path):
        path = tmp_path / "events_feed.json"
        board.write_feed(path, [_record()], TODAY)
        payload = json.loads(path.read_text())
        assert payload["generated"] == TODAY.isoformat()
        assert payload["events"][0]["title"] == "Demo night"


class TestTelegramIsOneBubble:
    def test_a_normal_run_pushes_exactly_one_message(self):
        from hk_events.schema import RelevanceResult

        messages = render(
            surfaced=[(_event(), RelevanceResult("founder_ai", "ok"), "new")],
            total_new=1,
            total_processed=30,
            calendar_stats=None,
            today=TODAY,
            board_path="/tmp/board.html",
            upcoming_count=12,
        )
        assert len(messages) == 1
        assert "1 new" in messages[0] and "12 upcoming" in messages[0]
        assert "/tmp/board.html" in messages[0]

    def test_the_staleness_alarm_is_exempt_and_still_leads(self):
        messages = render(
            surfaced=[], total_new=0, total_processed=0, calendar_stats=None,
            today=TODAY, staleness_alarm="🚨 luma has failed 3 runs",
        )
        assert messages[0].startswith("🚨") and len(messages) == 2

    def test_the_source_health_line_is_exempt(self):
        messages = render(
            surfaced=[], total_new=0, total_processed=0, calendar_stats=None,
            today=TODAY, source_errors={"luma": "feed 404"},
        )
        assert any("⚠️" in m for m in messages)

    def test_a_missing_board_path_is_stated_not_omitted(self):
        messages = render(
            surfaced=[], total_new=0, total_processed=0, calendar_stats=None, today=TODAY
        )
        assert "not written this run" in messages[0]


class TestDryRunWritesNoBoard:
    def test_dry_run_writes_no_file(self, monkeypatch, tmp_path):
        from hk_events import orchestrator

        target = tmp_path / "board.html"
        monkeypatch.setattr(config, "board_path", lambda: target)
        monkeypatch.setattr(config, "events_feed_path", lambda: tmp_path / "feed.json")
        monkeypatch.setattr(config, "jobs_feed_path", lambda: tmp_path / "missing.json")
        result = orchestrator._write_board([_record()], TODAY, dry_run=True)
        assert result.path is None and result.problem == "dry run"
        assert not target.exists() and not (tmp_path / "feed.json").exists()

    def test_a_real_run_writes_both(self, monkeypatch, tmp_path):
        from hk_events import orchestrator

        target = tmp_path / "board.html"
        monkeypatch.setattr(config, "board_path", lambda: target)
        monkeypatch.setattr(config, "events_feed_path", lambda: tmp_path / "feed.json")
        monkeypatch.setattr(config, "jobs_feed_path", lambda: tmp_path / "missing.json")
        result = orchestrator._write_board([_record()], TODAY, dry_run=False)
        assert result.path == target and result.problem is None
        assert "<!DOCTYPE html>" in target.read_text()
        assert (tmp_path / "feed.json").exists()

    def test_a_board_failure_reports_that_cause_and_not_a_guessed_one(
        self, monkeypatch, tmp_path
    ):
        from hk_events import orchestrator

        monkeypatch.setattr(config, "board_path", lambda: tmp_path / "board.html")
        monkeypatch.setattr(config, "events_feed_path", lambda: tmp_path / "feed.json")
        monkeypatch.setattr(orchestrator.board_mod, "build_board", lambda *a, **k: 1 / 0)
        result = orchestrator._write_board([_record()], TODAY, dry_run=False)
        assert result.path is None and "could not be written" in result.problem
        bubble = render(
            surfaced=[], total_new=0, total_processed=0, calendar_stats=None,
            today=TODAY, board_path=result.path, board_problem=result.problem,
        )[0]
        assert "could not be written" in bubble
        assert "no board path configured" not in bubble


class TestTestsDoNotTouchRealState:
    """Mirror of job-sift's guard: a test that patches only SOME of the write
    paths silently clobbers real state on every `pytest` run."""

    def test_the_real_state_dir_is_untouched_by_this_suite(self):
        feed = config.STATE_DIR / "events_feed.json"
        register = config.STATE_DIR / "open_events.json"
        assert not feed.exists() and not register.exists(), (
            "the test suite wrote real state — patch config.events_feed_path "
            "(and STATE_DIR) in whichever test calls _write_board"
        )


class TestAnUnreadableStartAlsoVetoes:
    """IMPORTANT 2 from review, applied here too. `start_date` returned None
    for both "no start" and "I could not parse the start", so the future-start
    veto silently did not apply to a row whose date we failed to read."""

    @pytest.mark.parametrize("bad", ["20 Sep 2026", "2026/09/20", "soon", "2026-13-45"])
    def test_a_malformed_start_keeps_the_row(self, bad):
        record = _record(starts=bad, first_seen="2026-01-01", last_seen="2026-01-01")
        kept, dropped = purge([record], TODAY, unseen_after_days=30, max_age_days=60)
        assert dropped == [], f"{bad!r} was treated as evidence the event is over"
        assert len(kept) == 1

    def test_the_three_start_states_are_distinguishable(self):
        assert _record(starts=None).start_state == ("none", None)
        assert _record(starts="20 Sep 2026").start_state == ("unreadable", None)
        assert _record(starts="2026-09-20").start_state[0] == "known"


class TestASightingTodayVetoesTheMaxAgeClock:
    def test_an_undated_event_seen_today_survives_the_age_clock(self):
        record = _record(starts=None, first_seen="2026-06-01", last_seen=TODAY.isoformat())
        kept, dropped = purge([record], TODAY, max_age_days=60)
        assert dropped == [] and len(kept) == 1

    def test_an_intermittently_sighted_old_undated_event_is_still_purged(self):
        record = _record(starts=None, first_seen="2026-06-01", last_seen="2026-08-25")
        kept, dropped = purge([record], TODAY, unseen_after_days=30, max_age_days=60)
        assert kept == [] and "first seen" in dropped[0][1]


class TestTheBoardIsWrittenAtomically:
    def test_no_tmp_file_is_left_behind(self, tmp_path):
        out = tmp_path / "board.html"
        board.write_board(out, board.build_board([_record()], TODAY))
        assert [p.name for p in tmp_path.iterdir()] == ["board.html"]

    def test_a_failed_write_leaves_the_previous_board_intact(self, tmp_path, monkeypatch):
        import os

        out = tmp_path / "board.html"
        out.write_text("<!DOCTYPE html>OLD")
        real = os.fdopen

        def _boom(fd, *a, **k):
            real(fd, *a, **k).close()
            raise OSError("disk full")

        monkeypatch.setattr(os, "fdopen", _boom)
        with pytest.raises(OSError):
            board.write_board(out, "NEW")
        assert out.read_text() == "<!DOCTYPE html>OLD"
