"""The silent-zero gap: "I could not look" must never render as "nothing today".

Sibling of job-sift/tests/test_silent_zero.py. `_ical_common.fetch_ics` catches
`httpx.HTTPError`, and `httpx` wraps `socket.gaierror` in `ConnectError`, which
IS an `HTTPError`. `fetch_feed_group` then skipped past every None and returned
`[]`, raising nothing — so a total DNS outage produced an EMPTY error map, and
`source_health` read "attempted and absent from `errors`" as proof of success:
it zeroed the accumulated failure streak and stamped today as `last_success`.

This file covers BOTH live sources at once — meetup and luma are both thin
wrappers over `fetch_feed_group`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from hk_events import config, orchestrator, source_health
from hk_events.errors import SourceFetchError
from hk_events.sources import _ical_common, luma, meetup

_DAY = date(2026, 9, 1)


def test_the_premise_httpx_wraps_dns_failure_as_an_httperror():
    """The reason `fetch_ics` swallowed a DNS outage. Pin it — if httpx ever
    changes this, the escalation below is guarding a different bug."""
    assert issubclass(httpx.ConnectError, httpx.HTTPError)


def _ics(uid: str = "evt-1") -> str:
    """A minimal, in-horizon VCALENDAR so a 'good' feed really parses."""
    start = datetime.now(timezone.utc) + timedelta(days=3)
    stamp = start.strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:Test Meetup\r\nDTSTART:{stamp}\r\nDTEND:{stamp}\r\n"
        "URL:https://example.com/e/1\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class _Resp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _outage(*_a, **_kw):
    raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")


_SOURCES = [("meetup", meetup.fetch_meetup_events), ("luma", luma.fetch_luma_events)]
_IDS = [s[0] for s in _SOURCES]


class TestFeedGroupEscalatesTotalFailure:
    @pytest.mark.parametrize("name,fetch", _SOURCES, ids=_IDS)
    def test_every_feed_failing_raises_instead_of_returning_empty(
        self, monkeypatch, name, fetch
    ):
        monkeypatch.setattr(
            _ical_common, "load_feed_urls", lambda group: ["https://a/x.ics", "https://b/y.ics"]
        )
        monkeypatch.setattr(httpx.Client, "get", _outage)

        with pytest.raises(SourceFetchError) as excinfo:
            fetch()
        assert excinfo.value.source == name

    @pytest.mark.parametrize("name,fetch", _SOURCES, ids=_IDS)
    def test_partial_failure_still_returns_what_landed(self, monkeypatch, name, fetch):
        """3 of 4 feeds down is a partial success, not a dead source."""
        monkeypatch.setattr(
            _ical_common,
            "load_feed_urls",
            lambda group: [f"https://host/{s}.ics" for s in ("ok", "b", "c", "d")],
        )

        def get(self, url, **kwargs):
            if "/ok." in url:
                return _Resp(_ics())
            _outage()

        monkeypatch.setattr(httpx.Client, "get", get)

        got = fetch()
        assert len(got) == 1
        assert got[0].source == name

    @pytest.mark.parametrize("name,fetch", _SOURCES, ids=_IDS)
    def test_a_reachable_feed_with_no_events_is_still_a_success(
        self, monkeypatch, name, fetch
    ):
        """Returning zero has to stay possible — the point is that it now means
        "I looked", not "I could not"."""
        monkeypatch.setattr(_ical_common, "load_feed_urls", lambda group: ["https://a/x.ics"])
        monkeypatch.setattr(
            httpx.Client,
            "get",
            lambda self, url, **kw: _Resp("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"),
        )

        assert fetch() == []

    @pytest.mark.parametrize("name,fetch", _SOURCES, ids=_IDS)
    def test_every_feed_403ing_is_also_a_total_failure(self, monkeypatch, name, fetch):
        """A wholesale bot-block means the source is dead, not that HK is quiet."""
        monkeypatch.setattr(
            _ical_common, "load_feed_urls", lambda group: ["https://a/x.ics", "https://b/y.ics"]
        )
        monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: _Resp("", 403))

        with pytest.raises(SourceFetchError):
            fetch()


class TestSuccessIsNeverInferredFromAbsence:
    _STREAK = {
        "meetup": {
            "consecutive_failures": 12,
            "last_success": "2026-08-20",
            "last_failure": "2026-08-31",
            "last_error": "fetch failed: connection reset",
            "first_seen": "2026-06-01",
        }
    }

    def test_a_source_that_reported_nothing_cannot_be_scored_a_success(self):
        """The exact defect: an empty error map used to mean "everyone passed"."""
        out = source_health.update_health(
            dict(self._STREAK), succeeded=[], errors={}, today=_DAY
        )
        assert "meetup" not in out
        assert _DAY.isoformat() not in repr(out)

    def test_errors_win_over_a_contradictory_success_claim(self):
        out = source_health.update_health(
            dict(self._STREAK), succeeded=["meetup"], errors={"meetup": "boom"}, today=_DAY
        )
        assert out["meetup"]["consecutive_failures"] == 13
        assert out["meetup"]["last_success"] == "2026-08-20"


class TestStreakSurvivesATotalOutage:
    """End-to-end reproduction of the reported bug, through the real adapters.

    Before the fix this run produced an EMPTY error map, reset the 12-run streak
    to 0, and wrote 2026-09-01 as `last_success`.
    """

    @pytest.fixture(autouse=True)
    def _isolated_state(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setenv("HK_EVENTS_STUB", "0")
        return tmp_path

    def _total_outage(self, monkeypatch):
        monkeypatch.setattr(
            _ical_common, "load_feed_urls", lambda group: ["https://a/x.ics", "https://b/y.ics"]
        )
        monkeypatch.setattr(httpx.Client, "get", _outage)

    def test_every_source_lands_in_the_error_map(self, monkeypatch):
        self._total_outage(monkeypatch)
        events, errors, succeeded = orchestrator._fetch_all_sources()

        assert events == []
        assert succeeded == []
        assert set(errors) == set(orchestrator.enabled_sources())

    def test_the_streak_grows_and_last_success_does_not_advance(self, monkeypatch):
        self._total_outage(monkeypatch)
        _events, errors, succeeded = orchestrator._fetch_all_sources()

        health = source_health.update_health(
            {
                "meetup": {
                    "consecutive_failures": 12,
                    "last_success": "2026-08-20",
                    "first_seen": "2026-06-01",
                }
            },
            succeeded=succeeded,
            errors=errors,
            today=_DAY,
        )

        assert health["meetup"]["consecutive_failures"] == 13
        assert health["meetup"]["last_success"] == "2026-08-20"
        assert source_health.render_alarm(health) is not None
