"""Duplicate listings must collapse to one row — issue #1b.

Two failure shapes from the live register:

  * one source re-listing the same posting under a NEW id (LinkedIn reposts get
    a fresh job id, so the seen-set — keyed on the id — cannot tell), and
  * the same posting arriving from two different sources.

Only the FIRST is fixable with a key worth trusting; see
`JobListing.identity_key` for why the cross-source half is deliberately
declined. These tests pin both halves: that same-source duplicates collapse,
and that cross-source rows are NEVER merged.

The collapse must also be invisible to the seen-set: whichever row survives, the
dropped row's id has to end up recorded, or the next run's hand-off re-notifies.
"""

from __future__ import annotations

from datetime import date

import pytest

from job_sift.dedupe import collapse_duplicates, mirror_collapsed, withhold_unclassified
from job_sift.open_roles import OpenRole, collapse_register
from job_sift.schema import ClassifierResult, JobListing

TODAY = date(2026, 9, 1)


def _listing(source, ext_id, employer, title, location="Hong Kong", deadline=None):
    return JobListing(
        source=source,
        external_id=ext_id,
        employer=employer,
        title=title,
        apply_url=f"https://example.test/{source}/{ext_id}",
        location=location,
        deadline=deadline,
    )


# --------------------------------------------------------------------------
# identity_key
# --------------------------------------------------------------------------


class TestIdentityKey:
    def test_punctuation_and_case_do_not_split_one_posting(self):
        a = _listing("linkedin", "1", "IMC Trading", "Software Engineer Intern")
        b = _listing("linkedin", "2", "IMC  trading.", "software engineer intern")
        assert a.identity_key == b.identity_key

    def test_two_sources_never_share_an_identity(self):
        """The declined half, asserted so nobody quietly enables it.

        A false merge silently DROPS a real job, which is strictly worse than
        the duplicate it would fix, and CEDARS and LinkedIn share no id.
        """
        a = _listing("cedars", "G2600001", "HSBC", "Global Banking Programme")
        b = _listing("linkedin", "1000000003", "HSBC", "Global Banking Programme")
        assert a.identity_key != b.identity_key

    def test_a_different_location_is_a_different_posting(self):
        a = _listing("linkedin", "1", "HSBC", "Graduate Programme", location="Hong Kong")
        b = _listing("linkedin", "2", "HSBC", "Graduate Programme", location="Singapore")
        assert a.identity_key != b.identity_key

    def test_an_unusable_key_falls_back_to_the_per_source_id(self):
        """No employer means nothing to key on — fall back, never merge."""
        a = _listing("linkedin", "1", "", "Software Engineer Intern")
        b = _listing("linkedin", "2", "", "Software Engineer Intern")
        assert a.identity_key == a.dedup_key
        assert a.identity_key != b.identity_key


# --------------------------------------------------------------------------
# collapse_duplicates + mirror_collapsed
# --------------------------------------------------------------------------


class TestCollapseDuplicates:
    def test_a_repost_collapses_to_one_listing(self):
        old = _listing("linkedin", "1000000001", "IMC", "Software Engineer Intern 2027")
        new = _listing("linkedin", "1000000002", "IMC", "software engineer intern 2027")
        kept, collapsed = collapse_duplicates([old, new], seen_lookup=lambda s: set())
        assert len(kept) == 1
        assert len(collapsed) == 1

    def test_the_already_seen_row_wins_so_nothing_re_notifies(self):
        old = _listing("linkedin", "111", "IMC", "SWE Intern")
        new = _listing("linkedin", "222", "IMC", "SWE Intern")
        kept, _ = collapse_duplicates([new, old], seen_lookup=lambda s: {"111"})
        assert [l.external_id for l in kept] == ["111"]

    def test_cross_source_duplicates_are_left_alone(self):
        a = _listing("cedars", "G2600001", "HSBC", "CIB Programme")
        b = _listing("linkedin", "1000000003", "HSBC", "CIB Programme")
        kept, collapsed = collapse_duplicates([a, b], seen_lookup=lambda s: set())
        assert len(kept) == 2
        assert collapsed == []

    def test_the_dropped_id_is_mirrored_into_the_seen_set(self):
        """Without this the next hand-off between ids re-pushes the role."""
        old = _listing("linkedin", "111", "IMC", "SWE Intern")
        new = _listing("linkedin", "222", "IMC", "SWE Intern")
        _kept, collapsed = collapse_duplicates([old, new], seen_lookup=lambda s: {"111"})
        seen_by_source = {"linkedin": {"111"}}
        mirror_collapsed(seen_by_source, collapsed, seen_lookup=lambda s: set())
        assert seen_by_source["linkedin"] == {"111", "222"}

    def test_mirroring_never_invents_a_source_bucket_it_cannot_fill(self):
        """A collapse whose winner was not recorded must not fabricate state."""
        old = _listing("linkedin", "111", "IMC", "SWE Intern")
        new = _listing("linkedin", "222", "IMC", "SWE Intern")
        _kept, collapsed = collapse_duplicates([old, new], seen_lookup=lambda s: set())
        seen_by_source: dict[str, set[str]] = {}
        mirror_collapsed(seen_by_source, collapsed, seen_lookup=lambda s: set())
        assert seen_by_source == {}

    def test_the_order_of_untouched_listings_is_preserved(self):
        a = _listing("cedars", "A", "Alpha", "One")
        b = _listing("cedars", "B", "Beta", "Two")
        c = _listing("cedars", "C", "Gamma", "Three")
        kept, collapsed = collapse_duplicates([a, b, c], seen_lookup=lambda s: set())
        assert [l.external_id for l in kept] == ["A", "B", "C"]
        assert collapsed == []


# --------------------------------------------------------------------------
# collapse_register — the duplicates that arrived on DIFFERENT days
# --------------------------------------------------------------------------


def _role(key, *, employer="IMC", title="SWE Intern", source="linkedin",
          status="open", first_seen="2026-08-01", last_seen="2026-08-01",
          location="Hong Kong", deadline=None):
    return OpenRole(
        dedup_key=key,
        source=source,
        employer=employer,
        title=title,
        apply_url=f"https://example.test/{key}",
        deadline=deadline,
        first_seen=first_seen,
        last_seen=last_seen,
        reason="because",
        status=status,
        location=location,
    )


class TestCollapseRegister:
    def test_two_rows_for_one_posting_become_one(self):
        """The reported symptom: both IMC ids sit in the register as `open`."""
        a = _role("linkedin:1000000001", title="Software Engineer Intern 2027")
        b = _role("linkedin:1000000002", title="software engineer intern 2027",
                  last_seen="2026-08-20")
        out = collapse_register([a, b])
        assert len(out) == 1

    def test_the_most_recently_seen_row_survives(self):
        a = _role("linkedin:1", last_seen="2026-08-01")
        b = _role("linkedin:2", last_seen="2026-08-20")
        out = collapse_register([a, b])
        assert out[0].dedup_key == "linkedin:2"

    def test_history_is_carried_across_the_merge(self):
        a = _role("linkedin:1", first_seen="2026-07-01", last_seen="2026-08-01")
        b = _role("linkedin:2", first_seen="2026-08-01", last_seen="2026-08-20")
        out = collapse_register([a, b])
        assert out[0].first_seen == "2026-07-01"
        assert out[0].last_seen == "2026-08-20"

    @pytest.mark.parametrize("sticky", ["applied", "dismissed"])
    def test_a_hand_set_status_survives_the_merge(self, sticky):
        """Collapsing must never resurrect a decision the operator already took."""
        marked = _role("linkedin:1", status=sticky, last_seen="2026-08-01")
        fresh = _role("linkedin:2", status="open", last_seen="2026-08-30")
        out = collapse_register([marked, fresh])
        assert len(out) == 1
        assert out[0].status == sticky

    def test_cross_source_register_rows_are_never_merged(self):
        a = _role("cedars:G2600001", source="cedars", employer="HSBC", title="CIB")
        b = _role("linkedin:1000000003", employer="HSBC", title="CIB")
        assert len(collapse_register([a, b])) == 2

    def test_a_deadline_is_not_lost_when_the_undated_row_wins(self):
        dated = _role("linkedin:1", deadline="2026-10-01", last_seen="2026-08-01")
        undated = _role("linkedin:2", last_seen="2026-08-20")
        out = collapse_register([dated, undated])
        assert out[0].dedup_key == "linkedin:2"
        assert out[0].deadline == "2026-10-01"

    def test_it_does_not_mutate_the_input(self):
        a = _role("linkedin:1", last_seen="2026-08-01")
        b = _role("linkedin:2", last_seen="2026-08-20")
        collapse_register([a, b])
        assert a.last_seen == "2026-08-01"


# --------------------------------------------------------------------------
# Wiring: the collapse is worthless if it runs on the wrong side of the diff
# --------------------------------------------------------------------------


class TestOrchestratorWiring:
    """`run()` must collapse BEFORE `filter_new`, then mirror after it.

    Unit-testing the two functions in isolation cannot catch the ordering
    mistake this exists to prevent: a collapse that runs after the seen-set diff
    only ever sees today's new rows, so the common case — a repost arriving
    while the original is already seen — walks straight past it. These assert on
    what `filter_new` was actually handed during a real run.
    """

    def _run(self, monkeypatch, tmp_path, listings, *, seen=None):
        from job_sift import config, orchestrator
        from job_sift.schema import ClassifierResult

        seen = seen or {}
        handed_to_filter: list[list[JobListing]] = []
        saved: dict[str, set] = {}

        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setattr(config, "assert_required", lambda: None)
        monkeypatch.setenv("JOB_SIFT_STUB", "0")
        monkeypatch.setattr(
            orchestrator, "_fetch_all_sources", lambda: (list(listings), {}, ["linkedin"])
        )
        monkeypatch.setattr(orchestrator, "load_seen", lambda s: set(seen.get(s, set())))
        monkeypatch.setattr(orchestrator, "log_classification", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "_update_open_roles", lambda *a, **k: [])
        monkeypatch.setattr(orchestrator, "push_messages", lambda msgs: None)
        monkeypatch.setattr(orchestrator, "write_archive", lambda *a, **k: None)
        monkeypatch.setattr(
            orchestrator,
            "classify_batch",
            lambda ls: [ClassifierResult("skip", "out_of_scope", "nope") for _ in ls],
        )

        real_filter = orchestrator.filter_new

        def _spy(ls):
            handed_to_filter.append(list(ls))
            return real_filter(ls)

        monkeypatch.setattr(orchestrator, "filter_new", _spy)
        monkeypatch.setattr(
            orchestrator, "save_seen", lambda source, s: saved.__setitem__(source, s)
        )
        assert orchestrator.run() == 0
        return handed_to_filter[0], saved

    def test_the_duplicate_is_gone_before_the_seen_set_ever_sees_it(
        self, monkeypatch, tmp_path
    ):
        old = _listing("linkedin", "111", "IMC", "SWE Intern")
        new = _listing("linkedin", "222", "IMC", "SWE Intern")
        handed, _saved = self._run(monkeypatch, tmp_path, [old, new])
        assert [l.external_id for l in handed] == ["111"]

    def test_the_dropped_id_is_persisted_by_the_run(self, monkeypatch, tmp_path):
        """Otherwise the next run hands over to the other id and re-pushes."""
        old = _listing("linkedin", "111", "IMC", "SWE Intern")
        new = _listing("linkedin", "222", "IMC", "SWE Intern")
        _handed, saved = self._run(monkeypatch, tmp_path, [old, new])
        assert saved["linkedin"] == {"111", "222"}

    def test_two_sources_still_both_reach_the_classifier(self, monkeypatch, tmp_path):
        a = _listing("cedars", "G2600001", "HSBC", "CIB Programme")
        b = _listing("linkedin", "1000000003", "HSBC", "CIB Programme")
        handed, _saved = self._run(monkeypatch, tmp_path, [a, b])
        assert len(handed) == 2


class TestMergeOrdering:
    def test_a_row_with_no_last_seen_never_wins_the_merge(self):
        """`_invert_iso("")` used to sort FIRST, so the row we know least about
        decided which record survived."""
        blank = _role("linkedin:1", last_seen="")
        real = _role("linkedin:2", last_seen="2026-08-20")
        out = collapse_register([blank, real])
        assert out[0].dedup_key == "linkedin:2"

    def test_a_sticky_row_still_wins_even_with_no_last_seen(self):
        """Rule 1 outranks recency; a hand-set status is not a date question."""
        marked = _role("linkedin:1", status="applied", last_seen="")
        fresh = _role("linkedin:2", last_seen="2026-08-20")
        out = collapse_register([marked, fresh])
        assert out[0].status == "applied"


# ---------------------------------------------------------------------------
# Unwinding a sighting that was banked before anything looked at it.
#
# `filter_new` records an id the MOMENT it decides a listing is new — before the
# classifier has seen it — and the orchestrator commits that set after the push.
# So a listing the classifier could not judge was still marked delivered and
# never came back: an outage did not just produce one wrong digest, it ate the
# backlog. `withhold_unclassified` is the counterpart to holding the verdict as
# `None`.
# ---------------------------------------------------------------------------


class TestWithholdUnclassified:
    def test_an_unjudged_id_is_taken_back_out(self):
        a = _listing("cedars", "1", "A", "Engineer")
        b = _listing("cedars", "2", "B", "Engineer")
        seen = {"cedars": {"1", "2"}}
        assert withhold_unclassified(seen, [b]) == 1
        assert seen == {"cedars": {"1"}}

    def test_a_judged_id_is_left_alone(self):
        seen = {"cedars": {"1", "2"}}
        assert withhold_unclassified(seen, []) == 0
        assert seen == {"cedars": {"1", "2"}}

    def test_it_only_ever_shrinks_a_set_it_was_given(self):
        """`save_seen` truncates, so writing a bucket this function invented
        would delete a source's entire history. A source with no bucket is
        skipped rather than created."""
        a = _listing("linkedin", "9", "A", "Engineer")
        seen: dict = {}
        assert withhold_unclassified(seen, [a]) == 0
        assert seen == {}

    def test_a_mirrored_loser_is_unwound_with_its_winner(self):
        """The subtle half. `mirror_collapsed` runs BEFORE classification and
        banks each dropped duplicate's id against its winner's sighting. If the
        winner is then unclassified, pulling only the winner would leave the
        loser marked delivered for a posting that was never judged OR pushed.
        The same collapse recurs next run, so dropping both is idempotent."""
        winner = _listing("linkedin", "100", "IMC", "Software Engineer Intern")
        loser = _listing("linkedin", "200", "IMC", "Software Engineer Intern")
        seen = {"linkedin": {"100", "200"}}
        assert withhold_unclassified(seen, [winner], [(winner, loser)]) == 2
        assert seen == {"linkedin": set()}

    def test_a_mirrored_loser_survives_a_JUDGED_winner(self):
        winner = _listing("linkedin", "100", "IMC", "Software Engineer Intern")
        loser = _listing("linkedin", "200", "IMC", "Software Engineer Intern")
        other = _listing("linkedin", "300", "X", "Engineer")
        seen = {"linkedin": {"100", "200", "300"}}
        assert withhold_unclassified(seen, [other], [(winner, loser)]) == 1
        assert seen == {"linkedin": {"100", "200"}}

    def test_it_is_idempotent(self):
        a = _listing("cedars", "1", "A", "Engineer")
        seen = {"cedars": {"1"}}
        assert withhold_unclassified(seen, [a]) == 1
        assert withhold_unclassified(seen, [a]) == 0
        assert seen == {"cedars": set()}


# ---------------------------------------------------------------------------
# The suite's sandbox has to actually hold.
#
# `dedupe` and `open_roles` used to do `from job_sift.config import STATE_DIR`,
# binding the path at IMPORT time. A test that points `config.STATE_DIR` at a
# tmp_path therefore did not redirect them at all — it only looked like it did.
# `source_health` had already been written the other way and says why.
#
# Nothing fired in practice: every pre-existing test that patches
# `config.STATE_DIR` also stubs `save_seen` / `save_open_roles` at the
# orchestrator level, so the suite wrote zero state files. But the guard was
# load-bearing-by-luck, and the first test to drive a real `run()` through to
# the commit (the classifier-outage ones on this branch) reached straight past
# it into the repo's own `.data/state/`. On a developer machine that is the LIVE
# deployment's seen-sets and register: re-notified listings at best, a
# clobbered `applied` history at worst.
#
# These fail the moment either module re-binds the name.
# ---------------------------------------------------------------------------


class TestStateDirIsRedirectable:
    def test_dedupe_resolves_the_state_dir_at_call_time(self, monkeypatch, tmp_path):
        from job_sift import config, dedupe

        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        assert dedupe._seen_path("cedars") == tmp_path / "seen_cedars.json"
        assert dedupe._log_path() == tmp_path / "classifier_log.jsonl"

    def test_open_roles_resolves_the_state_dir_at_call_time(self, monkeypatch, tmp_path):
        from job_sift import config, open_roles

        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        assert open_roles._state_path() == tmp_path / "open_roles.json"

    def test_no_writer_escapes_the_patch(self, monkeypatch, tmp_path):
        """The property that matters, asserted on the filesystem rather than on
        a path string: with the patch in place, every state writer lands inside
        tmp_path and the real state directory gains nothing."""
        from job_sift import config, dedupe, open_roles, source_health

        real = config.STATE_DIR
        before = set(real.iterdir()) if real.exists() else set()
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)

        dedupe.save_seen("cedars", {"1", "2"})
        dedupe.log_classification(
            _listing("cedars", "1", "A", "Engineer"),
            ClassifierResult("skip", "out_of_scope", "nope"),
        )
        open_roles.save_open_roles([])
        source_health.save_health({})

        assert {p.name for p in tmp_path.iterdir()} == {
            "seen_cedars.json",
            "classifier_log.jsonl",
            "open_roles.json",
            "source_health.json",
        }
        assert (set(real.iterdir()) if real.exists() else set()) == before
