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

import json
from datetime import date

import pytest

from job_sift import profile as profile_mod
from job_sift.classifier import (
    _hard_marginal_check,
    _hard_skip_check,
    _negative_title_no_subject_rescue,
    _route,
    _scope_quick_classify,
    assign_lane,
    floor_reason,
    named_monthly_rate,
    negative_title,
    stamp_tags,
)
from job_sift.tags import derive_role_type
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

# Captured at import time, before the autouse `_profile` fixture below ever
# monkeypatches `profile_mod.load_profile` out from under it. One test class
# needs the REAL loader — see `TestFloorLaneConfigAgainstTheShippedExample`.
_REAL_LOAD_PROFILE = profile_mod.load_profile


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

    def test_the_finance_summer_analyst_that_started_this_is_asked_not_assumed(self):
        """The listing named in the issue.

        It must not come back `in_scope` FOR FREE — but it must not come back
        `out_of_scope` for free either, which is what the fix for it originally
        did. The correct outcome is that nothing decides it here at all.
        """
        listing = _listing("IED Summer Analyst", employer="Morgan Stanley", source="linkedin")
        result, route = _route(listing)
        assert result is None and route in ("full", "scope")

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
    def test_a_negative_title_is_tagged_and_captured_not_deleted(self, title):
        """INVERTED, and this is the Critical the review caught.

        Every one of these carries an admit keyword and is not engineering, and
        every one of them used to be resolved `out_of_scope` here for free —
        a technical-ness judgment recorded as a SCOPE verdict. That was
        survivable while the near-miss digest still printed the rejection. It
        is not survivable now: near-misses are terminated, the seen-set has no
        TTL, so the keyword deleted the role permanently with nothing anywhere
        recording that it had been seen.

        Two of these titles refuted the gate outright. "Graduate Trainee
        Programme" died on `trainee` while `rotational` is IN the accepted
        scope definition and is what `tags` maps "graduate trainee" to. And a
        plain "Data Analyst Intern" died on `analyst` — the same family as the
        worked example the whole redesign was argued from.

        So the quick path passes them through (None = "ask the LLM"), and the
        business function survives as the `function` TAG.
        """
        listing = _listing(title)
        assert _scope_quick_classify(listing) is None, (
            f"{title!r} must be asked, not deleted by a keyword"
        )
        tagged = stamp_tags(listing, ClassifierResult("skip", "in_scope", ""))
        assert tagged.function is not None, "the keyword's information must survive as a tag"
        assert tagged.surface, "and the role must still be captured"

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
        # ...and with the gate removed, the rescue's remaining job is to keep
        # the `function` tag off a genuine engineering title.
        assert stamp_tags(_listing(title), ClassifierResult("skip", "in_scope", "")).function is None

    def test_negative_titles_reach_the_llm_on_the_full_lane_too(self):
        """The other half of the same removal, and the one that costs money.

        This branch used to resolve `out_of_scope` for free and was where most
        of the sift's LLM saving lived. It is gone, so these titles now pay for
        a scope pass — accepted, because batching makes the marginal cost a
        fraction of a call and the alternative is a keyword list silently
        deleting every marketing internship the operator will ever be offered.
        """
        result, route = _route(
            _listing("Business Development Manager", employer="Nobody Ltd", source="cedars")
        )
        assert route == "full"
        assert result is None


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

    def test_removing_the_technical_gate_costs_llm_calls_and_that_is_accepted(self):
        """THE BUDGET NUMBER, RESTATED HONESTLY AFTER THE GATE CAME OUT.

        This test used to assert that the negative-title branch made MORE
        listings free (3/8 -> 6/8 on this corpus). It did — by deleting them.
        Every "newly free" row below was a role resolved `out_of_scope` on a
        keyword, and once near-misses were terminated that saving was being
        paid for in roles the operator would never see.

        So the direction is now reversed on purpose, and asserted rather than
        hidden: only the SENIORITY check resolves for free, because that is the
        one genuine scope signal available without asking. Everything else
        pays. The bound that keeps this affordable is batching — `_BATCH_CHUNK_
        SIZE` listings per CLI call — not a keyword list.
        """
        corpus = [
            # Formerly deleted for free by the technical gate. All of these now
            # reach the LLM, and can therefore reach the board.
            _listing("Business Development Manager", employer="Nobody Ltd"),
            _listing("Sales Executive", employer="Nobody Ltd"),
            _listing("Talent Acquisition Intern", employer="Nobody Ltd"),
            _listing("Marketing Analyst", employer="Nobody Ltd"),
            _listing("Graduate Trainee Programme", employer="Nobody Ltd"),
            # Still free — seniority is a real scope judgment, not a taste one.
            _listing("Senior Software Engineer", employer="Google"),
            # Keyword admits: candidates, never verdicts. Unchanged.
            _listing("Software Engineer Intern", employer="Google"),
            _listing("Data Science Summer Analyst", employer="Google"),
        ]
        routes = [route for _, route in map(_route, corpus)]
        assert routes.count("done") == 1, f"only seniority resolves for free now: {routes}"
        assert all(r != "done" for r in routes[:5]), (
            "a business function in the title must never resolve a scope verdict"
        )

    @pytest.mark.parametrize(
        "title",
        ["Marketing Intern", "Data Analyst Intern", "Graduate Trainee Programme"],
    )
    def test_the_three_titles_the_review_named_reach_the_register(self, title):
        """Executed, end to end through the routing layer the orchestrator uses.

        None of the three may be resolved by a keyword, and each must carry a
        tag that says what the keyword knew.
        """
        listing = _listing(title, employer="Nobody Ltd", source="cedars")
        result, route = _route(listing)
        assert result is None and route == "full", f"{title!r} was decided without asking"
        # And once a real verdict arrives, it is captured and tagged.
        judged = assign_lane(listing, ClassifierResult("skip", "in_scope", "ok"))
        assert judged.surface
        assert (judged.function, judged.role_type) != (None, None)

    def test_graduate_trainee_no_longer_contradicts_its_own_role_type(self):
        """The self-refuting case. `trainee` was a delete keyword while
        `rotational` — what `tags` maps "graduate trainee" TO — is in the
        accepted scope definition, so the full lane could never produce a
        `rotational` row."""
        listing = _listing("Graduate Trainee Programme", employer="Nobody Ltd")
        assert derive_role_type(listing.title) == "rotational"
        assert stamp_tags(listing, ClassifierResult("skip", "in_scope", "")).role_type == "rotational"


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
            # Over-admission found in review: bare "ai" / "technical" /
            # "automation" / "research assistant" in _DEFAULT_TECHNICAL_TERMS
            # let these six through on the word alone, despite none being an
            # engineering-shaped role. All satisfy the engagement criterion
            # (Contract / Freelance / Temporary / Part time) so the technical
            # criterion is the only thing that should be gating them.
            ("Legal Counsel, AI Policy (Contract)", "Hong Kong", "policy, not engineering — bare 'ai' used to admit"),
            ("AI Content Moderator, Contract", "Hong Kong", "content moderation — bare 'ai' used to admit"),
            ("AI Data Annotator - Freelance", "Hong Kong", "data labeling — bare 'ai' used to admit"),
            ("Technical Writer (6-month contract)", "Hong Kong", "writing — bare 'technical' used to admit"),
            (
                "Research Assistant (History Department), Temporary",
                "Hong Kong",
                "non-technical RA — bare 'research assistant' used to admit",
            ),
            ("Office Automation Assistant (Part time)", "Hong Kong", "office admin — bare 'automation' used to admit"),
            # IMPORTANT bug found in review round 2: technical_qualifiers and
            # technical_terms deliberately share vocabulary (engineer*,
            # software, data scien*, technolog*, …), so the SAME word was
            # rescuing a negative title from `negative_title` AND separately
            # satisfying criterion (a)'s own technical_terms check — one word
            # doing double duty as "not really a sales role" and "is
            # technical". None of these is an engineering role; each mentions
            # a technical PRODUCT or DEPARTMENT after naming a non-technical
            # one. Fixed via `_negative_title_no_subject_rescue`: a qualifier
            # only rescues when it is at or before the negative term, not
            # trailing after it as a subject-matter descriptor.
            (
                "Sales Executive, Software Solutions (Contract)",
                "Hong Kong",
                "sells software, doesn't build it — 'software' trails 'sales'",
            ),
            (
                "Business Development Manager, Software (Contract)",
                "Hong Kong",
                "BD role for a software product — 'software' trails 'business develop*'",
            ),
            (
                "Recruitment Consultant, Software Engineering (Contract)",
                "Hong Kong",
                "recruits for eng roles, isn't one — 'engineer*' trails 'recruit*'",
            ),
            (
                "Sales Manager, Technology Products (Part time)",
                "Hong Kong",
                "sells technology, doesn't build it — 'technolog*' trails 'sales'",
            ),
            (
                "Marketing Manager, Data Science Products (Contract)",
                "Hong Kong",
                "markets a data-science product — 'data scien*' trails 'marketing'",
            ),
            (
                "Talent Acquisition Partner, Engineering (Contract)",
                "Hong Kong",
                "recruits engineers, isn't one — 'engineer*' trails 'talent acquisition'",
            ),
            (
                "Risk Technology Analyst (Contract)",
                "Hong Kong",
                "'risk' leads, 'technolog*' sits between it and 'analyst' — not a rescue position",
            ),
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
        """Moved from the digest to the archive, because the digest no longer
        renders listings at all — it is a pointer to the board. The property
        is the same one: lanes partition, so nothing is written twice."""
        listing = _listing("AI Engineer (12-month contract)", employer="Anthropic")
        surfaced = [(listing, assign_lane(listing, ClassifierResult("prestige", "in_scope", "ok")))]
        md = render_vault_archive(surfaced=surfaced, skipped=[], today=TODAY)
        assert md.count(listing.apply_url) == 1


# ---------------------------------------------------------------------------
# Rendering — separate headings, so the prestige signal is not diluted
# ---------------------------------------------------------------------------


def _pair(title, employer, prestige):
    listing = _listing(title, employer=employer, external_id=title[:6])
    return listing, assign_lane(listing, ClassifierResult(prestige, "in_scope", "ok"))


class TestLanesRenderSeparately:
    def test_the_archive_puts_the_floor_lane_under_its_own_header(self):
        prestige = _pair("AI Research Intern", "Anthropic", "prestige")
        floor = _pair("Data Scientist, 6-12 month contract", "Aster Recruiting", "skip")
        assert floor[1].lane == "floor"

        md = render_vault_archive(
            surfaced=[prestige, floor], skipped=[], today=TODAY
        )
        assert md.index("Anthropic") < md.index("floor lane") < md.index("Aster Recruiting")
        assert md.count(floor[0].apply_url) == 1

    def test_the_digest_renders_no_listings_at_all(self):
        """Telegram is a pointer now. Whatever the lanes did, the push is one
        summary bubble — the reading happens on the board."""
        messages = render(
            surfaced=[
                _pair("AI Research Intern", "Anthropic", "prestige"),
                _pair("Data Scientist, 6-12 month contract", "Aster Recruiting", "skip"),
            ],
            skipped=[],
            total_new=2,
            total_processed=2,
            today=TODAY,
        )
        assert len(messages) == 1
        assert "Anthropic" not in messages[0]
        assert "2 new" in messages[0]

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

    def test_a_garbage_lane_falls_back_to_the_weakest_claim(self):
        """An unrecognised lane resolves to "broad", not "prestige".

        A MISSING lane still resolves to "prestige" (the test above) because
        every row written before the field existed genuinely was surfaced by
        the only lane there was. An unrecognised one is a hand-edit or a
        newer vocabulary, and resolving it to "prestige" would upgrade
        garbage into the strongest claim the field can make — a claim about
        the employer that nothing checked."""
        role = OpenRole.from_dict({"dedup_key": "cedars:1", "lane": "banana"})
        assert role.lane == "broad"

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

    def test_prestige_is_no_longer_a_gate(self):
        """The capture inversion: an in-scope role at a no-name employer that
        no lane claims is CAPTURED, and tagged `broad`. It used to be dropped
        at classification time and lost forever, which is the whole reason
        prestige became a tag."""
        assert ClassifierResult("skip", "in_scope", "").surface
        assert ClassifierResult("marginal", "in_scope", "", lane="broad").surface

    def test_scope_is_still_a_gate(self):
        for lane in ("prestige", "floor", "broad"):
            assert not ClassifierResult("skip", "out_of_scope", "", lane=lane).surface

    def test_positional_construction_keeps_its_old_meaning(self):
        """Reading back a dataclass default proves nothing on its own — the old
        version of this test constructed a fresh `ClassifierResult` with no
        lane and asserted the default, which passes for any input.

        What actually needs protecting is that a three-argument construction
        still means what it meant before there were lanes: the caller said
        nothing about a lane, so nothing may claim one on its behalf, and
        `assign_lane` is what decides. So this asserts the pair — the default
        AND that the value is not silently upgraded by the thing that stamps
        lanes.
        """
        bare = ClassifierResult("skip", "out_of_scope", "nope")
        assert bare.lane == "prestige"
        listing = _listing("Data Scientist, 6-12 month contract")
        # Out of scope, so `assign_lane` returns before it can claim a lane.
        assert assign_lane(listing, bare).lane == "prestige"
        # In scope, and the SAME listing does get claimed — proving the
        # assertion above is about the caller's silence, not about a value
        # nothing ever writes.
        assert assign_lane(listing, ClassifierResult("skip", "in_scope", "")).lane == "floor"


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


class TestFloorLaneConfigAgainstTheShippedExample:
    """floor_lane_config() against the REAL committed profile.yaml.example,
    not the fixture in `_profile` above.

    CRITICAL bug found in review: an earlier draft of profile.yaml.example
    shipped a non-empty `floor_lane.locations` (a remote-only example value).
    Since that file doubles as the live fallback profile for any checkout
    with no config/profile.yaml (see PROFILE_EXAMPLE_PATH in
    job_sift/profile.py), a non-empty key there means `floor_lane_config()`
    never reaches `_default_floor_locations()` — the companies.yaml fallback
    the README and profile.py both promise. The floor lane was inert for a
    fresh clone, and specifically inert against every one of issue #2's own
    acceptance examples. Every floor-lane test above passed anyway, because
    the autouse `_profile` fixture monkeypatches `load_profile` with its own
    config that only exists inside the test suite — exactly the kind of test
    proving a feature works against a world that isn't real. This class
    forces the REAL loader (`_REAL_LOAD_PROFILE`, captured at import time,
    before `_profile` ever monkeypatches it) so a regression here fails.
    """

    @pytest.fixture
    def real_profile(self, monkeypatch):
        """Undo the module's autouse fixture for the duration of one test."""
        monkeypatch.setattr(profile_mod, "load_profile", _REAL_LOAD_PROFILE)
        # Force the "fresh clone" case even if this machine happens to have a
        # real config/profile.yaml — the example file is what's being tested.
        monkeypatch.setattr(
            profile_mod, "PROFILE_PATH", profile_mod.PROJECT_ROOT / "config" / "__does_not_exist__.yaml"
        )
        profile_mod.reset_config_cache()
        _REAL_LOAD_PROFILE.cache_clear()
        yield
        profile_mod.reset_config_cache()
        _REAL_LOAD_PROFILE.cache_clear()

    def test_the_shipped_example_is_not_inert(self, real_profile):
        """`cfg.active` alone is too weak: the BROKEN config (an earlier
        draft of profile.yaml.example with a non-empty, remote-only
        `locations`) also had `cfg.active is True` — non-empty locations,
        just the wrong ones, resolved from the shadowed example instead of
        the companies.yaml fallback. Assert the fallback specifically fired
        instead: "hong kong" is in the real committed config/companies.yaml
        `location_allowlist` and was NOT in the broken example's locations,
        so this fails under the broken config and passes under the fixed
        one — verified by execution (restoring the broken `locations:`
        value makes this assertion fail, not just the parametrized five)."""
        cfg = profile_mod.floor_lane_config()
        assert cfg.active, "floor lane is inert under the shipped default config"
        assert cfg.locations, "no locations resolved — the companies.yaml fallback did not fire"
        assert "hong kong" in cfg.locations, (
            "locations did not come from config/companies.yaml's location_allowlist — "
            f"got {cfg.locations!r}, which looks like a shadowing floor_lane.locations "
            "value in profile.yaml.example rather than the fallback"
        )

    @pytest.mark.parametrize(
        "employer, title",
        [
            ("Argyll Scott", "3x AI Platform Support Engineer / 12-month contract / FS / 30-50K P/M"),
            ("GUTolution", "Part time – AI & Bioinformatics"),
            ("ConnectedSolutions", "Junior Automation Engineer (Rolling Contract)"),
            ("Aster Recruiting", "Data Scientist, 6-12 month contract"),
            ("HK Metropolitan University", "Temporary Research Assistant (AI / Data Science)"),
        ],
    )
    def test_every_issue_2_example_admits_under_the_shipped_config(self, real_profile, employer, title):
        """The exact regression: all five of issue #2's own acceptance
        examples, run against config/profile.yaml.example as committed, not
        a fixture standing in for it."""
        cfg = profile_mod.floor_lane_config()
        listing = _listing(title, employer=employer, location="Hong Kong")
        assert floor_reason(listing, cfg) is not None, f"{employer} — {title!r} not admitted by the shipped config"


class TestFloorLaneIsBrandAgnostic:
    """IMPORTANT bug found in review: `_hard_skip_check` / `_hard_marginal_check`
    stamped `scope="out_of_scope"` for domain-wrong and crypto/marginal
    employers — a PRESTIGE opinion about the employer recorded as a SCOPE
    verdict about the role. `assign_lane` only offers a listing to the floor
    lane when `scope == "in_scope"`, so Coinbase, Binance, Hermes and every
    other hard-skip/hard-marginal employer never reached `floor_reason` even
    when the listing itself was a perfectly good technical/contract match —
    directly contradicting issue #2's "regardless of employer brand". Fixed
    in `_employer_gated_result`: scope is now decided by the same free
    `negative_title` guard the full LLM lane uses, not defaulted.
    """

    def test_a_hard_skip_employer_still_reaches_the_floor_lane(self):
        listing = _listing(
            "AI Platform Engineer, Contract", employer="Hermes International", location="Hong Kong"
        )
        assert _hard_skip_check("Hermes International")  # sanity: this employer IS hard-skip
        result, route = _route(listing)
        assert route == "done"
        assert result.prestige == "skip"  # prestige lane verdict is UNCHANGED
        surfaced = assign_lane(listing, result)
        assert surfaced.lane == "floor"
        assert surfaced.surface is True

    @pytest.mark.parametrize("employer", ["Coinbase", "Binance"])
    def test_a_hard_marginal_employer_still_reaches_the_floor_lane(self, employer):
        listing = _listing("Data Engineer (6-month contract)", employer=employer, location="Hong Kong")
        assert _hard_marginal_check(employer)  # sanity: this employer IS hard-marginal
        result, route = _route(listing)
        assert route == "done"
        assert result.prestige == "marginal"  # prestige lane verdict is UNCHANGED
        surfaced = assign_lane(listing, result)
        assert surfaced.lane == "floor"
        assert surfaced.surface is True

    def test_the_prestige_lane_verdict_is_unchanged_for_these_employers(self):
        """The PRESTIGE VERDICT is unchanged for hard-skip/hard-marginal
        employers — that was the original instruction and it still holds.

        What changed underneath it is that the verdict is no longer a gate.
        These listings now reach the board tagged `prestige="skip"`, where a
        reader can filter them out by hand, instead of being deleted at
        capture. So the assertion is on the tag, not on `surface`: the old
        version asserted `surface is False`, which today would be asserting
        that a captured role is thrown away."""
        listing = _listing("AI Platform Engineer, Contract", employer="Hermes International")
        result, _ = _route(listing)
        assert result.prestige == "skip"
        assert ClassifierResult(result.prestige, result.scope, result.reason).lane == "prestige"

    def test_a_hard_skip_employer_with_a_negative_title_is_tagged_not_deleted(self):
        """The floor lane is looser, not blind — and capture is looser still.

        This used to assert the listing was DELETED (`out_of_scope`,
        `surface is False`) on the strength of the word "Sales". It is now
        captured, because a business function is not a scope judgment; what
        the keyword knew survives as `prestige="skip"` and `function="sales"`,
        which is what a reader filters on.

        The property that genuinely belongs to the floor lane is unchanged and
        still asserted: a non-technical title must NOT be rescued into that
        lane just because the employer check ran first.
        """
        listing = _listing("Sales Executive, Luxury Retail", employer="Hermes International", location="Hong Kong")
        result, route = _route(listing)
        assert route == "done"
        assert result.prestige == "skip"
        surfaced = assign_lane(listing, result)
        assert surfaced.lane == "broad", "claimed by neither lane, and honest about it"
        assert surfaced.function == "sales"
        assert surfaced.surface is True


class TestFloorLaneQualifierRescueDoesNotEscapeToTechnical:
    """IMPORTANT bug found in review round 2: `technical_qualifiers` and
    `technical_terms` deliberately share vocabulary (`engineer*`, `software`,
    `data scien*`, `technolog*`, …). Plain `negative_title` let ANY of those
    words rescue a negative title from anywhere in it, and the same word then
    satisfied `floor_reason`'s own technical_terms check two lines later —
    one word doing double duty as "not really a sales role" and "is
    technical". "Sales Executive, Software Solutions" sells software, it
    doesn't build it; "Recruitment Consultant, Software Engineering" recruits
    for eng roles, it isn't one.

    `_negative_title_no_subject_rescue` fixes this for `floor_reason`
    specifically (not `negative_title`, which stays exactly as it was —
    its rescue is safe because an LLM call always follows it). The rule:
    a qualifier only rescues when it is AT OR BEFORE the negative term, not
    trailing after it as a subject-matter descriptor.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "Sales Executive, Software Solutions (Contract)",
            "Business Development Manager, Software (Contract)",
            "Recruitment Consultant, Software Engineering (Contract)",
            "Sales Manager, Technology Products (Part time)",
            "Marketing Manager, Data Science Products (Contract)",
            "Talent Acquisition Partner, Engineering (Contract)",
            "Risk Technology Analyst (Contract)",
        ],
    )
    def test_a_trailing_qualifier_does_not_rescue_for_the_floor_lane(self, title):
        assert _negative_title_no_subject_rescue(title) is not None, (
            f"{title!r} should stay negative for the floor lane — its qualifier trails the "
            "negative term (subject matter), it doesn't lead it (the role itself)"
        )
        assert floor_reason(_listing(title)) is None, title

    @pytest.mark.parametrize(
        "title",
        [
            "Technology Summer Analyst",
            "Engineering Analyst, Summer 2027",
            "Software Analyst Intern",
            "Data Science Analyst",
            "Software Engineer, Trading Systems",
            "Machine Learning Engineer, Finance Platform",
        ],
    )
    def test_a_leading_qualifier_still_rescues_for_the_floor_lane(self, title):
        """Most of the corpus `negative_title` rescues (see
        TestKeywordAdmitIsNowACandidate.test_a_technical_qualifier_rescues_a_negative_title)
        still rescues under the STRICTER floor-lane check — the qualifier is
        the role's own head noun in every one of these, at or before the
        negative term, not a trailing subject-matter mention.

        NOT included here: "Risk Engineering Intern". It is structurally
        identical to "Risk Technology Analyst" — the title review round 2
        explicitly wants REJECTED — negative term leading, qualifier
        trailing, no way for a position-only rule to tell them apart. Left
        as a known, accepted limitation: `negative_title` (unaffected by
        this change) still rescues it for the scope guard, so it still gets
        a fair LLM look via `_route`'s full path; it just doesn't clear the
        floor lane's free, no-LLM bar. A false negative here costs a job the
        operator could have taken via the floor lane specifically — the same
        cost this lane's own docstring already accepts as cheaper than a
        false admit.
        """
        assert _negative_title_no_subject_rescue(title) is None, title

    def test_a_same_structure_title_correctly_stays_rejected(self):
        """The other side of that limitation, stated as its own test: "Risk
        Technology Analyst" — structurally identical to "Risk Engineering
        Intern" — correctly stays rejected, which is what review round 2
        actually required. The rule trades a recoverable floor-lane miss
        (Risk Engineering Intern can still reach the LLM via the full lane)
        for a guaranteed reject on the title that must never free-admit."""
        assert _negative_title_no_subject_rescue("Risk Technology Analyst (Contract)") is not None

    def test_negative_title_itself_is_unaffected(self):
        """The scope guard's rescue (used by `_scope_quick_classify` and
        `_route`'s full-lane check, where escaping it only trades a free
        reject for a paid LLM call) is UNCHANGED — only `floor_reason`'s
        criterion (a) got stricter."""
        title = "Sales Executive, Software Solutions (Contract)"
        assert negative_title(title) is None  # still rescued for the scope guard
        assert _negative_title_no_subject_rescue(title) is not None  # NOT rescued for the floor lane


class TestFloorLaneRecoveredRecall:
    """IMPORTANT bug found in review round 2: removing bare "ai", "technical",
    "automation" and "research assistant" from `_DEFAULT_TECHNICAL_TERMS"
    (the I6 fix) was a real recall cut, not merely a precision fix as an
    earlier version of the comment on that list claimed — it also dropped
    genuine floor-lane targets. Recovered as narrow compounds ("ai
    research*", "ai specialist", "ai consultant", "automation specialist",
    "technical support") plus a domain-gated "research assistant" (see
    `_RESEARCH_ASSISTANT_DOMAIN_HINTS`), none of which reopen the six
    original false positives.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "AI Researcher (Contract)",
            "AI Specialist (Part time)",
            "AI Consultant (Contract)",
            "Automation Specialist (Contract)",
            "Technical Support Officer (Contract)",
            "Research Assistant (AI), Temporary",
            "Research Assistant, Computer Science Dept (Temporary)",
        ],
    )
    def test_genuine_technical_roles_admit_again(self, title):
        assert floor_reason(_listing(title)) is not None, title

    @pytest.mark.parametrize(
        "title",
        [
            "Legal Counsel, AI Policy (Contract)",
            "AI Content Moderator, Contract",
            "AI Data Annotator - Freelance",
            "Technical Writer (6-month contract)",
            "Research Assistant (History Department), Temporary",
            "Office Automation Assistant (Part time)",
        ],
    )
    def test_the_recovered_terms_do_not_reopen_the_original_false_positives(self, title):
        assert floor_reason(_listing(title)) is None, title


# ---------------------------------------------------------------------------
# A classifier outage is not a verdict.
#
# `_batch_llm` used to return a real ClassifierResult — skip / out_of_scope /
# "batch fallback" — for every listing in a chunk whose CLI call timed out,
# exited non-zero, or returned something that was not a JSON array. That is a
# judgement recorded for a call that never happened, and it is the same shape as
# the CEDARS cookie death: one value meaning both "nothing there" and "I could
# not look". These pin the replacement: no verdict is `None`, and `None` is not
# a value any lane, digest or seen-set may treat as an answer.
# ---------------------------------------------------------------------------


class TestClassifierOutageProducesNoVerdict:
    def _listings(self):
        from job_sift.schema import JobListing

        return [
            JobListing(
                source="linkedin",
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

    def _run_with_cli(self, monkeypatch, fake_run):
        from job_sift import classifier

        monkeypatch.setattr(classifier.subprocess, "run", fake_run)
        return classifier.classify_batch(self._listings())

    @staticmethod
    def _timeout(*a, **k):
        import subprocess

        raise subprocess.TimeoutExpired(cmd="claude", timeout=1.0)

    @staticmethod
    def _nonzero(*a, **k):
        import subprocess

        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

    @staticmethod
    def _garbage(*a, **k):
        import subprocess

        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="I'm sorry, I can't help with that.", stderr=""
        )

    @pytest.mark.parametrize("mode", ["_timeout", "_nonzero", "_garbage"])
    def test_every_outage_shape_yields_no_verdict_not_a_rejection(self, monkeypatch, mode):
        results = self._run_with_cli(monkeypatch, getattr(self, mode))
        # Not one of them may be a ClassifierResult: a rejection here is a
        # fabrication, and a "skip/out_of_scope" one is indistinguishable from a
        # real one downstream.
        assert results == [None, None, None]

    def test_a_partial_answer_leaves_the_unanswered_ones_unclassified(self, monkeypatch):
        import subprocess

        from job_sift import classifier
        from job_sift.schema import JobListing

        # All three take the `full` route (non-boosted employers), so they share
        # ONE chunk and one set of indices — otherwise "answered index 0" would
        # mean a different listing in each route's call.
        listings = [
            JobListing(
                source="linkedin",
                external_id=str(i),
                employer="Some Startup Ltd",
                title=t,
                apply_url=f"https://example.com/{i}",
                location="Hong Kong",
            )
            for i, t in enumerate(
                [
                    "Software Engineer (6-month contract)",
                    "Backend Engineer (Contract)",
                    "Data Scientist, Part Time",
                ]
            )
        ]
        assert {classifier._route(l)[1] for l in listings} == {"full"}

        def half(*a, **k):
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    [{"i": 0, "prestige": "prestige", "scope": "in_scope", "reason": "yes"}]
                ),
                stderr="",
            )

        monkeypatch.setattr(classifier.subprocess, "run", half)
        results = classifier.classify_batch(listings)
        assert results[0] is not None and results[0].surface
        # The model was asked about 1 and 2 and did not answer. That is not a no.
        assert results[1] is None and results[2] is None

    def test_the_floor_lane_does_not_rescue_an_unclassified_listing(self, monkeypatch):
        """Explicitly pinned because the floor lane COULD run without an LLM.

        Deliberately not done: an unclassified listing is held out of the
        seen-set for retry, so admitting it here would either double-push it or
        consume it on a lane that never asked the scope question. See
        `classify_batch`'s docstring.
        """
        results = self._run_with_cli(monkeypatch, self._timeout)
        # Listings 1 and 2 are textbook floor-lane matches — technical title,
        # Hong Kong, an explicit contract/part-time shape — and would admit if
        # `floor_reason` were consulted.
        from job_sift.classifier import floor_reason

        assert floor_reason(self._listings()[1]) is not None
        assert floor_reason(self._listings()[2]) is not None
        assert results[1] is None and results[2] is None

    def test_a_heuristic_verdict_still_resolves_during_an_outage(self, monkeypatch):
        """The outage must not swallow the listings that never needed the LLM."""
        from job_sift.schema import JobListing

        from job_sift import classifier

        monkeypatch.setattr(classifier.subprocess, "run", self._timeout)
        # A SENIORITY title, not a sales one. "Sales Executive" used to resolve
        # for free here; it no longer does, because a business function is not
        # a scope verdict. Seniority still is, so it is what this asserts on.
        free = JobListing(
            source="cedars",
            external_id="99",
            employer="Google",
            title="Senior Staff Software Engineer",
            apply_url="https://example.com/99",
        )
        results = classifier.classify_batch([free] + self._listings())
        assert results[0] is not None
        assert results[0].scope == "out_of_scope"
        assert results[1:] == [None, None, None]


# ---------------------------------------------------------------------------
# The example config is copy-pasted. Whatever it shows, an operator will run.
# ---------------------------------------------------------------------------


class TestTheExampleProfileDoesNotSuggestABrokenTermList:
    """`profile.yaml.example` used to show a `technical_terms:` list containing
    bare `ai` and bare `research assistant`. An operator's first move is to
    uncomment that line — and running with exactly it re-admits two of the six
    false positives `_DEFAULT_TECHNICAL_TERMS` spends nine lines explaining were
    removed. The comment is now a warning; this pins the fact behind it.
    """

    _LOC = ("hong kong", "remote, worldwide")
    _WAS_SUGGESTED = ("engineer*", "software", "data scien*", "ai", "research assistant")
    _NOW_SUGGESTED = ("engineer*", "software", "data scien*", "ai research*", "ml")

    def _cfg(self, terms):
        from job_sift.profile import _DEFAULT_ENGAGEMENT_TERMS

        return FloorLaneConfig(
            locations=self._LOC,
            technical_terms=terms,
            engagement_terms=_DEFAULT_ENGAGEMENT_TERMS,
        )

    def _listing(self, title):
        return JobListing(
            source="cedars",
            external_id="1",
            employer="Anon Ltd",
            title=title,
            apply_url="https://example.com/1",
            location="Hong Kong",
        )

    @pytest.mark.parametrize(
        "title",
        ["Legal Counsel, AI Policy (Contract)", "AI Content Moderator, Contract"],
    )
    def test_the_old_suggestion_really_did_admit_them(self, title):
        assert floor_reason(self._listing(title), self._cfg(self._WAS_SUGGESTED)) is not None

    @pytest.mark.parametrize(
        "title",
        ["Legal Counsel, AI Policy (Contract)", "AI Content Moderator, Contract"],
    )
    def test_the_new_suggestion_does_not(self, title):
        assert floor_reason(self._listing(title), self._cfg(self._NOW_SUGGESTED)) is None

    @pytest.mark.parametrize(
        "title", ["AI Researcher (Part time)", "ML Engineer (6-month contract)"]
    )
    def test_and_still_admits_the_genuine_targets(self, title):
        assert floor_reason(self._listing(title), self._cfg(self._NOW_SUGGESTED)) is not None

    def test_the_example_file_no_longer_offers_the_broken_list(self):
        from pathlib import Path

        import job_sift

        text = (Path(job_sift.__file__).resolve().parent.parent
                / "config" / "profile.yaml.example").read_text()
        assert "[engineer*, software, data scien*, ai, research assistant]" not in text
        assert "DO NOT COPY THE LINE BELOW" in text


class TestTheRescueRuleIsOneDirectional:
    """Pins the documented false POSITIVE, not just the false negative.

    `_negative_title_no_subject_rescue` decides on position alone, so reversing
    the word order flips the verdict on the same role. Deliberately not fixed —
    the pre-positional rule admitted both orderings, so this is never a
    regression — but it must be written down and it must not drift silently.
    """

    def _floor(self, title):
        return floor_reason(
            JobListing(
                source="cedars",
                external_id="1",
                employer="Anon Ltd",
                title=title,
                apply_url="https://example.com/1",
                location="Hong Kong",
            )
        )

    @pytest.mark.parametrize(
        "title",
        [
            "Software Sales Executive (Contract)",
            "Technology Sales Manager, Part Time",
            "Engineering Recruitment Consultant (Contract)",
        ],
    )
    def test_a_leading_qualifier_still_admits_a_non_technical_role(self, title):
        assert self._floor(title) is not None, "documented residual — see the docstring"

    @pytest.mark.parametrize(
        "title",
        [
            "Sales Executive, Software Solutions (Contract)",
            "Business Development Manager, Software (Contract)",
        ],
    )
    def test_a_trailing_qualifier_is_correctly_blocked(self, title):
        assert self._floor(title) is None

    def test_the_symmetric_false_negative_is_pinned_too(self):
        """A genuine software title lost to the same offset rule."""
        assert self._floor("Analyst Programmer (Contract)") is None
