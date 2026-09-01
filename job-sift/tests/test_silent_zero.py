"""The silent-zero gap: "I could not look" must never render as "nothing today".

This is the failure the whole branch exists for, in its structural form. CEDARS
was fixed source-by-source; this file pins the SHAPE.

`httpx` wraps `socket.gaierror` in `ConnectError`, which is an `HTTPError`.
Every ATS adapter catches `HTTPError` per slug and degrades. So a total network
outage used to produce a clean empty result and an EMPTY error map — and
`source_health` then read "attempted and absent from `errors`" as proof of a
successful fetch: it zeroed the accumulated failure streak and wrote today as
`last_success`. A fabricated fact, persisted to disk, later rendered to a human.

Both halves are pinned here:
  * adapters ESCALATE a total failure (and still degrade on a partial one);
  * `update_health` takes success as a POSITIVE signal and can no longer
    manufacture one from absence.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from job_sift import config, orchestrator, source_health
from job_sift.errors import SourceAuthError, SourceFetchError
from job_sift.sources import ashby, greenhouse, lever, linkedin

_DAY = date(2026, 9, 1)


def test_the_premise_httpx_wraps_dns_failure_as_an_httperror():
    """The reason every adapter swallowed a DNS outage. Pin it — if httpx ever
    changes this, the escalation logic below is guarding a different bug."""
    assert issubclass(httpx.ConnectError, httpx.HTTPError)


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _outage(*_a, **_kw):
    raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")


# (name, module, entry point, one-job payload in that vendor's response shape)
_ADAPTERS = [
    ("greenhouse", greenhouse, greenhouse.fetch_greenhouse_listings, {"jobs": [{"id": 1, "title": "SWE"}]}),
    ("lever", lever, lever.fetch_lever_listings, [{"id": "a1", "text": "SWE"}]),
    ("ashby", ashby, ashby.fetch_ashby_listings, {"jobs": [{"id": "a1", "title": "SWE"}]}),
]
_IDS = [a[0] for a in _ADAPTERS]


class TestAdapterEscalatesTotalFailure:
    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    def test_every_slug_failing_raises_instead_of_returning_empty(
        self, monkeypatch, name, module, fetch, payload
    ):
        monkeypatch.setattr(module, "load_slugs", lambda vendor: ["a", "b", "c"])
        monkeypatch.setattr(httpx.Client, "get", _outage)

        with pytest.raises(SourceFetchError) as excinfo:
            fetch()
        assert excinfo.value.source == name

    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    def test_partial_failure_still_returns_what_landed(
        self, monkeypatch, name, module, fetch, payload
    ):
        """3 of 4 boards down is a partial success, not a dead source."""
        monkeypatch.setattr(module, "load_slugs", lambda vendor: ["ok", "b", "c", "d"])

        def get(self, url, **kwargs):
            if "/ok" in url:
                return _Resp(payload)
            _outage()

        monkeypatch.setattr(httpx.Client, "get", get)

        got = fetch()
        assert len(got) == 1
        assert got[0].source == name

    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    def test_an_empty_but_reachable_board_is_still_a_success(
        self, monkeypatch, name, module, fetch, payload
    ):
        """Returning zero has to stay possible — the point is that it now means
        "I looked", not "I could not"."""
        empty = [] if isinstance(payload, list) else {"jobs": []}
        monkeypatch.setattr(module, "load_slugs", lambda vendor: ["a", "b"])
        monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: _Resp(empty))

        assert fetch() == []

    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    def test_every_board_404ing_is_also_a_total_failure(
        self, monkeypatch, name, module, fetch, payload
    ):
        """A wholesale 404 means the config is dead, not that nobody is hiring."""

        class _Gone(_Resp):
            status_code = 404

        monkeypatch.setattr(module, "load_slugs", lambda vendor: ["a", "b"])
        monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: _Gone(None))

        with pytest.raises(SourceFetchError):
            fetch()


class TestLinkedInEscalatesTotalFailure:
    """linkedin.py:96 was the case the earlier review parked. Same shape."""

    def _gws(self, monkeypatch, *, returncode=0, stdout="", stderr="", exc=None):
        def run(cmd, **kwargs):
            if exc is not None:
                raise exc
            return type("P", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()

        monkeypatch.setattr(linkedin.subprocess, "run", run)

    def test_gws_missing_raises_rather_than_reporting_an_empty_inbox(self, monkeypatch):
        self._gws(monkeypatch, exc=FileNotFoundError("gws"))
        with pytest.raises(SourceFetchError):
            linkedin.fetch_linkedin_listings()

    def test_nonzero_exit_raises_and_does_not_echo_stderr(self, monkeypatch):
        """The raised message reaches Telegram and the on-disk state file, so it
        must not carry gws stderr — that is not guaranteed token-free."""
        self._gws(monkeypatch, returncode=2, stderr="Bearer ya29.SUPERSECRET quota exceeded")
        with pytest.raises(SourceFetchError) as excinfo:
            linkedin.fetch_linkedin_listings()
        assert "SUPERSECRET" not in str(excinfo.value)
        assert "ya29" not in str(excinfo.value)

    def test_an_expired_token_still_raises_the_auth_error(self, monkeypatch):
        self._gws(monkeypatch, returncode=1, stderr="invalid_grant: token expired")
        with pytest.raises(SourceAuthError):
            linkedin.fetch_linkedin_listings()

    def test_a_genuinely_empty_mailbox_is_still_an_empty_list(self, monkeypatch):
        self._gws(monkeypatch, returncode=0, stdout='{"messages": []}')
        assert linkedin.fetch_linkedin_listings() == []


class TestSuccessIsNeverInferredFromAbsence:
    _STREAK = {
        "cedars": {
            "consecutive_failures": 12,
            "last_success": "2026-08-20",
            "last_failure": "2026-08-31",
            "last_error": "session expired",
            "first_seen": "2026-06-01",
        }
    }

    def test_a_source_that_reported_nothing_cannot_be_scored_a_success(self):
        """The exact defect: an empty error map used to mean "everyone passed"."""
        out = source_health.update_health(
            dict(self._STREAK), succeeded=[], errors={}, today=_DAY
        )
        assert "cedars" not in out
        assert _DAY.isoformat() not in repr(out)

    def test_errors_win_over_a_contradictory_success_claim(self):
        out = source_health.update_health(
            dict(self._STREAK), succeeded=["cedars"], errors={"cedars": "boom"}, today=_DAY
        )
        assert out["cedars"]["consecutive_failures"] == 13
        assert out["cedars"]["last_success"] == "2026-08-20"


class TestStreakSurvivesATotalOutage:
    """End-to-end reproduction of the reported bug, through the real adapters.

    Before the fix this run produced an EMPTY error map, reset the 12-run streak
    to 0, and wrote 2026-09-01 as `last_success`.
    """

    @pytest.fixture(autouse=True)
    def _isolated_state(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setenv("JOB_SIFT_STUB", "0")
        return tmp_path

    def _total_outage(self, monkeypatch):
        # The three ATS adapters go through their REAL code path with the
        # network pulled out from under them — that is the swallow being tested.
        monkeypatch.setattr(httpx.Client, "get", _outage)
        for module in (greenhouse, lever, ashby):
            monkeypatch.setattr(module, "load_slugs", lambda vendor: ["a", "b"])
        # The other two do not speak httpx; stub them at the same failure.
        monkeypatch.setattr(
            orchestrator.cedars,
            "fetch_cedars_listings",
            lambda **kw: _outage(),
        )
        monkeypatch.setattr(
            orchestrator.linkedin,
            "fetch_linkedin_listings",
            lambda: (_ for _ in ()).throw(SourceFetchError("linkedin", "gws unavailable")),
        )
        monkeypatch.setattr(orchestrator, "load_seen", lambda source: set())

    def test_every_source_lands_in_the_error_map(self, monkeypatch):
        self._total_outage(monkeypatch)
        listings, errors, succeeded = orchestrator._fetch_all_sources()

        assert listings == []
        assert succeeded == []
        assert set(errors) == set(orchestrator.enabled_sources())

    def test_the_streak_grows_and_last_success_does_not_advance(self, monkeypatch):
        self._total_outage(monkeypatch)
        _listings, errors, succeeded = orchestrator._fetch_all_sources()

        health = source_health.update_health(
            {
                "cedars": {
                    "consecutive_failures": 12,
                    "last_success": "2026-08-20",
                    "first_seen": "2026-06-01",
                }
            },
            succeeded=succeeded,
            errors=errors,
            today=_DAY,
        )

        assert health["cedars"]["consecutive_failures"] == 13
        assert health["cedars"]["last_success"] == "2026-08-20"
        assert source_health.render_alarm(health) is not None
