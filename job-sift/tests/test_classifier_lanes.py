"""The two admission lanes, and the scope guard that stopped the first one
admitting finance roles.

Two defects, one code path, so one test module.

**The keyword fallback bypassed the classifier.** `_scope_quick_classify` used
to return `in_scope` for any title containing intern / summer / trainee /
12-month, and `_route` marked that `done` — no LLM call ever ran. So a boosted
employer plus the word "Summer" in the title was enough to be surfaced, with
nothing asking whether the role was technical. Twenty of thirty-five entries in
the live register were finance, BD and sales roles admitted exactly that way.

**The prestige lane was the only lane.** Over 87 digests, 269 listings were
`in_scope` and discarded on employer brand alone — including contract
engineering roles with the monthly rate printed in the title.

Everything here is pure. No test in this module spawns the classifier CLI:
these are the decisions made BEFORE and AFTER the LLM, and the point of both
fixes is how much they settle without asking it.
"""

from __future__ import annotations

from datetime import date

import pytest

from job_sift import profile as profile_mod
from job_sift.classifier import (
    _route,
    _scope_quick_classify,
    assign_lane,
    floor_reason,
    named_monthly_rate,
    negative_title,
)
from job_sift.open_roles import (
    OpenRole,
    apply_status_overrides,
    in_lane,
    parse_status_overrides,
    upsert_roles,
)
from job_sift.profile import FloorLaneConfig, ScopeGuardConfig
from job_sift.render import render, render_open_roles, render_vault_archive
from job_sift.schema import ClassifierResult, JobListing

TODAY = date(2026, 9, 1)


def _listing(
    title: str,
    *,
    employer: str = "Some Company Limited",
    source: str = "cedars",
    location: str | None = "Hong Kong",
    description: str | None = None,
    external_id: str = "1",
    deadline: date | None = None,
) -> JobListing:
    return JobListing(
        source=source,
        external_id=external_id,
        employer=employer,
        title=title,
        apply_url=f"https://example.test/{external_id}",
        location=location,
        description=description,
        deadline=deadline,
    )


@pytest.fixture(autouse=True)
def _profile(monkeypatch):
    """Pin the profile so these tests describe the MECHANISM, not the operator.

    The term lists are configuration; a test that asserted against whatever
    `config/profile.yaml` happens to hold would be asserting about a gitignored
    file. So the profile is fixed here to a small, explicit set — and the
    caches every config reader memoizes through are cleared on the way in and
    on the way out, because `lru_cache` outlives a monkeypatch.
    """
    profile_mod.reset_config_cache()
    monkeypatch.setattr(
        profile_mod,
        "load_profile",
        lambda: {
            "identity": "a test candidate",
            "seeking": "test roles",
            "floor_lane": {"enabled": True, "locations": ["hong kong", "global remote"]},
        },
    )
    profile_mod.reset_config_cache()
    yield
    profile_mod.reset_config_cache()


# ---------------------------------------------------------------------------
# #1a — the keyword match is a candidate, not an admission
# ---------------------------------------------------------------------------


class TestKeywordAdmitIsNowACandidate:
    def test_intern_keyword_alone_no_longer_resolves_without_an_llm_call(self):
        """The core of the defect: "summer" in the title used to BE the verdict.

        `_scope_quick_classify` returning None is the whole fix — None means
        "ask the LLM", and the routing layer turns that into a real scope pass.
        """
        assert _scope_quick_classify(_listing("Software Engineer Intern")) is None
        assert _scope_quick_classify(_listing("Summer Technology Programme")) is None

    def test_a_boosted_employer_with_a_keyword_title_is_routed_to_the_llm(self):
        """End to end through `_route`, which is what `classify_batch` uses.

        Google is on the prestige boost list, so this listing takes the
        auto-prestige branch — the exact branch that used to short-circuit to
        `done` on the word "Intern".
        """
        result, route = _route(
            _listing("Software Engineering Intern, Summer 2027", employer="Google")
        )
        assert route == "scope", "a keyword title must still pay for a scope classification"
        assert result is None

    def test_the_finance_summer_analyst_that_started_this_is_not_admitted(self):
        """The listing named in the issue. It must not come back `in_scope`."""
        listing = _listing("IED Summer Analyst", employer="Morgan Stanley", source="linkedin")
        result, route = _route(listing)
        assert not (result is not None and result.scope == "in_scope")

    @pytest.mark.parametrize(
        "title",
        [
            "Strategy Intern",
            "Business Development Intern",
            "Business Developer, Summer Programme",
            "AI Sales Intern",
            "Talent Acquisition Intern",
            "Equity Trading Summer Analyst",
            "Graduate Trainee Programme",
            "Asset Management Summer Analyst",
            "Firm Risk Management Intern",
            "Transaction Finance Intern",
            "Markets Research Summer Analyst",
        ],
    )
    def test_negative_titles_are_never_admitted_by_the_quick_path(self, title):
        """Every one of these carries an admit keyword AND is not engineering.

        Drawn from the register entries the issue counted: Morgan Stanley IED /
        GCM / Firm Risk, HSBC CIB, UBS Asset Management, Societe Generale
        trainees, Blackstone Transaction Finance, JPMorgan Markets Research,
        BBVA Equity Trading, JD.COM Talent Acquisition, ByteDance Strategy and
        AI Sales.
        """
        verdict = _scope_quick_classify(_listing(title))
        assert verdict is not None, f"{title!r} should be settled, not passed through"
        assert verdict.scope == "out_of_scope"
        assert not verdict.surface

    @pytest.mark.parametrize(
        "title",
        [
            "Technology Summer Analyst",
            "Engineering Analyst, Summer 2027",
            "Software Analyst Intern",
            "Data Science Analyst",
            "Software Engineer, Trading Systems",
            "Risk Engineering Intern",
            "Machine Learning Engineer, Finance Platform",
        ],
    )
    def test_a_technical_qualifier_rescues_a_negative_title(self, title):
        """The carve-out, applied to every negative term rather than "analyst".

        Without it a substring match on "trading" / "risk" / "finance" would
        throw away real engineering roles — which would be the same bug as the
        one being fixed, pointed the other way.
        """
        assert negative_title(title) is None
        assert _scope_quick_classify(_listing(title)) is None, "should go to the LLM, not be rejected"

    def test_negative_titles_are_rejected_on_the_full_lane_too(self):
        """Scope is not a property of prestige.

        A no-name employer's sales role is out of scope for the same reason a
        famous one's is, and paying an LLM to be told so is waste. This is also
        where most of the cost given up by demoting the keyword admits is
        recovered — the full lane is the busy one.
        """
        result, route = _route(
            _listing("Business Development Manager", employer="Nobody Ltd", source="cedars")
        )
        assert route == "done"
        assert result is not None and result.scope == "out_of_scope"


class TestQuickPathStillEarnsItsPlace:
    def test_seniority_keywords_still_resolve_for_free(self):
        """The cost win the quick path exists for. Untouched by the fix.

        Only the ADMIT direction became a candidate; rejecting for free is
        safe, because being wrong costs one missed listing rather than a false
        entry in the register.
        """
        verdict = _scope_quick_classify(_listing("Senior Staff Engineer"))
        assert verdict is not None
        assert verdict.scope == "out_of_scope"

        result, route = _route(_listing("Director of Engineering", employer="Google"))
        assert route == "done"
        assert result is not None and result.scope == "out_of_scope"

    def test_the_fix_does_not_turn_every_listing_into_an_llm_call(self):
        """The explicit budget guard on this change.

        Anthropic alone lists ~389 roles, so "just classify everything" is not
        a free option. No real classifier_log.jsonl exists in a fresh
        checkout to replay, so this is measured on the corpus below instead:
        on it the free-resolution rate actually goes DOWN (59% -> 45%, see
        README "Two admission lanes") because several previously-free admits
        (bare "Software Engineer Intern" titles with no negative term to
        reject on) now fall through to the LLM. The new free rejections claw
        some of that back but do not fully offset it on this small, intern-
        heavy sample. This test only pins the floor on how bad that gets —
        not every listing may end up paid — pending a real log to measure the
        production mix against.
        """
        corpus = [
            # newly free — non-technical functions, previously an LLM call each
            _listing("Business Development Manager", employer="Nobody Ltd"),
            _listing("Sales Executive", employer="Nobody Ltd"),
            _listing("Talent Acquisition Intern", employer="Nobody Ltd"),
            _listing("Marketing Analyst", employer="Nobody Ltd"),
            _listing("Graduate Trainee Programme", employer="Nobody Ltd"),
            # still free — seniority, unchanged
            _listing("Senior Software Engineer", employer="Google"),
            # newly paid — the demoted keyword admits
            _listing("Software Engineer Intern", employer="Google"),
            _listing("Data Science Summer Analyst", employer="Google"),
        ]
        routes = [route for _, route in map(_route, corpus)]
        free = routes.count("done")
        assert free >= len(corpus) - 3, f"too many listings routed to an LLM: {routes}"


# ---------------------------------------------------------------------------
# #2 — the floor lane
# ---------------------------------------------------------------------------


class TestNamedMonthlyRate:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("3x AI Platform Support Engineer / 12-month contract / FS / 30-50K P/M", "30-50K P/M"),
            ("Research Assistant HK$25,000/month", "HK$25,000/month"),
            ("Data Engineer, 25,000 per month", "25,000 per month"),
        ],
    )
    def test_a_rate_in_the_title_is_detected(self, text, expected):
        assert named_monthly_rate(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Data Scientist",
            "Interviews start at 3pm",  # a clock, not a rate
            "12-month contract",  # a duration, not a rate
            "8 monthly reviews",
        ],
    )
    def test_non_rates_are_not_mistaken_for_one(self, text):
        assert named_monthly_rate(text) is None


class TestFloorLaneAdmission:
    @pytest.mark.parametrize(
        "employer, title",
        [
            ("Argyll Scott", "3x AI Platform Support Engineer / 12-month contract / FS / 30-50K P/M"),
            ("GUTolution", "Part time – AI & Bioinformatics"),
            ("ConnectedSolutions", "Junior Automation Engineer (Rolling Contract)"),
            ("ConnectedSolutions", "AI Engineers (FS Industry, Contract)"),
            ("Aster Recruiting", "Data Scientist, 6-12 month contract"),
            ("HK Metropolitan University", "Temporary Research Assistant (AI / Data Science)"),
        ],
    )
    def test_the_discarded_listings_from_the_issue_are_admitted(self, employer, title):
        """Every named example, including the two posted by staffing firms.

        A recruiter posting the role was previously an automatic skip; the lane
        asks about the work, not about who typed it in.
        """
        assert floor_reason(_listing(title, employer=employer)) is not None

    def test_a_rate_in_the_title_admits_on_its_own(self):
        """The rate is the strongest positive signal the lane has.

        Stated as a deliberate looseness: a permanent role CAN quote a monthly
        salary, so this will occasionally over-admit. It costs one line under a
        clearly-labelled heading, and the prestige lane is untouched.
        """
        assert floor_reason(_listing("AI Platform Engineer, 30-50K P/M")) is not None

    @pytest.mark.parametrize(
        "title, location, why",
        [
            ("Software Engineer", "Hong Kong", "permanent — no flexible-engagement signal"),
            ("Sales Executive (Contract)", "Hong Kong", "not technical"),
            ("Business Development Intern", "Hong Kong", "negative title disqualifies outright"),
            ("Data Scientist (6-month contract)", "London, United Kingdom", "not reachable"),
            ("Office Administrator (Part time)", "Hong Kong", "not technical"),
        ],
    )
    def test_the_lane_is_looser_but_not_open(self, title, location, why):
        assert floor_reason(_listing(title, location=location)) is None, why

    def test_an_unstated_location_passes(self):
        """Matching the convention the ATS adapters already use upstream: an
        unstated location is a question for the human, not a rejection."""
        assert floor_reason(_listing("Data Scientist (Contract)", location=None)) is not None

    def test_the_body_can_supply_the_engagement_shape(self):
        """Titles like "AI & Bioinformatics" leave the shape to the body."""
        assert floor_reason(_listing("Bioinformatics Associate", description=None)) is None
        assert (
            floor_reason(
                _listing(
                    "Bioinformatics Associate",
                    description="A part-time position, 3 days a week.",
                )
            )
            is not None
        )

    def test_the_lane_is_inert_with_no_configured_geography(self, monkeypatch):
        """The fail-safe. A floor lane with no geography is a firehose, so an
        operator who configures nothing keeps the old prestige-only digest."""
        cfg = FloorLaneConfig(enabled=True, locations=())
        assert not cfg.active
        assert floor_reason(_listing("Data Scientist (Contract)"), cfg) is None

    def test_a_disabled_lane_admits_nothing(self):
        cfg = FloorLaneConfig(enabled=False, locations=("hong kong",))
        assert floor_reason(_listing("Data Scientist (Contract)"), cfg) is None


class TestLaneOverlapResolution:
    """A prestige employer offering a contract role qualifies for both lanes.

    The rule is PRESTIGE WINS, implemented by making `lane` a single value
    assigned by precedence rather than a set of tags — so "appears once" is a
    property of the data, not something each renderer has to remember.
    """

    def test_a_prestige_verdict_stays_in_the_prestige_lane(self):
        listing = _listing("AI Engineer (12-month contract)", employer="Anthropic")
        out = assign_lane(listing, ClassifierResult("prestige", "in_scope", "ok"))
        assert out.lane == "prestige"
        assert out.surface

    def test_a_non_prestige_verdict_is_offered_to_the_floor_lane(self):
        listing = _listing("Data Scientist, 6-12 month contract", employer="Aster Recruiting")
        out = assign_lane(listing, ClassifierResult("skip", "in_scope", "no-name employer"))
        assert out.lane == "floor"
        assert out.surface, "previously discarded on prestige grounds"

    def test_out_of_scope_is_out_of_scope_in_both_lanes(self):
        listing = _listing("Senior Data Scientist (Contract)", employer="Aster Recruiting")
        out = assign_lane(listing, ClassifierResult("skip", "out_of_scope", "senior"))
        assert out.lane == "prestige"
        assert not out.surface

    def test_assign_lane_is_idempotent(self):
        listing = _listing("Data Scientist, 6-12 month contract")
        once = assign_lane(listing, ClassifierResult("skip", "in_scope", "r"))
        twice = assign_lane(listing, once)
        assert twice == once

    def test_a_listing_appears_exactly_once_across_the_rendered_lanes(self):
        listing = _listing("AI Engineer (12-month contract)", employer="Anthropic")
        surfaced = [(listing, assign_lane(listing, ClassifierResult("prestige", "in_scope", "ok")))]
        blob = "\n".join(
            render(
                surfaced=surfaced,
                skipped=[],
                total_new=1,
                total_processed=1,
                today=TODAY,
            )
        )
        assert blob.count(listing.apply_url) == 1


# ---------------------------------------------------------------------------
# Rendering — separate headings, so the prestige signal is not diluted
# ---------------------------------------------------------------------------


def _pair(title, employer, prestige):
    listing = _listing(title, employer=employer, external_id=title[:6])
    return listing, assign_lane(listing, ClassifierResult(prestige, "in_scope", "ok"))


class TestLanesRenderSeparately:
    def test_the_digest_puts_the_floor_lane_under_its_own_header(self):
        prestige = _pair("AI Research Intern", "Anthropic", "prestige")
        floor = _pair("Data Scientist, 6-12 month contract", "Aster Recruiting", "skip")
        assert floor[1].lane == "floor"

        messages = render(
            surfaced=[prestige, floor],
            skipped=[],
            total_new=2,
            total_processed=2,
            today=TODAY,
        )
        blob = "\n".join(messages)
        header_idx = next(i for i, m in enumerate(messages) if "Floor lane" in m)
        prestige_idx = next(i for i, m in enumerate(messages) if "Anthropic" in m)
        floor_idx = next(i for i, m in enumerate(messages) if "Aster Recruiting" in m)
        assert prestige_idx < header_idx < floor_idx, "the floor header must separate the lanes"
        assert blob.count(floor[0].apply_url) == 1

    def test_no_floor_header_when_nothing_took_that_lane(self):
        """An empty lane must not cost a bubble — the digest is chunked and
        every bubble is a notification."""
        messages = render(
            surfaced=[_pair("AI Research Intern", "Anthropic", "prestige")],
            skipped=[],
            total_new=1,
            total_processed=1,
            today=TODAY,
        )
        assert not any("Floor lane" in m for m in messages)

    def test_the_archive_splits_the_lanes_and_says_none_explicitly(self):
        md = render_vault_archive(
            surfaced=[_pair("AI Research Intern", "Anthropic", "prestige")],
            skipped=[],
            today=TODAY,
        )
        assert "## Surfaced — prestige lane" in md
        assert "## Surfaced — floor lane" in md
        assert md.index("prestige lane") < md.index("floor lane")

    def test_the_register_splits_the_lanes(self):
        roles = [
            OpenRole(
                dedup_key="cedars:1", source="cedars", employer="Anthropic", title="AI Intern",
                apply_url="u1", deadline=None, first_seen="2026-09-01", last_seen="2026-09-01",
                reason="r", status="open", lane="prestige",
            ),
            OpenRole(
                dedup_key="cedars:2", source="cedars", employer="Aster Recruiting",
                title="Data Scientist, 6-12 month contract", apply_url="u2", deadline=None,
                first_seen="2026-09-01", last_seen="2026-09-01", reason="r", status="open",
                lane="floor",
            ),
        ]
        md = render_open_roles(roles, TODAY)
        assert "## 📋 Open — prestige lane" in md
        assert "## 🧱 Open — floor lane" in md
        prestige_block = md.split("## 🧱 Open — floor lane")[0]
        floor_block = md.split("## 🧱 Open — floor lane")[1]
        assert "Anthropic" in prestige_block and "Aster Recruiting" not in prestige_block
        assert "Aster Recruiting" in floor_block
        assert md.count("u2") == 1, "a role must be rendered under exactly one heading"

    def test_empty_lane_sections_stay_explicit(self):
        md = render_open_roles([], TODAY)
        assert "_No other open roles._" in md
        assert "_No open floor-lane roles._" in md


class TestStatusStickinessAcrossTheLaneChange:
    def test_the_status_marker_shape_is_unchanged(self):
        """The hand-editable marker must keep working across this change — it
        is the operator's only interface to the register."""
        role = OpenRole(
            dedup_key="cedars:123", source="cedars", employer="Aster Recruiting",
            title="Data Scientist, 6-12 month contract", apply_url="u", deadline=None,
            first_seen="2026-09-01", last_seen="2026-09-01", reason="r", status="open",
            lane="floor",
        )
        md = render_open_roles([role], TODAY)
        assert "<!-- status:open cedars:123 -->" in md
        assert parse_status_overrides(md) == {}

        edited = md.replace("status:open cedars:123", "status:applied cedars:123")
        assert parse_status_overrides(edited) == {"cedars:123": "applied"}

    def test_applied_survives_a_role_moving_between_lanes(self):
        """The lane is the classifier's opinion and can change between runs;
        `applied` is the operator's decision and cannot. Since `dedup_key` does
        not mention the lane, the record — and the mark — is the same one."""
        existing = [
            OpenRole(
                dedup_key="cedars:9", source="cedars", employer="Aster Recruiting",
                title="Data Scientist", apply_url="u", deadline=None,
                first_seen="2026-08-01", last_seen="2026-08-30", reason="r",
                status="applied", lane="floor",
            )
        ]
        listing = _listing("Data Scientist", employer="Aster Recruiting", external_id="9")
        merged = upsert_roles(existing, [(listing, "now prestige", "prestige")], TODAY)

        assert len(merged) == 1, "the lane must not fork the record"
        assert merged[0].lane == "prestige", "the new lane is adopted"
        assert merged[0].status == "applied", "the operator's mark survives"
        assert merged[0].first_seen == "2026-08-01"

    def test_a_dismissed_floor_role_is_not_resurfaced(self):
        roles = apply_status_overrides(
            [
                OpenRole(
                    dedup_key="cedars:7", source="cedars", employer="X", title="AI Engineer",
                    apply_url="u", deadline=None, first_seen="2026-08-01",
                    last_seen="2026-08-01", reason="r", status="open", lane="floor",
                )
            ],
            {"cedars:7": "dismissed"},
        )
        assert roles[0].status == "dismissed"
        assert in_lane(roles, "floor")[0].status == "dismissed"


class TestRegisterBackCompat:
    def test_a_pre_lane_record_loads_into_the_prestige_lane(self):
        """State written before the floor lane existed has no `lane` key. It
        must not fail to load, and it must not silently become floor-lane."""
        role = OpenRole.from_dict(
            {
                "dedup_key": "cedars:1", "source": "cedars", "employer": "Anthropic",
                "title": "AI Intern", "apply_url": "u", "deadline": None,
                "first_seen": "2026-08-01", "last_seen": "2026-08-01", "reason": "r",
                "status": "open",
            }
        )
        assert role.lane == "prestige"

    def test_a_garbage_lane_falls_back_rather_than_propagating(self):
        role = OpenRole.from_dict({"dedup_key": "cedars:1", "lane": "banana"})
        assert role.lane == "prestige"

    def test_upsert_still_accepts_two_tuples(self):
        """The lane is optional: a caller that does not know about lanes gets
        the meaning it had before there were any."""
        merged = upsert_roles([], [(_listing("AI Intern"), "why")], TODAY)
        assert merged[0].lane == "prestige"


class TestSurfaceSemantics:
    def test_scope_is_the_shared_gate(self):
        assert not ClassifierResult("prestige", "out_of_scope", "", lane="floor").surface
        assert not ClassifierResult("skip", "out_of_scope", "", lane="floor").surface

    def test_the_floor_lane_surfaces_without_prestige(self):
        assert ClassifierResult("skip", "in_scope", "", lane="floor").surface
        assert not ClassifierResult("skip", "in_scope", "").surface

    def test_positional_construction_keeps_its_old_meaning(self):
        assert ClassifierResult("skip", "out_of_scope", "nope").lane == "prestige"


class TestConfigLivesInTheProfile:
    """The mechanism is in `classifier.py`; who it is for is in the profile.

    These assert the seam rather than the values — the point is that an
    operator retunes the sift by editing YAML, not the matcher.
    """

    def test_negative_titles_come_from_config(self):
        cfg = ScopeGuardConfig(negative_titles=("underwater basket weaving",), technical_qualifiers=())
        assert negative_title("Underwater Basket Weaving Intern", cfg) == "underwater basket weaving"
        assert negative_title("Sales Intern", cfg) is None

    def test_qualifiers_come_from_config(self):
        cfg = ScopeGuardConfig(negative_titles=("analyst",), technical_qualifiers=("quantum",))
        assert negative_title("Summer Analyst", cfg) == "analyst"
        assert negative_title("Quantum Analyst", cfg) is None

    def test_floor_terms_come_from_config(self):
        cfg = FloorLaneConfig(
            enabled=True,
            locations=("atlantis",),
            technical_terms=("submersible",),
            engagement_terms=("seasonal",),
        )
        assert floor_reason(_listing("Seasonal Submersible Pilot", location="Atlantis"), cfg)
        assert floor_reason(_listing("Seasonal Submersible Pilot", location="Hong Kong"), cfg) is None


class TestTermMatchingIsWordBounded:
    """Plain substring matching was not good enough once the lists grew short
    tokens: "ai" hits "aid", "Retail" and "Maintenance"; "ml" hits "HTML"."""

    @pytest.mark.parametrize(
        "title",
        ["Retail Assistant (Part time)", "Maintenance Technician (Contract)", "First Aid Trainer (Part time)"],
    )
    def test_short_technical_tokens_do_not_match_inside_words(self, title):
        assert floor_reason(_listing(title)) is None

    def test_a_prefix_star_matches_inflections(self):
        cfg = ScopeGuardConfig(negative_titles=("business develop*",), technical_qualifiers=())
        assert negative_title("Business Development Intern", cfg) == "business develop*"
        assert negative_title("Business Developer", cfg) == "business develop*"
        assert negative_title("Business Analyst", cfg) is None
