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
from hk_events.errors import SourceFetchError, SourceNotConfiguredError
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

    def _total_outage(self, monkeypatch, real_sources):
        # This class is about what the REAL adapters do when the network dies, so
        # it opts out of conftest's blanket stub. All four live sources go through
        # httpx.Client.get — the two .ics feeds and the two structured-data pages
        # — so patching it covers every one of them.
        real_sources()
        monkeypatch.setattr(
            _ical_common, "load_feed_urls", lambda group: ["https://a/x.ics", "https://b/y.ics"]
        )
        monkeypatch.setattr(httpx.Client, "get", _outage)

    def test_every_source_lands_in_the_error_map(self, monkeypatch, real_sources):
        self._total_outage(monkeypatch, real_sources)
        events, errors, succeeded = orchestrator._fetch_all_sources()

        assert events == []
        assert succeeded == []
        assert set(errors) == set(orchestrator.enabled_sources())

    def test_the_streak_grows_and_last_success_does_not_advance(self, monkeypatch, real_sources):
        self._total_outage(monkeypatch, real_sources)
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


class TestUnconfiguredSourceIsNeitherSuccessNorFailure:
    """The third outcome — "nobody asked me anything" — and the second way the
    staleness alarm could still be silently wrong.

    Sibling of job-sift's class of the same name. The escalation above closes
    the case where the network died; this closes the case where the CONFIG did.
    `_load_sources_yaml` degrades to `{}` when sources.yaml is missing, and
    `load_feed_urls` also returns `[]` when every entry for the group is still
    marked TODO — the documented way to park an unverified feed, and the state
    luma's entries were in until they were verified on 2026-08-09. The old
    escalation guard read `if urls and failed == len(urls)`, so an empty `urls`
    skipped the raise and `fetch_feed_group` returned `[]`. That landed the
    source in the orchestrator's `succeeded` list and scored it a SUCCESS:
    streak reset to 0, today stamped as `last_success`.

    Both groups are fully configured as of this commit, so nothing changes for
    the live run today — this closes the path back to the bug, it does not fix
    a source that is currently broken.

    "No config" is neither success nor failure. It is the same non-event as the
    three adapters commented out of `_source_tasks`, and gets the same handling:
    absent from BOTH sets, and pruned by `update_health`.
    """

    _STREAK = {
        "meetup": {
            "consecutive_failures": 12,
            "last_success": "2026-08-20",
            "last_failure": "2026-08-31",
            "last_error": "fetch failed: all feeds down",
            "first_seen": "2026-06-01",
        }
    }

    @pytest.mark.parametrize("name,fetch", _SOURCES, ids=_IDS)
    def test_no_configured_feeds_raises_instead_of_returning_empty(
        self, monkeypatch, name, fetch
    ):
        monkeypatch.setattr(_ical_common, "load_feed_urls", lambda group: [])
        # Nothing should be fetched — a request here means we guessed at config.
        monkeypatch.setattr(httpx.Client, "get", _outage)

        with pytest.raises(SourceNotConfiguredError) as excinfo:
            fetch()
        assert excinfo.value.source == name

    @pytest.mark.parametrize("name,fetch", _SOURCES, ids=_IDS)
    def test_a_missing_sources_yaml_is_what_produces_that(
        self, monkeypatch, tmp_path, name, fetch
    ):
        """Through the REAL config loader, not a stubbed `load_feed_urls`."""
        monkeypatch.setattr(_ical_common, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(_ical_common, "_CFG_CACHE", None)
        assert _ical_common.load_feed_urls(name) == []

        with pytest.raises(SourceNotConfiguredError):
            fetch()

    @pytest.mark.parametrize("name,fetch", _SOURCES, ids=_IDS)
    def test_a_group_whose_entries_are_all_TODO_is_also_unconfigured(
        self, monkeypatch, tmp_path, name, fetch
    ):
        """A parked feed: the loader skips TODO entries, so the group is empty."""
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "sources.yaml").write_text(
            "ical_feeds:\n"
            f"  {name}:\n"
            "    - {name: unverified, url: TODO-confirm-the-ics-url}\n"
        )
        monkeypatch.setattr(_ical_common, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(_ical_common, "_CFG_CACHE", None)

        with pytest.raises(SourceNotConfiguredError):
            fetch()

    def test_the_orchestrator_puts_it_in_neither_set(self, monkeypatch):
        """The wiring: not in `succeeded`, and NOT invented as an error either."""
        monkeypatch.setattr(
            orchestrator.meetup,
            "fetch_meetup_events",
            lambda: (_ for _ in ()).throw(
                SourceNotConfiguredError("meetup", "no feeds configured")
            ),
        )
        _events, errors, succeeded = orchestrator._fetch_all_sources()

        assert "meetup" not in succeeded
        assert "meetup" not in errors
        # The run is otherwise untouched: one dead config is not a dead run.
        # (conftest stubs every other adapter to a clean, empty fetch.)
        assert errors == {}
        assert set(succeeded) == set(orchestrator.enabled_sources()) - {"meetup"}

    def test_the_record_is_pruned_not_reset(self):
        """The consequence in the state file: no fabricated success.

        Pruned means DROPPED, per `update_health`'s existing contract for a
        source that reported nothing — deliberately not "kept at 12", and
        emphatically not "reset to 0 with today as last_success".
        """
        prior = {k: dict(v) for k, v in self._STREAK.items()}
        out = source_health.update_health(prior, succeeded=[], errors={}, today=_DAY)

        assert "meetup" not in out
        assert out == {}
        # The reset shape is what the bug wrote. Neither half may appear.
        assert _DAY.isoformat() not in repr(out)
        # update_health is pure — the caller's prior state is not mutated.
        assert prior == self._STREAK

    def test_an_unconfigured_source_cannot_silence_a_real_alarm(self):
        """A source that IS failing still alarms; only the unasked one drops."""
        health = source_health.update_health(
            {**self._STREAK, "luma": {"consecutive_failures": 2, "first_seen": "2026-06-01"}},
            succeeded=[],
            errors={"luma": "fetch failed: connection reset"},
            today=_DAY,
        )
        assert "meetup" not in health
        assert health["luma"]["consecutive_failures"] == 3
        assert source_health.render_alarm(health) is not None
