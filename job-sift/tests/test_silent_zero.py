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
from job_sift.schema import ClassifierResult, JobListing
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


def _with_location(vendor: str, payload, location: str):
    """The same one-job payload, given a location, in that vendor's own shape.

    The three ATSs disagree about where a location lives — Greenhouse nests it
    under `location.name`, Lever under `categories.location`, Ashby puts it flat
    on `location`. The allowlist tests below are about a listing that HAS a
    location (a listing without one passes the filter unconditionally, which
    would make them vacuous), so the shape has to be right per vendor.
    """
    if vendor == "greenhouse":
        return {"jobs": [dict(payload["jobs"][0], location={"name": location})]}
    if vendor == "lever":
        return [dict(payload[0], categories={"location": location})]
    if vendor == "ashby":
        return {"jobs": [dict(payload["jobs"][0], location=location)]}
    raise AssertionError(f"unknown vendor {vendor!r}")


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


class TestAnEmptyLocationAllowlistIsAConfigDefect:
    """#6 — the seventh costume, one level below the adapters.

    `location_matches` returns True for a listing with NO location (let a human
    decide) and otherwise asks whether any allowlist substring appears in it.
    With the allowlist empty, `any(...)` over an empty sequence is False, so
    EVERY located listing is filtered out. The adapter polls fine, keeps
    nothing, raises nothing, and returns `[]` — which `source_health` scores a
    SUCCESS: streak zeroed, `last_success` stamped. Verified before the fix:
    `allowlist empty -> HK listing matches? False`.

    That is a config defect wearing the "I looked and there was nothing" costume
    — the same overload the whole branch exists to make unrepresentable. It is
    NOT a fetch failure either: nothing was wrong with the network or the board.
    Nobody ever asked a real question, so it belongs in the third bucket with
    the missing-slugs case — `SourceNotConfiguredError`, pruned by
    `update_health`, scored neither success nor failure.

    The opposite error would be just as bad, so it is pinned below: a POPULATED
    allowlist that legitimately matches zero listings today is a real fetch of a
    real answer and must keep counting as a success.
    """

    @pytest.fixture(autouse=True)
    def _reachable_boards(self, monkeypatch):
        """One live board per vendor, so the only thing under test is config.

        Without this the empty-allowlist tests could pass for the wrong reason
        (total-failure escalation) rather than because the allowlist was checked.
        """

        def _serve(payload):
            return lambda self, url, **kw: _Resp(payload)

        self._serve = _serve

    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    @pytest.mark.parametrize(
        "allowlist",
        [[], None, ["   "], [""], ["", "  ", "\t"]],
        ids=["empty", "missing", "whitespace_entry", "blank_entry", "all_blank"],
    )
    def test_an_empty_allowlist_is_not_a_filter_that_matches_nothing(
        self, monkeypatch, name, module, fetch, payload, allowlist
    ):
        monkeypatch.setattr(module, "load_slugs", lambda vendor: ["a"])
        # NOT patching `load_location_allowlist` here — these cases are about
        # what that function does with the raw YAML, so the real one has to run.
        monkeypatch.setattr(
            _ats_common, "_load_companies_yaml",
            lambda: {"location_allowlist": list(allowlist)} if allowlist is not None else {},
        )
        monkeypatch.setattr(httpx.Client, "get", self._serve(payload))

        with pytest.raises(SourceNotConfiguredError) as excinfo:
            fetch()
        assert excinfo.value.source == name
        assert "location_allowlist" in str(excinfo.value)

    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    def test_it_raises_before_spending_a_single_request(
        self, monkeypatch, name, module, fetch, payload
    ):
        """Fail fast, like the missing-slugs check: there is nothing to learn
        from polling ten boards whose every result we are about to discard."""
        monkeypatch.setattr(module, "load_slugs", lambda vendor: ["a", "b", "c"])
        monkeypatch.setattr(_ats_common, "load_location_allowlist", list)

        def _never(*_a, **_kw):
            raise AssertionError("it polled a board before checking its own config")

        monkeypatch.setattr(httpx.Client, "get", _never)

        with pytest.raises(SourceNotConfiguredError):
            fetch()

    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    def test_a_populated_allowlist_matching_nothing_today_is_still_a_success(
        self, monkeypatch, name, module, fetch, payload
    ):
        """THE DIRECTION THAT MUST NOT REGRESS.

        The listing is in Reykjavik and the allowlist says Hong Kong. We looked,
        we read a real board, and the honest answer is zero — a success, and one
        that must keep resetting the failure streak. Manufacturing a failure
        here would be the mirror image of the bug being fixed, and just as
        dishonest.
        """
        located = _with_location(name, payload, "Reykjavik, Iceland")
        monkeypatch.setattr(module, "load_slugs", lambda vendor: ["a"])
        monkeypatch.setattr(_ats_common, "load_location_allowlist", lambda: ["hong kong"])
        monkeypatch.setattr(httpx.Client, "get", self._serve(located))

        assert fetch() == []

    @pytest.mark.parametrize("name,module,fetch,payload", _ADAPTERS, ids=_IDS)
    def test_the_premise_a_populated_allowlist_still_matches_what_it_should(
        self, monkeypatch, name, module, fetch, payload
    ):
        """Without this the test above could pass on an adapter that had simply
        stopped returning anything at all."""
        located = _with_location(name, payload, "Hong Kong")
        monkeypatch.setattr(module, "load_slugs", lambda vendor: ["a"])
        monkeypatch.setattr(_ats_common, "load_location_allowlist", lambda: ["hong kong"])
        monkeypatch.setattr(httpx.Client, "get", self._serve(located))

        got = fetch()
        assert [L.source for L in got] == [name]


# ---------------------------------------------------------------------------
# The same shape, one stage further in: THE CLASSIFIER.
#
# Everything above hardens the FETCH. But a listing that was fetched fine and
# then handed to a classifier that never answered used to be scored
# `skip / out_of_scope / "batch fallback"` — a rejection minted for a call that
# never happened — pushed nowhere, and permanently written into the seen-set.
# The digest read exactly like a quiet day. These pin the three properties the
# fix has to have, end to end through `run()`.
# ---------------------------------------------------------------------------


class TestAClassifierOutageIsVisibleAndNonConsuming:
    def _harness(self, monkeypatch, tmp_path, *, verdicts):
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setenv("JOB_SIFT_STUB", "0")
        monkeypatch.setattr(config, "assert_required", lambda: None)

        listings = [
            JobListing(
                source="cedars",
                external_id=str(i),
                employer=emp,
                title=title,
                apply_url=f"https://example.com/{i}",
                location="Hong Kong",
            )
            for i, (emp, title) in enumerate(
                [
                    ("Anthropic", "Machine Learning Intern"),
                    ("Some Startup Ltd", "Software Engineer (6-month contract)"),
                    ("HKU", "Research Assistant (Computer Science), Part Time"),
                ]
            )
        ]
        monkeypatch.setattr(
            orchestrator, "_fetch_all_sources", lambda: (list(listings), {}, ["cedars"])
        )
        monkeypatch.setattr(orchestrator, "log_classification", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "_update_open_roles", lambda *a, **k: [])
        monkeypatch.setattr(orchestrator, "write_archive", lambda *a, **k: None)
        monkeypatch.setattr(orchestrator, "classify_batch", lambda ls: list(verdicts))

        pushed: list[str] = []
        monkeypatch.setattr(orchestrator, "push_messages", lambda msgs: pushed.extend(msgs))
        return listings, pushed

    def test_the_digest_says_so_instead_of_printing_a_bare_quiet_line(
        self, monkeypatch, tmp_path
    ):
        _, pushed = self._harness(monkeypatch, tmp_path, verdicts=[None, None, None])
        assert orchestrator.run() == 0

        blob = "\n".join(pushed)
        # The exact digest the reviewer reproduced: a quiet line and a rolling
        # chip, with nothing anywhere saying the classifier never ran.
        assert "No new prestige matches today" in blob, "quiet line still expected"
        assert "⚠️" in blob, "an outage must not render as a clean quiet day"
        assert "classifier" in blob
        assert "3" in blob
        # ...and it must not be the ONLY bubble either side of the chip.
        assert any("classifier" in m for m in pushed)

    def test_the_listings_are_not_consumed_and_come_back_next_run(
        self, monkeypatch, tmp_path
    ):
        """The permanent half of the bug: `filter_new` banks the ids up front.

        Nothing retries automatically, so an id written here is gone for good —
        the outage did not just produce one bad digest, it ate the backlog.
        """
        listings, _ = self._harness(monkeypatch, tmp_path, verdicts=[None, None, None])
        assert orchestrator.run() == 0

        from job_sift.dedupe import load_seen

        assert load_seen("cedars") == set()
        # And prove the retry actually happens: same listings, working classifier.
        assert [l.external_id for l in listings] == ["0", "1", "2"]

    def test_a_partial_outage_commits_only_what_was_actually_judged(
        self, monkeypatch, tmp_path
    ):
        judged = ClassifierResult("skip", "out_of_scope", "not for you")
        _, pushed = self._harness(monkeypatch, tmp_path, verdicts=[judged, None, None])
        assert orchestrator.run() == 0

        from job_sift.dedupe import load_seen

        # Listing 0 got a real verdict and is delivered; 1 and 2 did not.
        assert load_seen("cedars") == {"0"}
        assert "classifier" in "\n".join(pushed)

    def test_a_working_classifier_is_completely_unaffected(self, monkeypatch, tmp_path):
        """The no-op case: no ⚠️, and every id committed exactly as before."""
        judged = ClassifierResult("skip", "out_of_scope", "not for you")
        _, pushed = self._harness(
            monkeypatch, tmp_path, verdicts=[judged, judged, judged]
        )
        assert orchestrator.run() == 0

        from job_sift.dedupe import load_seen

        assert load_seen("cedars") == {"0", "1", "2"}
        assert "classifier" not in "\n".join(pushed)

    def test_the_outage_does_not_invent_a_source_health_record(
        self, monkeypatch, tmp_path
    ):
        """The ⚠️ channel is shared with sources; the health COUNTERS are not.

        `source_errors` is reused so there is one banner mechanism rather than
        two, but "classifier" is not a source and must never acquire a
        consecutive-failure streak or a `last_success` in the state file.
        """
        self._harness(monkeypatch, tmp_path, verdicts=[None, None, None])
        assert orchestrator.run() == 0
        assert "classifier" not in source_health.load_health()


# ---------------------------------------------------------------------------
# The LinkedIn PARSER half. The transport half (gws, auth, message bodies) has
# been hardened since day one; the parser had not. LinkedIn owns this email
# template. When it changes, every selector misses, `unreadable` stays 0 so the
# existing total-failure guard never fires, and the adapter returns [] →
# `succeeded` → streak reset, `last_success` stamped today. A dead parser scored
# as a healthy quiet day, in the source issue #1c depends on.
# ---------------------------------------------------------------------------

_CARD = """
<tr>
  <td><a href="https://www.linkedin.com/comm/jobs/view/{jid}/?trk=x">
      <img alt="{company}" src="logo.png"></a></td>
  <td><a href="https://www.linkedin.com/comm/jobs/view/{jid}/?trk=y">{title}</a>
      <div>{company} &middot; Hong Kong</div></td>
</tr>
"""

# A digest whose card markup changed: the job links are still there (LinkedIn
# has not changed its URLs), but the logo <img alt> and the plain-text title
# anchor are gone, so no selector matches.
_REDESIGNED_CARD = """
<div data-job="{jid}">
  <a href="https://www.linkedin.com/comm/jobs/view/{jid}/?trk=x">
    <span class="logo" style="background-image:url(logo.png)"></span></a>
  <a href="https://www.linkedin.com/comm/jobs/view/{jid}/?trk=y"><h3>{title}</h3></a>
</div>
"""

_CONFIRMATION = """
<html><body><p>Your job alert for Software Engineer in Hong Kong has been
created.</p><a href="https://www.linkedin.com/jobs/">See jobs</a></body></html>
"""


def _email(template, *jids):
    return "<html><body><table>" + "".join(
        template.format(jid=j, company=f"Company {j}", title="Software Engineer")
        for j in jids
    ) + "</table></body></html>"


class TestALinkedInTemplateChangeIsNotAQuietDay:
    @pytest.fixture(autouse=True)
    def _use_the_real_adapter(self, real_sources):
        # conftest's `stub_all_sources` neutralises every fetcher by default;
        # this class is ABOUT the real one.
        real_sources()

    def _patch(self, monkeypatch, bodies):
        monkeypatch.setattr(
            linkedin, "_gws_list_messages", lambda: [{"id": str(i)} for i in range(len(bodies))]
        )
        monkeypatch.setattr(linkedin, "_gws_fetch_html", lambda mid: bodies[int(mid)])

    def test_the_premise_the_emails_are_readable_so_the_old_guard_never_fires(
        self, monkeypatch
    ):
        """Reproduces the reviewer's setup: `unreadable == 0`, and yet nothing parsed."""
        bodies = [_email(_REDESIGNED_CARD, "111"), _email(_REDESIGNED_CARD, "222")]
        for b in bodies:
            listings, cards = linkedin._parse_alert_email_detailed(b)
            assert listings == [] and cards == 1  # readable, carded, unparseable

    def test_a_redesigned_card_raises_instead_of_returning_empty(self, monkeypatch):
        self._patch(
            monkeypatch, [_email(_REDESIGNED_CARD, "111"), _email(_REDESIGNED_CARD, "222")]
        )
        with pytest.raises(SourceFetchError) as exc:
            linkedin.fetch_linkedin_listings()
        assert "template has changed" in str(exc.value)

    def test_a_confirmation_email_is_still_a_legitimate_zero(self, monkeypatch):
        """The carve-out that keeps this from crying wolf.

        LinkedIn's "your job alert has been created" mails come from the same
        senders and carry no job links at all. Zero CARDS is real evidence of
        nothing; zero listings from a carded email is not.
        """
        self._patch(monkeypatch, [_CONFIRMATION])
        assert linkedin.fetch_linkedin_listings() == []

    def test_a_partial_parse_failure_still_returns_what_it_read(self, monkeypatch):
        """One broken email must not discard the jobs the others gave up."""
        self._patch(monkeypatch, [_email(_REDESIGNED_CARD, "111"), _email(_CARD, "222")])
        got = linkedin.fetch_linkedin_listings()
        assert [l.external_id for l in got] == ["222"]

    def test_the_happy_path_is_unchanged(self, monkeypatch):
        self._patch(monkeypatch, [_email(_CARD, "111", "222")])
        got = linkedin.fetch_linkedin_listings()
        assert sorted(l.external_id for l in got) == ["111", "222"]
        assert got[0].employer.startswith("Company")

    def test_a_mix_of_confirmation_and_broken_digest_still_raises(self, monkeypatch):
        """The confirmation carve-out must not become a shield for a real failure."""
        self._patch(monkeypatch, [_CONFIRMATION, _email(_REDESIGNED_CARD, "111")])
        with pytest.raises(SourceFetchError):
            linkedin.fetch_linkedin_listings()
