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

import httpx
import pytest
from bs4 import BeautifulSoup

from job_sift import orchestrator, source_health
from job_sift.errors import SourceAuthError, SourceFetchError
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


def _real_portal_serving_a_maintenance_notice() -> str:
    """The captured page with the results table swapped for a notice.

    THE SHAPE THAT MATTERS MOST HERE. A maintenance page served BY CEDARS keeps
    CEDARS' own header, menu and footer — so every portal anchor is satisfied
    and the only thing missing is the results table. An earlier cut of this
    module read that as "we reached the portal, so zero is the answer" and
    returned `[]` on page 1, which is the fifty-day incident's exact mechanism.
    """
    soup = BeautifulSoup(_REAL_PORTAL_PAGE, "lxml")
    table = soup.select_one("table.tablesorter")
    notice = soup.new_tag("p")
    notice.string = "NETjobs is undergoing scheduled maintenance. Please try again later."
    table.replace_with(notice)
    return str(soup)


def _real_portal_with_a_renamed_table_class() -> str:
    """The captured page with `tablesorter` renamed. Rows and chrome intact.

    The third cause the old error message named. The listings are RIGHT THERE
    and the selector no longer reaches them, so a quiet `[]` here would be a
    fabricated zero standing next to the data it failed to read.
    """
    soup = BeautifulSoup(_REAL_PORTAL_PAGE, "lxml")
    soup.select_one("table.tablesorter")["class"] = ["results-grid"]
    return str(soup)


def _real_portal_header_without_its_content() -> str:
    """The captured page with `#content` removed — header, menu and footer kept.

    What a truncated or half-rendered response looks like: all the chrome the
    anchors read, none of the page that carries an answer.
    """
    soup = BeautifulSoup(_REAL_PORTAL_PAGE, "lxml")
    soup.select_one("#content").decompose()
    return str(soup)


# The negatives, derived from the SAME captured page as the positives.
#
# The hand-written shapes in `_NOT_A_LISTINGS_PAGE` below are chrome-less by
# construction, which makes them easy rejections: any anchor at all rejects a
# page with no markup in it. These are the hard ones, and they are the realistic
# ones — a page that is unmistakably CEDARS and still carries no answer. Since
# rejection is the whole job, the negatives need to be at least as real as the
# positives.
_REAL_BUT_NOT_A_LISTINGS_PAGE = {
    "portal_serving_maintenance": _real_portal_serving_a_maintenance_notice,
    "portal_with_renamed_table_class": _real_portal_with_a_renamed_table_class,
    "portal_header_without_content": _real_portal_header_without_its_content,
    "portal_with_the_table_removed": _real_portal_page_without_its_table,
}


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

    @pytest.mark.parametrize("shape", sorted(_REAL_BUT_NOT_A_LISTINGS_PAGE))
    def test_a_broken_portal_page_is_still_recognised_as_the_portal(self, shape):
        """EVENT-INDEPENDENCE, direction 2 — and the reason this function does
        NOT get to decide whether to raise.

        All four of these ARE the CEDARS portal, and none of them is a listings
        page. `_is_portal_page` says True to every one, correctly: it answers
        "which site served this?", which is a genuinely different question from
        "did we read the listings?". Wiring the first answer to the raise
        decision is what reopened the silent zero — see
        `TestATablelessPageAlwaysEscalates`.
        """
        assert cedars._is_portal_page(_REAL_BUT_NOT_A_LISTINGS_PAGE[shape]()) is True

    @pytest.mark.parametrize("shape", sorted(_NOT_A_LISTINGS_PAGE))
    def test_pages_that_are_not_the_portal_are_rejected(self, shape):
        assert cedars._is_portal_page(_NOT_A_LISTINGS_PAGE[shape]) is False

    def test_the_minimal_table_only_page_is_not_enough(self):
        """A bare results table proves nothing about which site served it. The
        anchor is chrome, not content — otherwise a scraped copy of the table on
        any host would pass."""
        assert cedars._is_portal_page(_page(_row("G2600001"))) is False

    def test_either_spelling_alone_carries_it_but_they_are_not_independent(self):
        """Deleting one anchor leaves the other — and that is worth much less
        than it looks, which is why the docstring no longer claims otherwise.

        In the captured document `form#search_form` and `div#mega-menu-1` are
        adjacent siblings under `div#container`, both from one shared header
        include. Deleting them one at a time (the first two cases) says nothing
        about the failure that would actually happen: a header redesign, which
        takes both at once. The third case does that, and it is the honest
        measure of the redundancy — near zero.
        """
        def _without(*selectors: str) -> str:
            soup = BeautifulSoup(_REAL_PORTAL_PAGE, "lxml")
            for sel in selectors:
                for el in soup.select(sel):
                    el.decompose()
            return str(soup)

        assert cedars._is_portal_page(_without("#mega-menu-1")) is True
        assert cedars._is_portal_page(_without("form")) is True
        # Both anchors live in the same header include. One edit removes both.
        assert cedars._is_portal_page(_without("#mega-menu-1", "form")) is False


class TestATablelessPageAlwaysEscalates:
    """No `table.tablesorter` is a failure on EVERY page, portal chrome or not.

    Two rules were tried and both were wrong in the same direction. The first
    read the PAGE NUMBER: page 1 raised, page 2+ degraded quietly — so a WAF
    trip on page 2 of 5 reported a complete day with three pages unread. The
    second read the PORTAL CHROME: chrome present meant "end of results" — so a
    CEDARS-served maintenance page, which of course carries CEDARS chrome, went
    quiet on page 1. That second rule was strictly worse than the code it
    replaced: it reopened the fifty-day silent zero for two of the three causes
    the error message itself named.

    Neither rule was needed. The portal template emits the table shell even at
    zero rows, so end-of-results is a table with NO ROWS — which the existing
    `if not page_listings: break` already handles. "No table at all" has no
    legitimate producer, so it never has to be tolerated.
    """

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

    @pytest.mark.parametrize("shape", sorted(_REAL_BUT_NOT_A_LISTINGS_PAGE))
    @pytest.mark.parametrize("page", [1, 2], ids=["page_one", "page_two"])
    def test_a_real_portal_page_with_no_table_raises_on_any_page(
        self, monkeypatch, shape, page
    ):
        """THE REGRESSION THIS CLASS EXISTS FOR, in real markup.

        Each of these IS the CEDARS portal — real captured chrome, header, menu,
        footer — and none of them carries a results table. Under the chrome
        rule every one returned `[]` and scored a SUCCESS. They must raise, and
        the page number must not change the answer.
        """
        broken = _REAL_BUT_NOT_A_LISTINGS_PAGE[shape]()
        pages = [broken] if page == 1 else [_page(_row("G2600001")), broken]
        served = self._serve(monkeypatch, pages)

        with pytest.raises(SourceFetchError) as excinfo:
            cedars.fetch_cedars_listings(seen_ids=set())
        assert "tablesorter" in str(excinfo.value)
        assert "NOT 'no jobs today'" in str(excinfo.value)
        assert served == list(range(1, page + 1)), "it must have reached the broken page"

    @pytest.mark.parametrize("shape", sorted(_REAL_BUT_NOT_A_LISTINGS_PAGE))
    def test_the_error_names_the_portal_when_the_chrome_is_there(self, monkeypatch, shape):
        """`_is_portal_page`'s ONLY remaining job. The operator's next move
        differs — re-authenticate vs go look at the markup — so the message has
        to say which of the two it is. It still does not affect the raise."""
        self._serve(monkeypatch, [_REAL_BUT_NOT_A_LISTINGS_PAGE[shape]()])
        with pytest.raises(SourceFetchError) as excinfo:
            cedars.fetch_cedars_listings(seen_ids=set())
        assert "we reached the NETjobs portal" in str(excinfo.value)

    @pytest.mark.parametrize("shape", sorted(_NOT_A_LISTINGS_PAGE))
    def test_the_error_says_we_were_off_the_portal_when_the_chrome_is_gone(
        self, monkeypatch, shape
    ):
        self._serve(monkeypatch, [_NOT_A_LISTINGS_PAGE[shape]])
        with pytest.raises(SourceFetchError) as excinfo:
            cedars.fetch_cedars_listings(seen_ids=set())
        assert "not on the portal at all" in str(excinfo.value)

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


class TestTheRedirectGuardWinsOverTheParser:
    """`_fetch_listings_page`'s logged-out-bounce guard, which had no test.

    Every other test in this file monkeypatches `_fetch_listings_page` out
    wholesale, so the guard inside it — the one that turns a 302 to login.php /
    main.php into `SourceAuthError` — was never executed by the suite. It is
    load-bearing twice over: it is the ONE known shape where the portal serves
    its full chrome around something that is not a listings page, and it is the
    only error in this module that tells the operator to re-authenticate rather
    than go read markup.

    ORDER IS THE POINT. The guard runs before `return resp.text`, so it fires
    before the parser ever sees the body. That was correct by inspection and is
    now correct by test: the bodies below are real portal chrome with no results
    table, which `_is_portal_page` accepts and which would otherwise raise the
    generic `SourceFetchError`. `SourceAuthError` has to win, or an expired
    cookie gets diagnosed as a markup change and nobody logs back in.
    """

    @pytest.fixture(autouse=True)
    def _real_cedars(self, monkeypatch):
        monkeypatch.setenv("JOB_SIFT_STUB", "0")
        monkeypatch.setattr(cedars, "fetch_cedars_listings", _REAL_FETCH)
        # A placeholder so `_load_cookies` does not short-circuit on a missing
        # file. Never a real session value — see the repo's secrets rule.
        monkeypatch.setattr(cedars, "_load_cookies", lambda: {"PHPSESSID": "not-a-real-session"})

    def _land_on(self, monkeypatch, final_path: str, body: str):
        """Patch the transport, not `_fetch_listings_page` — the guard is IN it."""

        class _Resp:
            status_code = 200
            url = type("U", (), {"path": final_path})()
            text = body

        monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: _Resp())

    @pytest.mark.parametrize("bounce", ["login.php", "main.php"])
    def test_a_bounce_raises_an_auth_error_not_a_parse_error(self, monkeypatch, bounce):
        self._land_on(monkeypatch, f"/jobs/{bounce}", _real_portal_header_without_its_content())
        with pytest.raises(SourceAuthError) as excinfo:
            cedars.fetch_cedars_listings(seen_ids=set())
        assert excinfo.value.source == "cedars"
        assert bounce in str(excinfo.value)

    @pytest.mark.parametrize("bounce", ["login.php", "main.php"])
    def test_the_auth_error_beats_the_tableless_escalation(self, monkeypatch, bounce):
        """Same body, and it IS a tableless portal page — so both guards have
        something to say. The auth one must be the one that speaks."""
        self._land_on(monkeypatch, f"/jobs/{bounce}", _real_portal_serving_a_maintenance_notice())
        with pytest.raises(SourceAuthError):
            cedars.fetch_cedars_listings(seen_ids=set())

    def test_a_third_bounce_target_still_escalates_as_a_fetch_error(self, monkeypatch):
        """The whitelist is two filenames, and that is the point of not relying
        on it alone: a bounce to `notice.php` is not in it, falls through to the
        parser, and must still raise rather than report an empty day."""
        self._land_on(monkeypatch, "/jobs/notice.php", _real_portal_serving_a_maintenance_notice())
        with pytest.raises(SourceFetchError):
            cedars.fetch_cedars_listings(seen_ids=set())

    def test_a_normal_landing_is_not_treated_as_a_bounce(self, monkeypatch):
        """Premise: the guard must not fire on the ordinary results URL, or
        every healthy run would report an expired session."""
        self._land_on(monkeypatch, "/jobs/", _REAL_PORTAL_PAGE)
        assert cedars.fetch_cedars_listings(seen_ids=set())

    def test_the_error_carries_no_cookie_value(self, monkeypatch):
        """The message reaches Telegram and the on-disk state file."""
        self._land_on(monkeypatch, "/jobs/login.php", _REAL_PORTAL_PAGE)
        with pytest.raises(SourceAuthError) as excinfo:
            cedars.fetch_cedars_listings(seen_ids=set())
        assert "not-a-real-session" not in str(excinfo.value)
        assert "PHPSESSID" not in str(excinfo.value)


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
