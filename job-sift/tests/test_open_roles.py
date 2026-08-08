"""Tests for the rolling Open Roles register.

Key behaviours under test:
  - upsert is idempotent on `first_seen` and never resurrects a hand-set status
  - ageing closes out past deadlines / undated roles we stopped seeing
  - the active-roles sort puts undated roles LAST (the whole point of the note)
  - pruning keeps `applied` forever (it's the operator's application history)
"""

from __future__ import annotations

from datetime import date, timedelta

from job_sift.open_roles import (
    OpenRole,
    active_roles,
    age_roles,
    apply_status_overrides,
    closing_within,
    parse_status_overrides,
    prune,
    upsert_roles,
)
from job_sift.render import render_open_roles
from job_sift.schema import JobListing

TODAY = date(2026, 7, 31)


def _listing(external_id="1", employer="PwC", title="Intern", deadline=None):
    return JobListing(
        source="cedars",
        external_id=external_id,
        employer=employer,
        title=title,
        apply_url=f"https://example.com/{external_id}",
        deadline=deadline,
    )


def _role(key="cedars:1", *, deadline=None, last_seen="2026-07-31", status="open",
          first_seen="2026-07-01", employer="PwC"):
    return OpenRole(
        dedup_key=key,
        source="cedars",
        employer=employer,
        title="Intern",
        apply_url="https://example.com",
        deadline=deadline,
        first_seen=first_seen,
        last_seen=last_seen,
        reason="prestige firm, in scope",
        status=status,
    )


# --- upsert ---------------------------------------------------------------


def test_upsert_appends_new_key():
    out = upsert_roles([], [(_listing("42", deadline=date(2026, 8, 15)), "why")], TODAY)
    assert len(out) == 1
    role = out[0]
    assert role.dedup_key == "cedars:42"
    assert role.first_seen == role.last_seen == "2026-07-31"
    assert role.deadline == "2026-08-15"
    assert role.status == "open"
    assert role.reason == "why"


def test_upsert_existing_key_bumps_last_seen_preserves_first_seen():
    existing = [_role(first_seen="2026-07-03", last_seen="2026-07-10")]
    out = upsert_roles(existing, [(_listing("1"), "fresh reason")], TODAY)
    assert len(out) == 1
    assert out[0].first_seen == "2026-07-03"
    assert out[0].last_seen == "2026-07-31"
    assert out[0].reason == "fresh reason"


def test_upsert_does_not_resurrect_applied_or_dismissed():
    existing = [
        _role("cedars:1", status="applied"),
        _role("cedars:2", status="dismissed"),
        _role("cedars:3", status="expired"),
    ]
    out = upsert_roles(
        existing,
        [(_listing("1"), "r"), (_listing("2"), "r"), (_listing("3"), "r")],
        TODAY,
    )
    by_key = {r.dedup_key: r for r in out}
    assert by_key["cedars:1"].status == "applied"
    assert by_key["cedars:2"].status == "dismissed"
    # non-sticky statuses DO re-open when the listing is seen again
    assert by_key["cedars:3"].status == "open"


def test_upsert_does_not_mutate_input():
    existing = [_role(last_seen="2026-07-10")]
    upsert_roles(existing, [(_listing("1"), "r")], TODAY)
    assert existing[0].last_seen == "2026-07-10"


# --- ageing ---------------------------------------------------------------


def test_age_marks_past_deadline_expired():
    out = age_roles([_role(deadline="2026-07-30")], TODAY)
    assert out[0].status == "expired"


def test_age_keeps_today_deadline_open():
    out = age_roles([_role(deadline="2026-07-31")], TODAY)
    assert out[0].status == "open"


def test_age_marks_undated_unseen_role_stale():
    out = age_roles([_role(deadline=None, last_seen="2026-06-20")], TODAY, stale_after_days=30)
    assert out[0].status == "stale"


def test_age_leaves_recently_seen_undated_role_open():
    out = age_roles([_role(deadline=None, last_seen="2026-07-25")], TODAY, stale_after_days=30)
    assert out[0].status == "open"


def test_age_never_downgrades_applied_or_dismissed():
    roles = [
        _role("cedars:1", deadline="2026-01-01", status="applied"),
        _role("cedars:2", deadline=None, last_seen="2026-01-01", status="dismissed"),
    ]
    out = age_roles(roles, TODAY)
    assert [r.status for r in out] == ["applied", "dismissed"]


# --- sorting --------------------------------------------------------------


def test_active_roles_sorts_deadline_ascending_with_none_last():
    roles = [
        _role("cedars:none1", deadline=None, employer="Zeta"),
        _role("cedars:late", deadline="2026-09-01", employer="Beta"),
        _role("cedars:none2", deadline=None, employer="Alpha"),
        _role("cedars:soon", deadline="2026-08-02", employer="Gamma"),
    ]
    out = active_roles(roles)
    assert [r.dedup_key for r in out] == [
        "cedars:soon",
        "cedars:late",
        # None-deadline entries come LAST, tie-broken by employer
        "cedars:none2",
        "cedars:none1",
    ]
    assert out[-1].deadline is None
    assert out[-2].deadline is None


def test_active_roles_excludes_non_open():
    roles = [
        _role("cedars:1", status="open"),
        _role("cedars:2", status="expired"),
        _role("cedars:3", status="applied"),
    ]
    assert [r.dedup_key for r in active_roles(roles)] == ["cedars:1"]


# --- closing_within -------------------------------------------------------


def test_closing_within_boundary_seven_in_eight_out():
    roles = [
        _role("cedars:7", deadline=(TODAY + timedelta(days=7)).isoformat()),
        _role("cedars:8", deadline=(TODAY + timedelta(days=8)).isoformat()),
    ]
    keys = [r.dedup_key for r in closing_within(roles, TODAY, days=7)]
    assert keys == ["cedars:7"]


def test_closing_within_excludes_undated_and_past():
    roles = [
        _role("cedars:none", deadline=None),
        _role("cedars:past", deadline="2026-07-01"),
        _role("cedars:today", deadline=TODAY.isoformat()),
    ]
    assert [r.dedup_key for r in closing_within(roles, TODAY)] == ["cedars:today"]


# --- prune ----------------------------------------------------------------


def test_prune_keeps_applied_forever_and_drops_old_expired():
    roles = [
        _role("cedars:applied", status="applied", last_seen="2024-01-01"),
        _role("cedars:old", status="expired", last_seen="2026-01-01"),
        _role("cedars:recent", status="expired", last_seen="2026-07-20"),
        _role("cedars:open", status="open", last_seen="2020-01-01"),
    ]
    keys = {r.dedup_key for r in prune(roles, TODAY, keep_days=60)}
    assert keys == {"cedars:applied", "cedars:recent", "cedars:open"}


# --- status overrides -----------------------------------------------------


def test_parse_status_overrides_reads_markers():
    md = (
        "- **PwC** — Intern\n"
        "  <!-- status:applied cedars:123 -->\n"
        "- **CLSA** — Analyst\n"
        "  <!-- status:dismissed cedars:456 -->\n"
        "- **Other** — Role\n"
        "  <!-- status:open cedars:789 -->\n"
        "no marker here\n"
        "  <!-- status:garbage -->\n"
    )
    overrides = parse_status_overrides(md)
    # only sticky, user-intent statuses are honoured
    assert overrides == {"cedars:123": "applied", "cedars:456": "dismissed"}


def test_status_override_round_trip_through_rendered_note():
    roles = [_role("cedars:1", deadline="2026-08-20", status="open")]
    md = render_open_roles(roles, TODAY)
    # the operator hand-edits the emitted marker
    edited = md.replace("<!-- status:open cedars:1 -->", "<!-- status:applied cedars:1 -->")

    overrides = parse_status_overrides(edited)
    assert overrides == {"cedars:1": "applied"}

    restored = apply_status_overrides(roles, overrides)
    assert restored[0].status == "applied"
    # ...and a later run must not undo it
    after_run = age_roles(upsert_roles(restored, [(_listing("1"), "r")], TODAY), TODAY)
    assert after_run[0].status == "applied"


def test_parse_status_overrides_empty_on_plain_markdown():
    assert parse_status_overrides("# Open Roles\n\n_None._\n") == {}


# --- rendering ------------------------------------------------------------


def test_render_open_roles_keeps_empty_sections_explicit():
    md = render_open_roles([], TODAY)
    assert "## ⏰ Closing this week" in md
    assert "## 📋 Open" in md
    assert "## ✅ Applied" in md
    assert "_Nothing closing in the next 7 days._" in md
    assert "_No other open roles._" in md
    assert "_Nothing marked applied yet._" in md


def test_render_open_roles_places_closing_roles_in_their_own_section():
    roles = [
        _role("cedars:soon", deadline=(TODAY + timedelta(days=3)).isoformat(), employer="PwC"),
        _role("cedars:later", deadline=(TODAY + timedelta(days=40)).isoformat(), employer="CLSA"),
        _role("cedars:done", status="applied", employer="Northwind Capital"),
    ]
    md = render_open_roles(roles, TODAY)
    closing_block = md.split("## 📋 Open")[0]
    assert "PwC" in closing_block
    assert "CLSA" not in closing_block
    assert "3 days left" in closing_block
    assert "Jane Street" in md.split("## ✅ Applied")[1]
