"""The CEDARS parser must not answer "nothing today" when it means "not a page".

Nothing in this suite touched `_parse_listings_html` or the word `tablesorter`
before this file existed, and that gap is exactly where the sixth variant of the
silent-zero bug lived. The branch closed the bug at every ADAPTER boundary; this
one is a level down, INSIDE cedars:

    _parse_listings_html  ->  []            (no results table on the page)
    fetch_cedars_listings ->  []            ("page 1 returned 0 listings — stopping")
    orchestrator          ->  succeeded += ["cedars"]
    source_health         ->  streak 12 -> 0, last_success = today

Every hop was individually reasonable and the composition fabricated a fact. The
redirect guard in `_fetch_listings_page` only catches a bounce whose final path
segment is `login.php` or `main.php` — a two-filename whitelist that a
maintenance page, a WAF interstitial, a third bounce target, or a renamed table
class all walk straight past.

So the parser now distinguishes the two, and these tests pin BOTH directions:
a tableless page escalates, and a genuinely empty results table does not.
"""

from __future__ import annotations

import pytest

from job_sift import orchestrator, source_health
from job_sift.errors import SourceFetchError
from job_sift.sources import cedars

# The genuine adapter, captured at import — `conftest.stub_all_sources` replaces
# the module attribute for every test, and the tests below are ABOUT this
# function. Restoring only cedars (rather than taking the `real_sources`
# wholesale opt-out) keeps the other four neutralised, which matters: they would
# otherwise sit on real DNS timeouts inside `_fetch_all_sources`.
_REAL_FETCH = cedars.fetch_cedars_listings

_HEADER = "<tr><th>Job ID</th><th>Employer</th><th>Title</th><th>Deadline</th><th>Posted</th></tr>"


def _row(job_id: str) -> str:
    return (
        f'<tr><td><a href="job_detail.php?job_id={job_id}">{job_id}</a></td>'
        "<td>Acme Ltd</td><td>Software Engineer</td>"
        "<td>2026-10-01</td><td>2026-09-01</td></tr>"
    )


def _page(*rows: str) -> str:
    return f'<html><body><table class="tablesorter">{_HEADER}{"".join(rows)}</table></body></html>'


# Shapes a real portal can serve on a 200 that are NOT a listings page. The
# point of the table is that none of them are exotic: the first is what an
# expired session gets when the bounce target is not in the two-name whitelist.
_NOT_A_LISTINGS_PAGE = {
    "third_bounce_target": "<html><body><h1>Please sign in to continue</h1></body></html>",
    "maintenance_notice": "<html><body><p>NETJobs is undergoing scheduled maintenance.</p></body></html>",
    "waf_interstitial": "<html><body><div id='cf-wrapper'>Checking your browser…</div></body></html>",
    "renamed_table_class": (
        '<html><body><table class="results-grid">' + _HEADER + _row("G2600001") + "</table></body></html>"
    ),
}


@pytest.mark.parametrize("shape", sorted(_NOT_A_LISTINGS_PAGE))
def test_a_page_with_no_results_table_is_not_an_empty_result(shape):
    """None, not []. `None` is "I could not look"; `[]` is a fact about jobs."""
    assert cedars._parse_listings_html(_NOT_A_LISTINGS_PAGE[shape]) is None


def test_a_real_table_with_no_rows_is_a_genuine_empty_result():
    """The other direction, and the one that must NOT regress into an alarm.

    A quiet week is a real thing. The portal renders the table with its header
    row and no data rows; we read it, and the honest answer is zero.
    """
    assert cedars._parse_listings_html(_page()) == []


def test_a_populated_table_still_parses():
    """Premise. Without this the two tests above could both pass on a parser
    that had simply stopped working."""
    got = cedars._parse_listings_html(_page(_row("G2600001"), _row("G2600002")))
    assert [L.external_id for L in got] == ["G2600001", "G2600002"]
    assert got[0].source == "cedars"


class TestTablelessPageOneEscalates:
    """`fetch_cedars_listings` must turn a tableless page 1 into a raise."""

    @pytest.fixture(autouse=True)
    def _real_cedars(self, monkeypatch):
        monkeypatch.setenv("JOB_SIFT_STUB", "0")
        monkeypatch.setattr(cedars, "fetch_cedars_listings", _REAL_FETCH)

    def _serve(self, monkeypatch, pages: list[str]):
        served: list[int] = []

        def _fetch(page: int = 1) -> str:
            served.append(page)
            return pages[page - 1] if page <= len(pages) else pages[-1]

        monkeypatch.setattr(cedars, "_fetch_listings_page", _fetch)
        return served

    def test_page_one_without_a_table_raises(self, monkeypatch):
        self._serve(monkeypatch, [_NOT_A_LISTINGS_PAGE["maintenance_notice"]])
        with pytest.raises(SourceFetchError) as excinfo:
            cedars.fetch_cedars_listings(seen_ids=set())
        # The message has to send the operator to the portal, not to the logs.
        assert "tablesorter" in str(excinfo.value)
        assert "NOT 'no jobs today'" in str(excinfo.value)

    def test_page_one_with_an_empty_table_returns_empty_without_raising(self, monkeypatch):
        self._serve(monkeypatch, [_page()])
        assert cedars.fetch_cedars_listings(seen_ids=set()) == []

    def test_a_tableless_page_two_keeps_page_one_and_stops(self, monkeypatch):
        """PARTIAL degrade. By page 2 we hold positive evidence the source is
        alive and the run reports real listings, so this is not a silent zero —
        and paginating past the last page is a legitimate way to reach a page
        the parser does not recognise."""
        served = self._serve(
            monkeypatch,
            [_page(_row("G2600001")), _NOT_A_LISTINGS_PAGE["maintenance_notice"]],
        )
        got = cedars.fetch_cedars_listings(seen_ids=set())
        assert [L.external_id for L in got] == ["G2600001"]
        assert served == [1, 2], "it must have actually tried page 2"


class TestTheStreakSurvivesATablelessPage:
    """The end-to-end consequence, through the real orchestrator + health scorer.

    This is the assertion that would have caught the fifty-day outage. Before
    the fix: `succeeded == ["cedars"]`, streak 12 -> 0, `last_success` stamped
    with today, alarm None — every one of those a fabrication.
    """

    _STREAK = {
        "cedars": {
            "consecutive_failures": 12,
            "last_success": "2026-08-20",
            "last_failure": "2026-08-31",
            "last_error": "session expired",
            "first_seen": "2026-06-01",
        }
    }

    @pytest.fixture
    def _fetched(self, monkeypatch):
        # Only cedars is un-stubbed: the other four stay in the conftest's
        # "not opted in" state, so they land in NEITHER list and cannot muddy
        # the assertions (or spend the fetch budget on real DNS).
        monkeypatch.setattr(cedars, "fetch_cedars_listings", _REAL_FETCH)
        monkeypatch.setenv("JOB_SIFT_STUB", "0")
        monkeypatch.setattr(
            cedars,
            "_fetch_listings_page",
            lambda page=1: _NOT_A_LISTINGS_PAGE["maintenance_notice"],
        )
        monkeypatch.setattr(orchestrator, "load_seen", lambda source: set())
        return orchestrator._fetch_all_sources()

    def test_cedars_lands_in_the_error_map_not_the_succeeded_list(self, _fetched):
        _listings, errors, succeeded = _fetched
        assert "cedars" not in succeeded
        assert "cedars" in errors

    def test_the_streak_grows_and_the_alarm_fires(self, _fetched):
        from datetime import date

        _listings, errors, succeeded = _fetched
        today = date(2026, 9, 2)
        health = source_health.update_health(
            dict(self._STREAK), succeeded=succeeded, errors=errors, today=today
        )
        assert health["cedars"]["consecutive_failures"] == 13
        assert health["cedars"]["last_success"] == "2026-08-20"
        alarm = source_health.render_alarm(health)
        assert alarm and "cedars" in alarm


def test_the_conftest_fetcher_list_still_covers_every_source():
    """Premise for `conftest.stub_all_sources`.

    If `_FETCHERS` drifts from `_source_tasks`, the uncovered source silently
    starts running for real in every test in this suite — the exact hazard the
    fixture exists to remove. Lives here because pytest does not COLLECT tests
    out of a conftest.
    """
    import conftest  # the suite's own; pytest puts rootdir/tests on sys.path

    assert {attr for _module, attr in conftest._FETCHERS} == {
        f"fetch_{name}_listings" for name in orchestrator.enabled_sources()
    }
