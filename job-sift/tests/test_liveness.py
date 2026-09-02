"""LinkedIn entries must age out — issue #1c.

LinkedIn alert emails carry no deadline and each posting appears in exactly one
digest, so `last_seen` never moves and the 30-day `stale` rule was the only
thing that could ever close a LinkedIn row. A posting that shut two days after
it was mailed therefore sat in the register as `open` for a month.

The re-check has one hard requirement, which most of this file is about: it must
FAIL SAFE. A request that did not come back is "could not check", never
"closed". A network error is not evidence about a job.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from job_sift import liveness
from job_sift.open_roles import (
    OpenRole,
    apply_liveness,
    roles_due_liveness_check,
)

TODAY = date(2026, 9, 1)

# Padded for the same reason `_OPEN_PAGE` is: `classify_page` rejects a body
# too short to have been a posting BEFORE it looks for any marker.
_CLOSED_PAGE = (
    "<html><head><title>IMC hiring Software Engineer Intern</title></head><body>"
    "<div class='top-card'>Software Engineer Intern</div>"
    "<div class='description'>" + ("This role has now closed. " * 20) + "</div>"
    "<span class='artdeco-inline-feedback__message'>No longer accepting "
    "applications</span></body></html>"
)

# The page LinkedIn actually serves at the end of an expired-job redirect: a
# company jobs INDEX, on a different host, carrying expiry-flavoured prose.
# Observed live: /jobs/view/3500000000/ 301s to this.
_REDIRECT_TARGET = "https://br.linkedin.com/jobs/escale-vagas?trk=expired_jd_redirect"
_REDIRECT_TARGET_PAGE = (
    "<html><head><title>Escale jobs</title></head><body>"
    "<h1>Jobs at Escale</h1>"
    "<p>This job is no longer available.</p>"
    "<div>" + ("Browse our other openings. " * 20) + "</div>"
    "</body></html>"
)
# Padded to a plausible page weight on purpose: a 200 whose body is two lines
# long is an interstitial or a redirect stub, and `classify_page` calls that
# UNKNOWN rather than guessing.
_OPEN_PAGE = (
    "<html><head><title>IMC hiring Software Engineer Intern</title></head><body>"
    "<div class='top-card'>Software Engineer Intern</div>"
    "<div class='description'>" + ("We are looking for interns. " * 20) + "</div>"
    "<button>Apply</button></body></html>"
)


def _role(key="linkedin:111", *, source="linkedin", status="open", deadline=None,
          last_checked=None, first_seen="2026-08-01", last_seen="2026-08-01"):
    return OpenRole(
        dedup_key=key,
        source=source,
        employer="IMC",
        title="Software Engineer Intern",
        apply_url="https://www.linkedin.com/jobs/view/111/",
        deadline=deadline,
        first_seen=first_seen,
        last_seen=last_seen,
        reason="because",
        status=status,
        last_checked=last_checked,
    )


# --------------------------------------------------------------------------
# The page reading
# --------------------------------------------------------------------------


class TestReadPage:
    def test_the_closed_banner_is_recognised(self):
        assert liveness.classify_page(_CLOSED_PAGE) == liveness.CLOSED

    def test_a_normal_posting_reads_as_open(self):
        assert liveness.classify_page(_OPEN_PAGE) == liveness.OPEN

    @pytest.mark.parametrize("body", [None, "", "   "])
    def test_an_empty_body_is_unknown_not_closed(self, body):
        assert liveness.classify_page(body) == liveness.UNKNOWN

    def test_a_stub_sized_body_is_unknown_not_open(self):
        """A 200 too small to be a posting is an interstitial, not evidence."""
        assert liveness.classify_page("<html><body>redirecting</body></html>") == (
            liveness.UNKNOWN
        )


class TestProbeFailsSafe:
    def test_a_transport_error_is_unknown(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("name resolution failed")

        monkeypatch.setattr(liveness, "_get", _boom)
        assert liveness.probe_linkedin("111") == liveness.UNKNOWN

    @pytest.mark.parametrize("status", [403, 404, 410, 429, 500, 503])
    def test_no_http_status_is_ever_read_as_closed(self, monkeypatch, status):
        """Including 404 — and that one was CHECKED, not reasoned about.

        A genuinely closed posting answers 200 with the banner; 404 is what a
        nonexistent job id returns. 404 is not LinkedIn's expiry signal, so
        reading it as one would only ever delete rows we could not look at.

        3xx is deliberately absent from this list: `_get` follows redirects, so
        production can essentially never hand one to `probe_linkedin`. Asserting
        on it here looked like coverage and was vacuous — the redirect hazard is
        covered for real in TestGetFollowsRedirectsSafely.
        """
        monkeypatch.setattr(
            liveness, "_get", lambda url, **kw: (status, liveness.job_url("111"), _CLOSED_PAGE)
        )
        assert liveness.probe_linkedin("111") == liveness.UNKNOWN

    def test_only_a_200_carrying_the_banner_closes_a_role(self, monkeypatch):
        monkeypatch.setattr(
            liveness, "_get", lambda url, **kw: (200, liveness.job_url("111"), _CLOSED_PAGE)
        )
        assert liveness.probe_linkedin("111") == liveness.CLOSED


class TestTheMarkerSet:
    """Exactly one marker, because the other two were net-negative.

    All three real closed rows (IMC 1000000001, HSBC 1000000003, BBVA
    1000000004) match "no longer accepting applications" on its own, so the
    broader strings contributed no true positives — and every string below is
    ordinary LinkedIn error prose that would have retired a live role.
    """

    @pytest.mark.parametrize(
        "prose",
        [
            "This page is no longer available.",
            "Sorry, this content is no longer available",
            "That profile is no longer available on LinkedIn",
            "This job is no longer available",
        ],
    )
    def test_generic_expiry_prose_does_not_retire_a_role(self, prose):
        page = (
            "<html><head><title>LinkedIn</title></head><body><p>" + prose + "</p>"
            "<div>" + ("Try searching for something else. " * 20) + "</div></body></html>"
        )
        assert liveness.classify_page(page) != liveness.CLOSED

    def test_the_one_real_banner_still_fires(self):
        assert liveness.classify_page(_CLOSED_PAGE) == liveness.CLOSED


class TestIsPostingUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.linkedin.com/jobs/view/1000000001/", True),
            ("https://linkedin.com/jobs/view/1/", True),
            # A real LinkedIn host, so a host-only check would have passed it.
            (_REDIRECT_TARGET, False),
            ("https://www.linkedin.com/authwall?sessionRedirect=x", False),
            # A posting-shaped path somewhere else entirely.
            ("https://linkedin.com.evil.test/jobs/view/1/", False),
            ("", False),
            (None, False),
        ],
    )
    def test_only_a_linkedin_posting_page_qualifies(self, url, expected):
        assert liveness.is_posting_url(url) is expected


class TestGetFollowsRedirectsSafely:
    """Drives the REAL `_get` against a stubbed transport.

    Every other test in this file monkeypatches `_get` away, which is exactly
    why the redirect hole survived the first round: the bug was in the code
    those tests replaced. `httpx.MockTransport` opens no socket, so the suite's
    `no_network` guard is untouched.
    """

    def _transport(self, handler):
        return httpx.MockTransport(handler)

    def test_a_cross_host_redirect_carrying_expiry_prose_is_unknown(self):
        """The CRITICAL case, reproduced from live behaviour.

        `/jobs/view/3500000000/` 301s to a company jobs index on br.linkedin.com
        whose body says a job is no longer available. Reading a verdict off that
        page marks a LIVE role `expired`, which drops it from the register and
        prunes it 60 days later — and LinkedIn never re-lists, so it is
        unrecoverable.
        """

        def handler(request):
            if "/jobs/view/" in request.url.path:
                return httpx.Response(301, headers={"Location": _REDIRECT_TARGET})
            return httpx.Response(200, text=_REDIRECT_TARGET_PAGE)

        verdict = liveness.probe_linkedin("3500000000", transport=self._transport(handler))
        assert verdict == liveness.UNKNOWN

    def test_a_redirect_to_the_auth_wall_is_unknown_not_open(self):
        """It used to return OPEN and stamp `last_checked` — a week of silence
        bought by a request that never saw the posting."""

        def handler(request):
            if "/jobs/view/" in request.url.path:
                return httpx.Response(
                    302, headers={"Location": "https://www.linkedin.com/authwall?x=1"}
                )
            return httpx.Response(200, text="<html><body>" + ("Sign in. " * 60) + "</body></html>")

        verdict = liveness.probe_linkedin("111", transport=self._transport(handler))
        assert verdict == liveness.UNKNOWN

    def test_a_redirect_that_stays_on_the_posting_is_still_read(self):
        """The canonicalising hop LinkedIn does on a real posting must not be
        collateral damage."""

        def handler(request):
            if not request.url.query:
                return httpx.Response(
                    301,
                    headers={"Location": "https://www.linkedin.com/jobs/view/111/?refId=abc"},
                )
            return httpx.Response(200, text=_CLOSED_PAGE)

        verdict = liveness.probe_linkedin("111", transport=self._transport(handler))
        assert verdict == liveness.CLOSED

    def test_a_live_posting_reads_as_open_through_the_real_get(self):
        handler = lambda request: httpx.Response(200, text=_OPEN_PAGE)  # noqa: E731
        assert liveness.probe_linkedin("111", transport=self._transport(handler)) == liveness.OPEN

    def test_an_endless_redirect_chain_is_bounded_and_unknown(self):
        """`_MAX_REDIRECTS` is what bounds a probe's worst case — the httpx
        `timeout` is per socket operation, so it does not bound a chain at all."""
        hops = []

        def handler(request):
            hops.append(str(request.url))
            return httpx.Response(
                301, headers={"Location": f"https://www.linkedin.com/jobs/view/{len(hops)}/"}
            )

        verdict = liveness.probe_linkedin("111", transport=self._transport(handler))
        assert verdict == liveness.UNKNOWN
        assert len(hops) <= liveness._MAX_REDIRECTS + 1

    def test_a_transport_that_raises_is_unknown(self):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        assert liveness.probe_linkedin("111", transport=self._transport(handler)) == (
            liveness.UNKNOWN
        )


# --------------------------------------------------------------------------
# Selecting what to check
# --------------------------------------------------------------------------


class TestRolesDue:
    def test_only_linkedin_rows_are_checked(self):
        due = roles_due_liveness_check(
            [_role("cedars:1", source="cedars"), _role("linkedin:1")], TODAY
        )
        assert [r.dedup_key for r in due] == ["linkedin:1"]

    def test_a_row_with_a_real_deadline_is_left_to_the_ager(self):
        due = roles_due_liveness_check([_role(deadline="2026-10-01")], TODAY)
        assert due == []

    @pytest.mark.parametrize("status", ["applied", "dismissed", "expired", "stale"])
    def test_only_open_rows_are_checked(self, status):
        assert roles_due_liveness_check([_role(status=status)], TODAY) == []

    def test_a_recently_checked_row_is_not_re_checked(self):
        due = roles_due_liveness_check(
            [_role(last_checked="2026-08-30")], TODAY, interval_days=7
        )
        assert due == []

    def test_the_budget_is_a_hard_cap(self):
        roles = [_role(f"linkedin:{i}") for i in range(20)]
        assert len(roles_due_liveness_check(roles, TODAY, limit=5)) == 5

    def test_never_checked_rows_go_first(self):
        checked = _role("linkedin:checked", last_checked="2026-08-01")
        never = _role("linkedin:never")
        due = roles_due_liveness_check([checked, never], TODAY, limit=1)
        assert [r.dedup_key for r in due] == ["linkedin:never"]


# --------------------------------------------------------------------------
# Applying the verdict
# --------------------------------------------------------------------------


class TestApplyLiveness:
    def test_a_confirmed_closed_posting_is_retired(self):
        out = apply_liveness([_role()], {"linkedin:111": liveness.CLOSED}, TODAY)
        assert out[0].status == "expired"
        assert out[0].last_checked == TODAY.isoformat()

    def test_an_open_posting_is_only_stamped_not_touched(self):
        out = apply_liveness([_role()], {"linkedin:111": liveness.OPEN}, TODAY)
        assert out[0].status == "open"
        assert out[0].last_checked == TODAY.isoformat()

    def test_unknown_changes_absolutely_nothing(self):
        """Not even `last_checked` — a failed check must not buy a week of silence."""
        out = apply_liveness([_role()], {"linkedin:111": liveness.UNKNOWN}, TODAY)
        assert out[0].status == "open"
        assert out[0].last_checked is None

    @pytest.mark.parametrize("sticky", ["applied", "dismissed"])
    def test_a_hand_set_status_is_never_overwritten(self, sticky):
        out = apply_liveness(
            [_role(status=sticky)], {"linkedin:111": liveness.CLOSED}, TODAY
        )
        assert out[0].status == sticky

    def test_it_does_not_mutate_the_input(self):
        role = _role()
        apply_liveness([role], {"linkedin:111": liveness.CLOSED}, TODAY)
        assert role.status == "open"


# --------------------------------------------------------------------------
# Wiring into the run
# --------------------------------------------------------------------------


class TestLivenessPassInTheRun:
    """The pass is a convenience bolted onto a run that must not be able to die.

    `no_network` (tests/conftest.py) is doing real work in this class: any test
    here that accidentally let a probe through would fail loudly rather than
    quietly reaching LinkedIn.
    """

    def test_a_crash_in_the_pass_leaves_the_register_untouched(self, monkeypatch):
        from job_sift import orchestrator

        def _boom(job_id):
            raise RuntimeError("liveness exploded")

        monkeypatch.setattr(orchestrator.liveness, "probe_linkedin", _boom)
        roles = [_role()]
        out = orchestrator._liveness_pass(roles, TODAY)
        assert [r.status for r in out] == ["open"]

    def test_a_closed_posting_is_retired_by_the_pass(self, monkeypatch):
        from job_sift import orchestrator

        monkeypatch.setattr(
            orchestrator.liveness, "probe_linkedin", lambda job_id: liveness.CLOSED
        )
        out = orchestrator._liveness_pass([_role("linkedin:111")], TODAY)
        assert out[0].status == "expired"

    def test_the_probe_is_given_the_bare_job_id(self, monkeypatch):
        """`dedup_key` is source-prefixed; the URL builder wants the id alone."""
        from job_sift import orchestrator

        asked: list[str] = []
        monkeypatch.setattr(
            orchestrator.liveness,
            "probe_linkedin",
            lambda job_id: asked.append(job_id) or liveness.OPEN,
        )
        orchestrator._liveness_pass([_role("linkedin:1000000001")], TODAY)
        assert asked == ["1000000001"]

    def test_a_zero_budget_disables_the_pass_entirely(self, monkeypatch):
        from job_sift import config, orchestrator

        monkeypatch.setenv(config.LIVENESS_MAX_ENV, "0")

        def _never(job_id):
            raise AssertionError("probed despite a zero budget")

        monkeypatch.setattr(orchestrator.liveness, "probe_linkedin", _never)
        assert orchestrator._liveness_pass([_role()], TODAY)[0].status == "open"

    def test_a_dry_run_never_probes(self, monkeypatch, tmp_path):
        """--dry-run is offline and side-effect-free, network reads included."""
        from job_sift import config, orchestrator

        monkeypatch.setattr(config, "STATE_DIR", tmp_path)

        def _never(*a, **k):
            raise AssertionError("the liveness pass ran under --dry-run")

        monkeypatch.setattr(orchestrator, "_liveness_pass", _never)
        monkeypatch.setattr(orchestrator, "load_open_roles", lambda: [_role()])
        monkeypatch.setattr(orchestrator, "read_open_roles_note", lambda: "")
        orchestrator._update_open_roles([], TODAY, dry_run=True)

    def test_stub_mode_never_probes(self, monkeypatch, tmp_path):
        from job_sift import config, orchestrator

        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setenv("JOB_SIFT_STUB", "1")

        def _never(*a, **k):
            raise AssertionError("the liveness pass ran under --stub")

        monkeypatch.setattr(orchestrator, "_liveness_pass", _never)
        monkeypatch.setattr(orchestrator, "load_open_roles", lambda: [_role()])
        monkeypatch.setattr(orchestrator, "read_open_roles_note", lambda: "")
        monkeypatch.setattr(orchestrator, "save_open_roles", lambda roles: None)
        monkeypatch.setattr(orchestrator, "write_open_roles", lambda md: None)
        orchestrator._update_open_roles([], TODAY, dry_run=False)


class TestTheLivenessBudget:
    """The pass needs a wall-clock ceiling of its own.

    `httpx`'s `timeout` is per socket OPERATION, not per request, so it bounds
    neither a redirect chain nor a slow-drip body — measured against a local
    server, a 6-hop chain at 2s/hop took 14.0s and a drip-fed body 24.0s, both
    under a configured 10s timeout. A serial loop with no ceiling is the exact
    shape recorded in concurrency.py's header as having got a run SIGTERM'd
    before it could push.
    """

    def _slow_roles(self, n):
        return [_role(f"linkedin:{i}") for i in range(n)]

    def test_a_hanging_probe_cannot_outlast_the_budget(self, monkeypatch):
        import time

        from job_sift import config, orchestrator

        monkeypatch.setenv(config.LIVENESS_BUDGET_ENV, "0.3")
        monkeypatch.setenv(config.LIVENESS_MAX_ENV, "5")
        monkeypatch.setattr(
            orchestrator.liveness, "probe_linkedin", lambda job_id: time.sleep(30)
        )

        started = time.monotonic()
        out = orchestrator._liveness_pass(self._slow_roles(5), TODAY)
        elapsed = time.monotonic() - started

        assert elapsed < 5, f"the liveness budget is not a ceiling: {elapsed:.2f}s"
        # Abandoned probes produce no verdict, which is the same no-op as
        # UNKNOWN: nothing is retired and nothing is stamped.
        assert all(r.status == "open" for r in out)
        assert all(r.last_checked is None for r in out)

    def test_a_fast_probe_does_not_wait_for_the_budget(self, monkeypatch):
        import time

        from job_sift import config, orchestrator

        monkeypatch.setenv(config.LIVENESS_BUDGET_ENV, "30")
        monkeypatch.setattr(
            orchestrator.liveness, "probe_linkedin", lambda job_id: liveness.OPEN
        )
        started = time.monotonic()
        orchestrator._liveness_pass(self._slow_roles(3), TODAY)
        assert time.monotonic() - started < 5

    def test_each_row_is_probed_with_its_own_id(self, monkeypatch):
        """A lambda closing over the loop variable would probe the last row N times."""
        from job_sift import orchestrator

        asked: list[str] = []
        monkeypatch.setattr(
            orchestrator.liveness,
            "probe_linkedin",
            lambda job_id: asked.append(job_id) or liveness.OPEN,
        )
        orchestrator._liveness_pass(
            [_role("linkedin:111"), _role("linkedin:222"), _role("linkedin:333")], TODAY
        )
        assert sorted(asked) == ["111", "222", "333"]
