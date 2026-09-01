"""A feed that answers 200 with an HTML error page is a FAILED feed.

`test_silent_zero.py` covers the transport half: `fetch_ics` returns None, and
`fetch_feed_group` counts it toward `failed`. This file covers the half nothing
touched — `parse_ics`, which had no test at all.

The gap: `fetch_ics` decides a feed is healthy from the status code alone, so it
cannot see this failure BY CONSTRUCTION — the failure IS a 200. `parse_ics`
caught `Calendar.from_ical`'s refusal and returned `[]`, which never incremented
`failed`, so with all four Luma feeds serving a Cloudflare interstitial the
total-failure raise at the bottom of `fetch_feed_group` could not fire:

    parse_ics        ->  []          (not a calendar — but indistinguishable)
    fetch_feed_group ->  failed == 0, no raise, returns []
    orchestrator     ->  succeeded += ["luma"]
    source_health    ->  streak 9 -> 0, last_success = today, alarm None

That is the CEDARS outage with a different hostname. Both directions are pinned
below: an unparseable body escalates, and a real calendar holding no VEVENT
still scores a clean success.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from hk_events import source_health
from hk_events.errors import SourceFetchError
from hk_events.sources import _ical_common

_URLS = [f"https://lu.ma/cal-{i}.ics" for i in range(4)]


def _ics(uid: str = "evt-1") -> str:
    start = datetime.now(timezone.utc) + timedelta(days=3)
    stamp = start.strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:Test Event\r\nDTSTART:{stamp}\r\nDTEND:{stamp}\r\n"
        "URL:https://example.com/e/1\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


_EMPTY_CALENDAR = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nEND:VCALENDAR\r\n"

# Bodies a feed host really serves on a 200. None of them is a calendar; all of
# them used to come back as a clean, streak-resetting empty list.
_NOT_A_CALENDAR = {
    "cloudflare_interstitial": (
        "<!DOCTYPE html><html><head><title>Just a moment…</title></head>"
        "<body><div id='cf-wrapper'>Checking your browser before accessing lu.ma</div>"
        "</body></html>"
    ),
    "html_error_page": "<!DOCTYPE html><html><body><h1>404 — calendar not found</h1></body></html>",
    "login_wall": "<!DOCTYPE html><html><body><form action='/login'>Sign in</form></body></html>",
    "json_error": '{"error": "calendar_not_found", "status": 404}',
    "empty_body": "",
}


@pytest.fixture(autouse=True)
def _feeds(monkeypatch):
    """Four configured feeds, no config file read, no socket."""
    monkeypatch.setattr(_ical_common, "load_feed_urls", lambda group: list(_URLS))


def _serve(monkeypatch, bodies: dict[str, str | None]):
    monkeypatch.setattr(_ical_common, "fetch_ics", lambda url: bodies[url])


class TestParseIcsSeparatesUnreadableFromEmpty:
    @pytest.mark.parametrize("shape", sorted(_NOT_A_CALENDAR))
    def test_a_body_that_is_not_a_calendar_returns_none(self, shape):
        """None, not []. `[]` is a claim that we read a calendar."""
        assert _ical_common.parse_ics(_NOT_A_CALENDAR[shape], source="luma") is None

    def test_a_real_calendar_with_no_vevents_returns_empty(self):
        """The direction that must NOT regress into an alarm — a quiet calendar
        is a real, observed zero."""
        assert _ical_common.parse_ics(_EMPTY_CALENDAR, source="luma") == []

    def test_a_real_calendar_with_a_vevent_still_parses(self):
        """Premise: the two above could both pass on a parser that had stopped
        working entirely."""
        got = _ical_common.parse_ics(_ics("evt-abc"), source="luma")
        assert [e.external_id for e in got] == ["evt-abc"]


class TestFeedGroupCountsAnUnparseableFeedAsFailed:
    def test_every_feed_serving_an_interstitial_escalates(self, monkeypatch):
        """The reported outage. Four 200s, zero calendars, and before the fix
        `failed` stayed at 0 so the raise below could never fire."""
        _serve(monkeypatch, {u: _NOT_A_CALENDAR["cloudflare_interstitial"] for u in _URLS})
        with pytest.raises(SourceFetchError) as excinfo:
            _ical_common.fetch_feed_group("luma", source="luma")
        assert excinfo.value.source == "luma"
        assert "4 configured feed(s)" in str(excinfo.value)

    def test_a_mixed_transport_and_parse_wipeout_also_escalates(self, monkeypatch):
        """The two failure kinds must feed the SAME counter. Two feeds dead at
        the transport, two answering 200 with junk, is still a total failure —
        it would not be if each kind had its own count."""
        _serve(
            monkeypatch,
            {
                _URLS[0]: None,
                _URLS[1]: None,
                _URLS[2]: _NOT_A_CALENDAR["html_error_page"],
                _URLS[3]: _NOT_A_CALENDAR["json_error"],
            },
        )
        with pytest.raises(SourceFetchError):
            _ical_common.fetch_feed_group("luma", source="luma")

    def test_one_unparseable_feed_out_of_four_is_a_partial_degrade(self, monkeypatch):
        """PARTIAL degrade is preserved: three good feeds still report."""
        _serve(
            monkeypatch,
            {
                _URLS[0]: _NOT_A_CALENDAR["login_wall"],
                _URLS[1]: _ics("evt-1"),
                _URLS[2]: _ics("evt-2"),
                _URLS[3]: _ics("evt-3"),
            },
        )
        got = _ical_common.fetch_feed_group("luma", source="luma")
        assert sorted(e.external_id for e in got) == ["evt-1", "evt-2", "evt-3"]

    def test_four_genuinely_empty_calendars_are_a_success_not_an_alarm(self, monkeypatch):
        """The false-positive guard. If this raised, a quiet week in Hong Kong
        would page the operator, and an alarm that cries wolf is an alarm that
        gets ignored — which is how the fifty days happened."""
        _serve(monkeypatch, {u: _EMPTY_CALENDAR for u in _URLS})
        assert _ical_common.fetch_feed_group("luma", source="luma") == []


class TestTheStreakSurvivesAnInterstitial:
    """The consequence, scored the way a real run scores it."""

    _STREAK = {
        "luma": {
            "consecutive_failures": 9,
            "last_success": "2026-08-23",
            "last_failure": "2026-09-01",
            "last_error": "all 4 configured feed(s) failed",
            "first_seen": "2026-06-01",
        }
    }

    def _score(self, monkeypatch):
        _serve(monkeypatch, {u: _NOT_A_CALENDAR["cloudflare_interstitial"] for u in _URLS})
        try:
            _ical_common.fetch_feed_group("luma", source="luma")
        except SourceFetchError as exc:
            return source_health.update_health(
                dict(self._STREAK),
                succeeded=[],
                errors={"luma": f"fetch failed: {exc}"},
                today=date(2026, 9, 2),
            )
        pytest.fail("fetch_feed_group returned instead of raising")

    def test_the_streak_grows_and_last_success_does_not_advance(self, monkeypatch):
        health = self._score(monkeypatch)
        assert health["luma"]["consecutive_failures"] == 10
        assert health["luma"]["last_success"] == "2026-08-23"

    def test_the_alarm_fires(self, monkeypatch):
        alarm = source_health.render_alarm(self._score(monkeypatch))
        assert alarm and "luma" in alarm
