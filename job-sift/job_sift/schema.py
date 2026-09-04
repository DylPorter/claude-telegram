"""Data shapes for listings + classifier verdicts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal


Source = Literal["cedars", "linkedin", "greenhouse", "lever", "ashby"]
PrestigeVerdict = Literal["prestige", "marginal", "skip"]
ScopeVerdict = Literal["in_scope", "out_of_scope"]
# Which admission lane surfaced a listing. Two lanes run in parallel:
#   "prestige" — the original strict-brand heuristic (unchanged)
#   "floor"    — brand-agnostic: technical + local/remote + contract/part-time
# A listing carries exactly ONE lane; see classifier.assign_lane for the
# precedence rule that keeps an overlapping listing from appearing twice.
# A THIRD VALUE, "broad", was added when prestige stopped being a GATE and
# became a tag (see `ClassifierResult.surface`). Before that, every in-scope
# listing was either a recognisable brand ("prestige") or a technical
# short-engagement match ("floor") — anything else never reached the register at
# all, so two values covered everything that existed. Now everything in scope is
# captured, so most rows are neither, and calling those "prestige" would be a
# false claim about the employer printed on a board the reader filters by.
Lane = Literal["prestige", "floor", "broad"]

# Everything that is not a letter or a digit becomes a single space, so
# "Northwind Trading." and "Northwind  trading" key the same. Deliberately nothing more
# clever than that — see `JobListing.identity_key`.
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalise(value: str | None) -> str:
    """Casefold + squash non-alphanumerics. The ONLY normalisation we do."""
    return _NON_ALNUM_RE.sub(" ", (value or "").casefold()).strip()


@dataclass
class JobListing:
    source: Source
    external_id: str  # stable dedup key (CEDARS Job ID, LinkedIn job ID, Greenhouse id, etc.)
    employer: str
    title: str
    apply_url: str
    posting_date: date | None = None
    deadline: date | None = None
    location: str | None = None
    description: str | None = None  # body text when source provides one; classifier uses this for ambiguous titles
    raw: dict = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """Globally unique key across sources. Prefix source to avoid collisions
        when two platforms happen to use the same id format."""
        return f"{self.source}:{self.external_id}"

    @property
    def identity_key(self) -> str:
        """Identity of the REAL POSTING, across ids — but never across sources.

        `dedup_key` answers "have I seen this row before"; this answers "is this
        the same job as that other row". The gap between the two is issue #1b: a
        LinkedIn repost gets a NEW job id, so the seen-set (keyed on the id)
        cannot tell, and the register carried both — one of them already closed.

        THE KEY IS SOURCE-SCOPED, ON PURPOSE, AND THAT IS THE DESIGN DECISION
        WORTH READING. hk-events could collapse across sources because both Luma
        adapters carry the same `evt-` api_id, so the merge was an id match.
        Job listings have no such thing: CEDARS keys on its own `G26xxxxx`,
        LinkedIn on its numeric job id, the ATS boards on a board slug + req id,
        and no two of them ever appear in each other's payloads (CEDARS'
        `apply_url` is a CEDARS detail page, LinkedIn's is a linkedin.com job
        view). The only thing they share is prose — employer and title — and
        matching on prose across sources is a guess.

        The cost of guessing wrong is asymmetric and that is what settles it. A
        missed collapse shows one job twice; a wrong merge DROPS a real job and
        logs nothing. So the key stays exact, and `source` stays in it: the Contoso Bank
        pair from issue #1b (`cedars:G2600001` / `linkedin:4400000003`) is
        knowingly left as two rows rather than merged on "Contoso Bank" + a title.

        Within one source the same-prose match is safe enough to act on, because
        a source does not list two genuinely different jobs under an identical
        employer, an identical title AND an identical location — and if it does,
        they are interchangeable enough that applying to one is applying to
        both.

        BE HONEST ABOUT LOCATION: on today's two real sources it discriminates
        nothing. CEDARS hardcodes `location="Hong Kong"` on every row it parses,
        including roles that are plainly elsewhere, and LinkedIn's is parsed off
        the digest card but only reaches a register row when a source re-lists
        it — which, by the premise of the LinkedIn ageing problem, LinkedIn never
        does. So it is currently a constant on one source and usually absent on
        the other. It stays in the key anyway: it costs nothing, it only ever
        makes the key STRICTER (a mismatch means a missed collapse, never a
        wrong merge), and it is the field that starts discriminating the moment
        a source with real per-row locations is added or the CEDARS parser
        learns to read one.

        Employer or title missing → fall back to `dedup_key`, which is
        source-prefixed and therefore collides with nothing. That is the safe
        direction: at worst the posting is listed twice, never zero times.
        """
        employer = normalise(self.employer)
        title = normalise(self.title)
        if not employer or not title:
            return self.dedup_key
        return f"{self.source}|{employer}|{title}|{normalise(self.location)}"


@dataclass
class ClassifierResult:
    prestige: PrestigeVerdict
    scope: ScopeVerdict
    reason: str  # short human-readable explanation from the LLM
    # Defaults to "prestige" so every existing positional construction —
    # ClassifierResult("skip", "out_of_scope", "...") — keeps its old meaning.
    # Only classifier.assign_lane ever sets "floor" or "broad".
    lane: Lane = "prestige"

    # ------------------------------------------------------------------
    # ADVISORY TAGS. None means UNTAGGED and nothing else — never "no", never
    # a default. They are carried into the register and rendered as board
    # facets; not one of them is allowed to decide whether a role is captured.
    # `role_type` is derived from the title in Python (job_sift.tags), the
    # other two come from the same LLM call that already returns `reason`.
    # ------------------------------------------------------------------
    role_type: str | None = None
    industry: str | None = None
    is_technical: bool | None = None
    # The non-technical business function named in the title ("sales",
    # "analyst", "talent acquisition"), or None. THIS FIELD IS THE FORMER
    # TECHNICAL GATE. The same keyword list used to stamp `out_of_scope` and
    # delete the role; it now stamps a tag and the board filters on it. See
    # classifier._route for the full account.
    function: str | None = None

    @property
    def surface(self) -> bool:
        """True if this listing is captured into the register and the board.

        SCOPE IS THE ONLY GATE, and that is the inversion this codebase was
        rebuilt around. It used to also require a recognisable employer (or a
        floor-lane match), so a taste decision was taken at capture time and
        everything it rejected was gone — which is why two work cycles were
        burned arguing about keyword lists that could not tell a research
        internship at a lab from a research associate at an asset manager.

        Scope survives because it is not a taste question: "is this a role a
        student could actually take" has an answer that does not vary by
        reader, and a permanent senior role is not relevant to anyone this runs
        for. Prestige and technical-ness DO vary by reader — the sibling
        deployment's reader wants design and art roles — so they are tags on
        the row, filtered in the UI, where being wrong costs a dropdown and not
        a lost opportunity.
        """
        return self.scope == "in_scope"
