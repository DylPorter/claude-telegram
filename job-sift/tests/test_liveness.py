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

import pytest

from job_sift import liveness
from job_sift.open_roles import (
    OpenRole,
    apply_liveness,
    roles_due_liveness_check,
)

TODAY = date(2026, 9, 1)

_CLOSED_PAGE = (
    "<html><body><div class='top-card'>Software Engineer Intern</div>"
    "<span class='artdeco-inline-feedback__message'>No longer accepting "
    "applications</span></body></html>"
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

    @pytest.mark.parametrize("status", [301, 403, 404, 410, 429, 500, 503])
    def test_no_http_status_is_ever_read_as_closed(self, monkeypatch, status):
        """Including 404. A missing page is a page we could not read."""
        monkeypatch.setattr(liveness, "_get", lambda url: (status, _CLOSED_PAGE))
        assert liveness.probe_linkedin("111") == liveness.UNKNOWN

    def test_only_a_200_carrying_the_banner_closes_a_role(self, monkeypatch):
        monkeypatch.setattr(liveness, "_get", lambda url: (200, _CLOSED_PAGE))
        assert liveness.probe_linkedin("111") == liveness.CLOSED


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
