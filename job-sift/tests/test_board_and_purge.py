"""The capture inversion: tags that cannot gate, a purge that cannot eat a
hand-set mark, and a board that cannot hide a row for being untagged.

Every test here is about the SAME property from a different angle, and it is
the property the redesign exists to establish: a value meaning "nothing there"
must never be usable as a value meaning "exclude this". The old classifier let
one keyword list delete a role forever; the new failure mode would be a tag
doing it quietly in the UI instead, which would be strictly worse because
nothing would even log it.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from job_sift import board, classifier, tags
from job_sift.board_html import Section, render_board
from job_sift.open_roles import (
    OpenRole,
    prune,
    purge,
    upsert_roles,
)
from job_sift.render import render
from job_sift.schema import ClassifierResult, JobListing

TODAY = date(2026, 9, 4)


def _role(key="cedars:1", **kw):
    base = dict(
        dedup_key=key,
        source="cedars",
        employer="Acme",
        title="Software Engineer",
        apply_url="https://example.invalid/1",
        deadline=None,
        first_seen="2026-09-01",
        last_seen="2026-09-01",
        reason="because",
    )
    base.update(kw)
    return OpenRole(**base)


def _listing(title="Software Engineer", **kw):
    base = dict(
        source="cedars",
        external_id="1",
        employer="Acme",
        title=title,
        apply_url="https://example.invalid/1",
    )
    base.update(kw)
    return JobListing(**base)


# ---------------------------------------------------------------------------
# Tags are advisory
# ---------------------------------------------------------------------------


class TestATagNeverBecomesAGuess:
    @pytest.mark.parametrize("title", ["Software Engineer", "Data Scientist", "Quant Developer"])
    def test_an_untyped_title_stays_untyped(self, title):
        """NOT "full-time". A bare title names no engagement shape, and
        defaulting it to the one role type a student reader is most likely to
        have filtered out would hide the role behind a tag nobody asserted."""
        assert tags.derive_role_type(title) is None

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Summer Intern, Platform", "intern"),
            ("Part-time Data Annotator", "part-time"),
            ("AI Engineer (12-month contract)", "contract"),
            ("Graduate Trainee Programme", "rotational"),
            ("Research Assistant, Computer Science", "research-assistant"),
            ("Permanent Full-time Engineer", "full-time"),
        ],
    )
    def test_the_shapes_it_does_recognise(self, title, expected):
        assert tags.derive_role_type(title) == expected

    def test_precedence_prefers_the_more_specific_shape(self):
        assert tags.derive_role_type("Summer Internship (6-month contract)") == "intern"
        # A title naming both a role kind and a schedule resolves to the kind:
        # "research assistant" is what the job IS, "part-time" is how much of
        # it there is, and the reader filtering for RA work would not find it
        # under "part-time".
        assert tags.derive_role_type("Part-time Research Assistant") == "research-assistant"

    @pytest.mark.parametrize("value", [None, "", "  ", "unknown", "n/a", "other", 7, True, {"a": 1}])
    def test_an_unusable_industry_is_untagged_not_invented(self, value):
        assert tags.clean_tag(value) is None

    @pytest.mark.parametrize("value", [None, "", "maybe", "sort of", 7, [], {}])
    def test_an_unparseable_technical_flag_is_none_not_false(self, value):
        """False and None are different claims — "I looked and it is not
        technical" versus "nobody said". Collapsing them would hide unjudged
        roles from a reader filtering `technical = yes`."""
        assert tags.clean_bool(value) is None

    def test_the_flag_does_parse_when_it_is_really_there(self):
        assert tags.clean_bool(True) is True
        assert tags.clean_bool("no") is False


class TestGarbageTagsDoNotAffectTheVerdict:
    def test_coerce_keeps_the_verdict_and_drops_only_the_tags(self):
        result = classifier._coerce(
            "prestige", "in_scope", "ok", {"industry": "unknown", "is_technical": "maybe"}
        )
        assert result.surface, "a bad tag must not cost the listing its capture"
        assert result.industry is None
        assert result.is_technical is None

    def test_a_missing_tag_block_entirely_is_fine(self):
        result = classifier._coerce("skip", "in_scope", "ok")
        assert result.surface
        assert (result.industry, result.is_technical, result.role_type) == (None, None, None)

    def test_stamp_tags_is_idempotent(self):
        listing = _listing("Summer Intern, Platform")
        once = classifier.stamp_tags(listing, ClassifierResult("skip", "in_scope", "r"))
        assert once.role_type == "intern"
        assert classifier.stamp_tags(listing, once) == once


class TestTagsSurviveIntoTheRegisterWithoutGating:
    def test_an_untagged_listing_still_lands_in_the_register(self):
        merged = upsert_roles([], [(_listing(), "why", "broad", {})], TODAY)
        assert len(merged) == 1
        role = merged[0]
        assert (role.role_type, role.industry, role.is_technical) == (None, None, None)

    def test_a_later_silent_run_does_not_erase_an_earlier_tag(self):
        """"No answer today" must not overwrite yesterday's answer. The
        classifier that re-sighted the role may simply not have been asked."""
        first = upsert_roles(
            [], [(_listing(), "why", "broad", {"industry": "banking", "is_technical": True})], TODAY
        )
        second = upsert_roles(first, [(_listing(), "why", "broad", {})], TODAY)
        assert second[0].industry == "banking"
        assert second[0].is_technical is True

    def test_a_garbage_is_technical_in_the_state_file_reads_as_untagged(self):
        role = OpenRole.from_dict({"dedup_key": "cedars:1", "is_technical": "yes-ish"})
        assert role.is_technical is None


# ---------------------------------------------------------------------------
# The purge
# ---------------------------------------------------------------------------


class TestThePurge:
    def test_a_role_the_source_stopped_listing_is_dropped(self):
        kept, dropped = purge([_role(last_seen="2026-07-01")], TODAY, unseen_after_days=30)
        assert kept == []
        assert len(dropped) == 1 and "not listed" in dropped[0][1]

    def test_a_role_older_than_two_months_is_dropped(self):
        """...but NOT one the source listed TODAY — see
        TestASightingTodayVetoesTheMaxAgeClock. A sighting in this very run is
        the strongest evidence available that the role is live, so the fixture
        here is an intermittently-sighted row: under the unseen threshold, over
        the age one."""
        kept, dropped = purge(
            [_role(first_seen="2026-06-01", last_seen="2026-08-25")],
            TODAY,
            unseen_after_days=30,
            max_age_days=60,
        )
        assert kept == []
        assert "first seen" in dropped[0][1]

    def test_a_fresh_role_survives_both_clocks(self):
        kept, dropped = purge([_role()], TODAY)
        assert len(kept) == 1 and dropped == []

    @pytest.mark.parametrize("status", ["applied", "dismissed"])
    def test_a_hand_set_mark_survives_both_rules(self, status):
        """⚠️ The one rule here that is not a heuristic. Both marks are the
        operator's own and neither is reconstructible from anything else on
        disk: an application is a fact about what he did, and a dismissal is
        what stops a role he already refused from being handed back every time
        its source re-lists it."""
        role = _role(status=status, first_seen="2026-01-01", last_seen="2026-01-01")
        kept, dropped = purge([role], TODAY, unseen_after_days=1, max_age_days=1)
        assert dropped == []
        assert [r.status for r in kept] == [status]

    @pytest.mark.parametrize("bad", ["", "not-a-date", "2026-13-45"])
    def test_an_unreadable_date_keeps_the_row(self, bad):
        """An unparseable `last_seen` is not evidence the source stopped
        listing the role — it is evidence we cannot tell. The safe direction
        for a delete is not to delete."""
        kept, dropped = purge([_role(last_seen=bad, first_seen=bad)], TODAY, unseen_after_days=1)
        assert len(kept) == 1 and dropped == []

    def test_the_reason_names_the_rule_that_fired(self):
        _, dropped = purge([_role(last_seen="2026-01-01")], TODAY, unseen_after_days=30)
        assert "30" in dropped[0][1], "a silent purge is indistinguishable from a capture failure"

    def test_prune_keeps_dismissed_forever_too(self):
        role = _role(status="dismissed", last_seen="2020-01-01")
        assert prune([role], TODAY, keep_days=1) == [role]


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


def _html(roles, **kw):
    return board.build_board(roles, TODAY, **kw)


class TestTheBoardIsSelfContained:
    def test_no_external_resources_at_all(self):
        html = _html([_role()])
        for forbidden in ("<script src", "<link rel=\"stylesheet\"", "cdn.", "fetch(", "XMLHttpRequest"):
            assert forbidden not in html, f"the board must open from disk: found {forbidden!r}"

    def test_the_data_cannot_terminate_its_own_script_tag(self):
        html = _html([_role(title="</script><script>alert(1)</script>")])
        assert "</script><script>alert(1)" not in html
        assert "\\u003c/script" in html

    def test_it_is_one_document_with_two_tabs(self):
        html = _html([_role()])
        assert html.count("<!DOCTYPE html>") == 1
        assert 'id="tab-jobs"' in html and 'id="tab-events"' in html


class TestNoRowIsHiddenForBeingUntagged:
    def test_a_completely_untagged_role_is_still_in_the_data(self):
        section = board.jobs_section([_role()], TODAY)
        assert len(section.rows) == 1
        row = section.rows[0]
        assert row["role_type"] is None and row["industry"] is None and row["technical"] is None

    def test_a_none_technical_flag_is_not_rendered_as_the_string_none(self):
        """`str(None)` is "None", which would appear in the dropdown as a real
        answer. Absent has to stay absent all the way to the cell."""
        assert board._tri(None) is None
        assert (board._tri(True), board._tri(False)) == ("yes", "no")

    def test_every_status_is_present_rather_than_pre_filtered(self):
        roles = [
            _role("cedars:1"),
            _role("cedars:2", status="expired"),
            _role("cedars:3", status="dismissed"),
        ]
        section = board.jobs_section(roles, TODAY)
        assert {r["status"] for r in section.rows} == {"open", "expired", "dismissed"}

    def test_the_view_always_states_showing_n_of_m(self):
        html = _html([_role()])
        assert '"showing " + sorted.length + " of " + section.rows.length' in html


class TestTheEventsTabIsHonestAboutMissingData:
    def test_a_missing_feed_is_unavailable_not_empty(self, tmp_path):
        rows, note = board.read_events_feed(tmp_path / "nope.json")
        assert rows is None
        section = board.events_section(rows, note)
        assert section.available is False
        assert "nope.json" in (section.note or "")

    def test_an_unreadable_feed_is_unavailable_not_empty(self, tmp_path):
        path = tmp_path / "events_feed.json"
        path.write_text("{not json")
        rows, note = board.read_events_feed(path)
        assert rows is None and "could not be read" in note

    def test_a_genuinely_empty_feed_is_available_and_empty(self, tmp_path):
        path = tmp_path / "events_feed.json"
        path.write_text(json.dumps({"generated": "2026-09-04", "events": []}))
        rows, note = board.read_events_feed(path)
        assert rows == []
        assert board.events_section(rows, note).available is True

    def test_rows_come_through(self, tmp_path):
        path = tmp_path / "events_feed.json"
        path.write_text(
            json.dumps({"generated": "2026-09-04", "events": [{"title": "Demo night"}]})
        )
        rows, _ = board.read_events_feed(path)
        assert rows == [{"title": "Demo night"}]


class TestFacetsAreDataDriven:
    def test_the_generator_declares_no_facet_values(self):
        """Options are derived from the rows in the browser, which is what
        lets a different reader — one who wants design and art roles — get
        useful dropdowns out of the same generator with no code change."""
        html = render_board(
            [Section(key="x", label="X", rows=[{"a": "design"}])],
            generated_on=TODAY,
            title="t",
        )
        assert "design" in html
        assert "function optionsFor(rows, key)" in html


# ---------------------------------------------------------------------------
# Telegram is a pointer
# ---------------------------------------------------------------------------


class TestTelegramIsOneBubble:
    def test_a_normal_run_pushes_exactly_one_message(self):
        surfaced = [(_listing(), ClassifierResult("skip", "in_scope", "ok", lane="broad"))]
        messages = render(
            surfaced=surfaced,
            skipped=[],
            total_new=1,
            total_processed=40,
            today=TODAY,
            open_roles=[_role()],
            board_path="/tmp/board.html",
        )
        assert len(messages) == 1
        assert "1 new" in messages[0] and "1 open" in messages[0]
        assert "/tmp/board.html" in messages[0]

    def test_the_near_miss_concept_is_gone(self):
        skipped = [(_listing(), ClassifierResult("prestige", "out_of_scope", "wrong shape"))]
        blob = "\n".join(
            render(
                surfaced=[], skipped=skipped, total_new=1, total_processed=1, today=TODAY
            )
        )
        assert "near miss" not in blob.lower()
        assert "wrong shape" not in blob

    def test_the_staleness_alarm_is_exempt_and_still_leads(self):
        messages = render(
            surfaced=[], skipped=[], total_new=0, total_processed=0, today=TODAY,
            staleness_alarm="🚨 cedars has failed 3 runs",
        )
        assert messages[0].startswith("🚨")
        assert len(messages) == 2

    def test_the_source_health_line_is_exempt_and_still_pushes(self):
        messages = render(
            surfaced=[], skipped=[], total_new=0, total_processed=0, today=TODAY,
            source_errors={"cedars": "auth failed"},
        )
        assert any("⚠️" in m for m in messages)

    def test_a_missing_board_path_is_stated_not_omitted(self):
        messages = render(
            surfaced=[], skipped=[], total_new=0, total_processed=0, today=TODAY
        )
        assert "not written this run" in messages[0]


class TestDryRunWritesNoBoard:
    def test_dry_run_writes_no_file_and_reports_the_reason(self, monkeypatch, tmp_path):
        """--dry-run writes no state, pushes nothing, and writes no board.
        The board is a file on disk like any other output — and so is the feed
        the OTHER service reads, which is why the feed path is patched too."""
        from job_sift import config, orchestrator

        target = tmp_path / "board.html"
        feed = tmp_path / "jobs_feed.json"
        monkeypatch.setattr(config, "board_path", lambda: target)
        monkeypatch.setattr(config, "jobs_feed_path", lambda: feed)
        monkeypatch.setattr(config, "events_feed_path", lambda: tmp_path / "events.json")
        result = orchestrator._write_board([_role()], TODAY, dry_run=True)
        assert result.path is None and result.problem == "dry run"
        assert not target.exists() and not feed.exists()

    def test_a_real_run_writes_the_board_and_the_feed(self, monkeypatch, tmp_path):
        from job_sift import config, orchestrator

        target = tmp_path / "sub" / "board.html"
        feed = tmp_path / "jobs_feed.json"
        monkeypatch.setattr(config, "board_path", lambda: target)
        monkeypatch.setattr(config, "jobs_feed_path", lambda: feed)
        monkeypatch.setattr(config, "events_feed_path", lambda: tmp_path / "missing.json")
        result = orchestrator._write_board([_role()], TODAY, dry_run=False)
        assert result.path == target and result.problem is None
        assert "<!DOCTYPE html>" in target.read_text()
        assert json.loads(feed.read_text())["jobs"], "the feed hk-events reads"

    def test_a_board_failure_reports_that_cause_and_not_a_guessed_one(
        self, monkeypatch, tmp_path
    ):
        """The board is a VIEW of state that is already safely persisted, so a
        render failure must not take down a run that has fetched, classified,
        pushed and saved — and the push must say what actually happened. It
        used to print "no board path configured" for this branch, which is a
        cause the code never checked."""
        from job_sift import config, orchestrator

        monkeypatch.setattr(config, "board_path", lambda: tmp_path / "board.html")
        monkeypatch.setattr(config, "jobs_feed_path", lambda: tmp_path / "jobs_feed.json")
        monkeypatch.setattr(orchestrator.board_mod, "build_board", lambda *a, **k: 1 / 0)
        result = orchestrator._write_board([_role()], TODAY, dry_run=False)
        assert result.path is None
        assert "could not be written" in result.problem
        assert not (tmp_path / "board.html").exists()

        bubble = render(
            surfaced=[], skipped=[], total_new=0, total_processed=0, today=TODAY,
            board_path=result.path, board_problem=result.problem,
        )[0]
        assert "could not be written" in bubble
        assert "no board path configured" not in bubble

    def test_a_missing_board_path_still_says_so(self, monkeypatch, tmp_path):
        from job_sift import config, orchestrator

        monkeypatch.setattr(config, "board_path", lambda: None)
        monkeypatch.setattr(config, "jobs_feed_path", lambda: tmp_path / "jobs_feed.json")
        result = orchestrator._write_board([_role()], TODAY, dry_run=False)
        assert result.problem == "no board path configured"


class TestTestsDoNotTouchRealState:
    """IMPORTANT 3 from review: a test in this file patched `board_path` and
    `events_feed_path` but not `jobs_feed_path`, so every `pytest` run wrote
    `.data/state/jobs_feed.json` with a fixture row — clobbering, in a
    production checkout, the exact file hk-events renders as its Jobs tab.

    This asserts the property rather than the fix, so a future test that
    forgets the same patch fails here.
    """

    def test_the_real_state_dir_is_untouched_by_this_suite(self):
        from job_sift import config

        feed = config.STATE_DIR / "jobs_feed.json"
        assert not feed.exists(), (
            f"{feed} was written by the test suite — patch config.jobs_feed_path "
            "in whichever test calls _write_board"
        )


class TestAFutureDeadlineVetoesThePurge:
    """Reproduces the live-data failure found while generating the first real
    board: the unseen rule alone deleted eleven roles whose deadlines were
    three weeks out, because the CEDARS crawl had moved past them."""

    def test_an_unseen_role_with_a_future_deadline_is_kept(self):
        role = _role(last_seen="2026-07-31", deadline="2026-09-27")
        kept, dropped = purge([role], TODAY, unseen_after_days=30)
        assert dropped == [] and len(kept) == 1

    def test_an_old_role_with_a_future_deadline_is_kept(self):
        role = _role(first_seen="2026-01-01", last_seen="2026-01-01", deadline="2026-12-01")
        kept, dropped = purge([role], TODAY, unseen_after_days=1, max_age_days=1)
        assert dropped == [] and len(kept) == 1

    def test_the_veto_lifts_the_day_after_the_deadline(self):
        role = _role(last_seen="2026-07-31", deadline="2026-09-03")
        kept, dropped = purge([role], TODAY, unseen_after_days=30)
        assert kept == [] and len(dropped) == 1

    def test_a_deadline_today_still_counts_as_open(self):
        role = _role(last_seen="2026-07-31", deadline=TODAY.isoformat())
        kept, _ = purge([role], TODAY, unseen_after_days=30)
        assert len(kept) == 1

    def test_a_role_with_no_deadline_is_still_governed_by_the_clocks(self):
        kept, dropped = purge([_role(last_seen="2026-07-01")], TODAY, unseen_after_days=30)
        assert kept == [] and len(dropped) == 1


class TestAnUnreadableDeadlineAlsoVetoes:
    """IMPORTANT 2 from review. `deadline_date` returns None for BOTH "no
    deadline" and "I could not parse the deadline", so the veto that stops the
    purge deleting live roles silently did not apply to any row whose date we
    failed to read — one-value-two-meanings rebuilt inside the fix for it.

    It bites hardest here because the register is documented as hand-editable:
    typing "30 Sep 2026" into the JSON purged the row.
    """

    @pytest.mark.parametrize("bad", ["30 Sep 2026", "2026/09/30", "next Friday", "2026-13-45"])
    def test_a_malformed_deadline_keeps_the_row(self, bad):
        role = _role(last_seen="2026-01-01", first_seen="2026-01-01", deadline=bad)
        kept, dropped = purge([role], TODAY, unseen_after_days=30, max_age_days=60)
        assert dropped == [], f"{bad!r} was treated as evidence the role closed"
        assert len(kept) == 1

    def test_the_three_deadline_states_are_distinguishable(self):
        assert _role(deadline=None).deadline_state == ("none", None)
        assert _role(deadline="30 Sep 2026").deadline_state == ("unreadable", None)
        assert _role(deadline="2026-09-30").deadline_state[0] == "known"

    def test_an_absent_deadline_is_still_governed_by_the_clocks(self):
        """The distinction has to cut both ways, or it is just a way of never
        purging anything."""
        kept, dropped = purge([_role(deadline=None, last_seen="2026-01-01")], TODAY)
        assert kept == [] and len(dropped) == 1


class TestASightingTodayVetoesTheMaxAgeClock:
    """IMPORTANT 5 from review. The max-age rule deleted a role whose
    `last_seen` was today — inverting the very argument the deadline veto is
    built on, that the source's own statement beats our inference."""

    def test_a_role_seen_today_survives_the_age_clock(self):
        role = _role(first_seen="2026-06-01", last_seen=TODAY.isoformat())
        kept, dropped = purge([role], TODAY, max_age_days=60)
        assert dropped == [] and len(kept) == 1

    def test_an_intermittently_sighted_old_role_is_still_purged(self):
        """What the max-age clock still catches, and why it is kept: seen ten
        days ago, first seen seventy — under the unseen threshold and over the
        age one."""
        role = _role(first_seen="2026-06-01", last_seen="2026-08-25")
        kept, dropped = purge([role], TODAY, unseen_after_days=30, max_age_days=60)
        assert kept == [] and "first seen" in dropped[0][1]

    @pytest.mark.parametrize("status", ["applied", "dismissed"])
    def test_a_hand_set_mark_still_outranks_everything(self, status):
        role = _role(status=status, first_seen="2020-01-01", last_seen="2020-01-01")
        kept, dropped = purge([role], TODAY, unseen_after_days=1, max_age_days=1)
        assert dropped == [] and len(kept) == 1


class TestTheViewTimeRoleTypeFallback:
    def test_a_row_written_before_the_tag_existed_still_gets_one(self):
        row = board.job_row(_role(title="2027 Summer Internship, Trading"), TODAY)
        assert row["role_type"] == "intern"

    def test_a_stored_tag_wins_over_the_derivation(self):
        row = board.job_row(_role(title="Summer Intern", role_type="contract"), TODAY)
        assert row["role_type"] == "contract"

    def test_an_underivable_title_stays_untagged(self):
        assert board.job_row(_role(title="Software Engineer"), TODAY)["role_type"] is None

    def test_industry_and_technical_get_no_fallback(self):
        """There is no pure function that produces them — they come from a
        model. Deriving them here would be fabrication, not computation."""
        row = board.job_row(_role(title="AI Research Intern"), TODAY)
        assert row["industry"] is None and row["technical"] is None

    def test_the_fallback_agrees_with_capture_exactly(self):
        """The docstring used to claim this and it was false: capture also
        scanned the description, so a permanent role whose body mentioned an
        internship programme was tagged `intern` at capture and untagged on
        the board. Capture is title-only now, so the two cannot diverge."""
        listing = _listing(
            "Software Engineer",
            description="Join us! Ask about our summer internship programme.",
        )
        at_capture = classifier.stamp_tags(listing, ClassifierResult("skip", "in_scope", ""))
        on_board = board.job_row(_role(title=listing.title), TODAY)
        assert at_capture.role_type is None, "the body must not mislabel a permanent role"
        assert on_board["role_type"] == at_capture.role_type


class TestTheFunctionTagReplacesTheDeletedGate:
    """CRITICAL 1 from review. The keyword list that used to stamp
    `out_of_scope` — deleting the role permanently, with near-misses
    terminated — is now a tag."""

    @pytest.mark.parametrize(
        "title,function",
        [
            ("Marketing Intern", "marketing"),
            ("Data Analyst Intern", "analyst"),
            ("Sales Development Representative", "sales"),
            ("Talent Acquisition Intern", "talent acquisition"),
        ],
    )
    def test_the_keywords_information_survives_as_a_tag(self, title, function):
        tagged = classifier.stamp_tags(_listing(title), ClassifierResult("skip", "in_scope", ""))
        assert tagged.function == function
        assert tagged.surface, "and the role is captured, not deleted"

    def test_it_reaches_the_register_and_the_board(self):
        merged = upsert_roles(
            [], [(_listing("Marketing Intern"), "why", "broad", {"function": "marketing"})], TODAY
        )
        assert merged[0].function == "marketing"
        section = board.jobs_section(merged, TODAY)
        assert section.rows[0]["function"] == "marketing"
        assert "function" in {f.key for f in section.facets}

    def test_an_untagged_row_is_not_hidden_by_the_new_facet(self):
        section = board.jobs_section([_role(title="Software Engineer")], TODAY)
        assert len(section.rows) == 1 and section.rows[0]["function"] is None


class TestTheBoardIsWrittenAtomically:
    def test_no_tmp_file_is_left_behind(self, tmp_path):
        out = tmp_path / "board.html"
        board.write_board(out, _html([_role()]))
        assert [p.name for p in tmp_path.iterdir()] == ["board.html"]

    def test_a_failed_write_leaves_the_previous_board_intact(self, tmp_path, monkeypatch):
        """A half-written page still OPENS, showing whatever rows made it
        before the cut with nothing to say the rest is missing — a silent
        partial result, which is the one output shape this codebase refuses to
        produce. The feed was already atomic; the page was not."""
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
        assert [p.name for p in tmp_path.iterdir()] == ["board.html"]


class TestTheFunctionTagIsReadable:
    def test_the_prefix_marker_never_reaches_a_dropdown(self):
        """`negative_titles` is a MATCHER vocabulary: `business develop*` covers
        development and developer. Printing the glob shows the reader a pattern
        where a category should be — caught on the live register, which
        produced a literal "business develop*" option."""
        assert tags.clean_function("business develop*") == "business develop"
        tagged = classifier.stamp_tags(
            _listing("Business Development Intern"), ClassifierResult("skip", "in_scope", "")
        )
        assert tagged.function == "business develop"
        assert "*" not in board.job_row(_role(title="Business Development Intern"), TODAY)["function"]

    def test_a_term_without_a_marker_is_left_alone(self):
        """The tag has to stay traceable to the list entry that produced it, so
        nothing beyond the marker is rewritten."""
        assert tags.clean_function("talent acquisition") == "talent acquisition"
