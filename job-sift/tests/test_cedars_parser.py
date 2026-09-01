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

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

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
    """A MINIMAL listings page: the results table and nothing else.

    Deliberately carries none of the portal's chrome, so it can only exercise
    the table half of the contract. The portal-anchor half is exercised against
    `_REAL_PORTAL_PAGE` below, which is real captured markup — an anchor tested
    only against HTML this file invented would be pinning my guess about the
    portal rather than the portal.
    """
    return f'<html><body><table class="tablesorter">{_HEADER}{"".join(rows)}</table></body></html>'


_FIXTURES = Path(__file__).parent / "fixtures"
_REAL_PORTAL_PAGE = (_FIXTURES / "cedars_listings_page.html").read_text(encoding="utf-8")


def _real_portal_page_without_its_table() -> str:
    """The captured portal page, minus the results table. Nothing else changes.

    This is the shape the anchor exists to recognise: unmistakably the CEDARS
    portal — its search form, its mega menu, its footer — carrying no results
    table. Derived from the real page rather than hand-written, so it cannot
    drift into a straw man that happens to satisfy whatever the anchor checks.
    """
    soup = BeautifulSoup(_REAL_PORTAL_PAGE, "lxml")
    soup.select_one("table.tablesorter").decompose()
    return str(soup)


def _real_portal_page_with_no_rows() -> str:
    """The captured portal page with the results table emptied of data rows.

    A quiet day on a healthy portal — the template still emits the table shell.
    """
    soup = BeautifulSoup(_REAL_PORTAL_PAGE, "lxml")
    for tr in soup.select_one("table.tablesorter").find_all("tr"):
        if tr.find("td"):
            tr.decompose()
    return str(soup)


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


class TestThePortalAnchor:
    """`_is_portal_page` decides "did we read the CEDARS portal?" — and NOTHING
    about whether the portal had anything to say.

    Sibling of `hk_events.sources.aitinkerers._is_chapter_page`, same job. HTTP
    200 plus well-formed HTML is not proof we read the page we meant to, and the
    old code inferred that from the PAGE NUMBER instead of from the page: page 1
    was assumed to be the real portal and page 2+ was assumed to be a legitimate
    walk off the end. Neither assumption looks at anything.

    The property under test is independence from content. If the anchor could be
    satisfied only by a page that has listings, then a genuinely quiet portal
    would raise — manufacturing a failure, the mirror image of the bug.
    """

    def test_the_real_portal_is_recognised(self):
        assert cedars._is_portal_page(_REAL_PORTAL_PAGE) is True

    def test_the_real_portal_is_still_recognised_with_zero_listings(self):
        """EVENT-INDEPENDENCE, direction 1. Same page, no data rows."""
        assert cedars._is_portal_page(_real_portal_page_with_no_rows()) is True

    def test_the_real_portal_is_still_recognised_with_no_results_table_at_all(self):
        """EVENT-INDEPENDENCE, direction 2. The anchor must not be reading the
        results table — that is the very thing it is being consulted about."""
        assert cedars._is_portal_page(_real_portal_page_without_its_table()) is True

    @pytest.mark.parametrize("shape", sorted(_NOT_A_LISTINGS_PAGE))
    def test_pages_that_are_not_the_portal_are_rejected(self, shape):
        assert cedars._is_portal_page(_NOT_A_LISTINGS_PAGE[shape]) is False

    def test_the_minimal_table_only_page_is_not_enough(self):
        """A bare results table proves nothing about which site served it. The
        anchor is chrome, not content — otherwise a scraped copy of the table on
        any host would pass."""
        assert cedars._is_portal_page(_page(_row("G2600001"))) is False

    def test_either_anchor_alone_carries_it(self):
        """Two independent signals, either sufficient — so a rename of one does
        not take the source down. Verified by deleting each in turn."""
        for gone, kept in (("#mega-menu-1", "search form"), ("form", "mega menu")):
            soup = BeautifulSoup(_REAL_PORTAL_PAGE, "lxml")
            for el in soup.select(gone):
                el.decompose()
            assert cedars._is_portal_page(str(soup)) is True, f"{kept} alone should carry it"


class TestTablelessPageEscalates:
    """`fetch_cedars_listings` must turn a tableless page OFF THE PORTAL into a
    raise — on any page number, not just page 1."""

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

    @pytest.mark.parametrize("shape", sorted(_NOT_A_LISTINGS_PAGE))
    def test_a_page_two_that_is_not_the_portal_raises_instead_of_degrading(
        self, monkeypatch, shape
    ):
        """THE CASE THE PAGE-NUMBER RULE GOT WRONG, and the one the old
        `test_a_tableless_page_two_keeps_page_one_and_stops` could not see.

        That test passed identically whether `_parse_listings_html` returned
        `None` or `[]` for a tableless page: both fell into `if not
        page_listings: break`, both kept page 1, both stopped. The None-vs-[]
        distinction it was written to protect was unobservable on that path, so
        the sixth costume of the bug could have been reintroduced underneath it
        without a single red test.

        It is observable now. A WAF interstitial on page 2 of 5 is not a walk
        off the end of the results — it is the portal refusing to talk to us,
        with pages 3, 4 and 5 unread. Reporting pages 1-2 as the day's listings
        and scoring the source a success is a silent partial: streak zeroed,
        `last_success` stamped, the missing pages invisible.
        """
        served = self._serve(
            monkeypatch,
            [_page(_row("G2600001")), _NOT_A_LISTINGS_PAGE[shape]],
        )
        with pytest.raises(SourceFetchError):
            cedars.fetch_cedars_listings(seen_ids=set())
        assert served == [1, 2], "it must have actually tried page 2"

    def test_a_portal_page_two_with_no_results_table_keeps_page_one_and_stops(
        self, monkeypatch
    ):
        """The other direction, and the reason the fix is an anchor rather than
        "always raise": a page that IS the portal and carries no results table
        is the end of the walk, not a failure. Real captured markup, table
        removed — see `_real_portal_page_without_its_table`.
        """
        served = self._serve(
            monkeypatch,
            [_page(_row("G2600001")), _real_portal_page_without_its_table()],
        )
        got = cedars.fetch_cedars_listings(seen_ids=set())
        assert [L.external_id for L in got] == ["G2600001"]
        assert served == [1, 2], "it must have actually tried page 2"

    def test_a_portal_page_one_with_no_results_table_is_a_quiet_zero(self, monkeypatch):
        """Page 1 stops being a special case in BOTH directions.

        The old rule read the page number: page 1 tableless meant "could not
        look" no matter what the page said. Now the page decides. This one says,
        in its own chrome, that it is the CEDARS portal, so zero is what the
        portal told us.
        """
        self._serve(monkeypatch, [_real_portal_page_without_its_table()])
        assert cedars.fetch_cedars_listings(seen_ids=set()) == []

    def test_a_real_portal_page_with_an_empty_table_is_a_quiet_zero(self, monkeypatch):
        """A quiet day, in real markup rather than the minimal one above."""
        self._serve(monkeypatch, [_real_portal_page_with_no_rows()])
        assert cedars.fetch_cedars_listings(seen_ids=set()) == []

    def test_the_real_portal_page_still_parses(self, monkeypatch):
        """Premise for all four above: the captured fixture is a page this
        parser genuinely reads, not just a bag of chrome the anchor likes."""
        self._serve(monkeypatch, [_REAL_PORTAL_PAGE])
        got = cedars.fetch_cedars_listings(seen_ids=set())
        assert got, "the captured portal page must yield listings"
        assert all(L.source == "cedars" for L in got)
        assert all(L.external_id for L in got)


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
