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
from job_sift.errors import SourceAuthError, SourceFetchError, SourceNotConfiguredError
from job_sift.sources import _ats_common, ashby, greenhouse, lever, linkedin

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

    @pytest.fixture(autouse=True)
    def _use_the_real_adapter(self, real_sources):
        """These tests call the adapter directly, so they must undo
        `conftest.stub_all_sources`. `no_network` still applies — the transport
        here is `subprocess`, patched per-test."""
        real_sources()

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

    def _total_outage(self, monkeypatch, real_sources):
        # This reproduction is ABOUT the genuine adapters swallowing the
        # outage, so it opts back out of `conftest.stub_all_sources`.
        real_sources()
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

    def test_every_source_lands_in_the_error_map(self, monkeypatch, real_sources):
        self._total_outage(monkeypatch, real_sources)
        listings, errors, succeeded = orchestrator._fetch_all_sources()

        assert listings == []
        assert succeeded == []
        assert set(errors) == set(orchestrator.enabled_sources())

    def test_the_streak_grows_and_last_success_does_not_advance(self, monkeypatch, real_sources):
        self._total_outage(monkeypatch, real_sources)
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


class TestUnconfiguredSourceIsNeitherSuccessNorFailure:
    """The third outcome — "nobody asked me anything" — and the second way the
    staleness alarm could still be silently wrong.

    The escalation above closes the case where the network died. It does not
    close the case where the CONFIG died. `_ats_common._load_companies_yaml`
    degrades to `{}` when companies.yaml is missing, `load_slugs` therefore
    returns `[]`, and the adapters used to answer that with a bare `return []`.
    An empty list raises nothing, so the source landed in the orchestrator's
    `succeeded` list and was scored a SUCCESS: streak reset to 0, today stamped
    as `last_success`. Reproduced live — with companies.yaml removed, a seeded
    12-run streak went to 0.

    "No config" is neither success nor failure. It is the same non-event as a
    source commented out of the fetch list, and gets the same handling: absent
    from BOTH sets, and pruned by `update_health`.
    """

    _STREAK = {
        "greenhouse": {
            "consecutive_failures": 12,
            "last_success": "2026-08-20",
            "last_failure": "2026-08-31",
            "last_error": "fetch failed: all boards down",
            "first_seen": "2026-06-01",
        }
    }

    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    def test_no_configured_slugs_raises_instead_of_returning_empty(
        self, monkeypatch, name, module, fetch, payload
    ):
        monkeypatch.setattr(module, "load_slugs", lambda vendor: [])
        # Nothing should be polled — a request here means we guessed at config.
        monkeypatch.setattr(httpx.Client, "get", _outage)

        with pytest.raises(SourceNotConfiguredError) as excinfo:
            fetch()
        assert excinfo.value.source == name

    def test_a_missing_companies_yaml_is_what_produces_that(self, monkeypatch, tmp_path):
        """Through the REAL config loader, not a stubbed `load_slugs`.

        `_CFG_CACHE = {}` is the actual degrade the reviewer found; point
        PROJECT_ROOT at an empty dir so there is genuinely no companies.yaml.
        """
        monkeypatch.setattr(_ats_common, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(_ats_common, "_CFG_CACHE", None)
        assert _ats_common.load_slugs("greenhouse") == []

        with pytest.raises(SourceNotConfiguredError):
            greenhouse.fetch_greenhouse_listings()

    def test_the_orchestrator_puts_it_in_neither_set(self, monkeypatch):
        """The wiring: not in `succeeded`, and NOT invented as an error either."""
        monkeypatch.setattr(
            orchestrator.greenhouse,
            "fetch_greenhouse_listings",
            lambda: (_ for _ in ()).throw(
                SourceNotConfiguredError("greenhouse", "no slugs configured")
            ),
        )
        for module, attr in (
            (orchestrator.lever, "fetch_lever_listings"),
            (orchestrator.ashby, "fetch_ashby_listings"),
            (orchestrator.linkedin, "fetch_linkedin_listings"),
        ):
            monkeypatch.setattr(module, attr, lambda: [])
        monkeypatch.setattr(orchestrator.cedars, "fetch_cedars_listings", lambda **kw: [])
        monkeypatch.setattr(orchestrator, "load_seen", lambda source: set())

        _listings, errors, succeeded = orchestrator._fetch_all_sources()

        assert "greenhouse" not in succeeded
        assert "greenhouse" not in errors
        # The run is otherwise untouched: one dead config is not a dead run.
        assert set(succeeded) == {"cedars", "lever", "ashby", "linkedin"}

    def test_the_record_is_pruned_not_reset(self):
        """The consequence in the state file: no fabricated success.

        Pruned means DROPPED, per `update_health`'s existing contract for a
        source that reported nothing — deliberately not "kept at 12", and
        emphatically not "reset to 0 with today as last_success".
        """
        prior = {k: dict(v) for k, v in self._STREAK.items()}
        out = source_health.update_health(prior, succeeded=[], errors={}, today=_DAY)

        assert "greenhouse" not in out
        assert out == {}
        # The reset shape is what the bug wrote. Neither half may appear.
        assert _DAY.isoformat() not in repr(out)
        # update_health is pure — the caller's prior state is not mutated.
        assert prior == self._STREAK

    def test_an_unconfigured_source_cannot_silence_a_real_alarm(self):
        """A source that IS failing still alarms; only the unasked one drops."""
        health = source_health.update_health(
            {**self._STREAK, "cedars": {"consecutive_failures": 2, "first_seen": "2026-06-01"}},
            succeeded=[],
            errors={"cedars": "session expired"},
            today=_DAY,
        )
        assert "greenhouse" not in health
        assert health["cedars"]["consecutive_failures"] == 3
        assert source_health.render_alarm(health) is not None
