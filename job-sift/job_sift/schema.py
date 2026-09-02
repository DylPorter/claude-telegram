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
Lane = Literal["prestige", "floor"]

# Everything that is not a letter or a digit becomes a single space, so
# "IMC Trading." and "IMC  trading" key the same. Deliberately nothing more
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
        logs nothing. So the key stays exact, and `source` stays in it: the HSBC
        pair from issue #1b (`cedars:G2600001` / `linkedin:1000000003`) is
        knowingly left as two rows rather than merged on "HSBC" + a title.

        Within one source the same-prose match is safe enough to act on, because
        a source does not list two genuinely different jobs under an identical
        employer, an identical title AND an identical location — and if it does,
        they are interchangeable enough that applying to one is applying to
        both. Location is in the key precisely to keep "HSBC / Graduate
        Programme / Hong Kong" from swallowing the Singapore one.

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
    # Only classifier.assign_lane ever sets "floor".
    lane: Lane = "prestige"

    @property
    def surface(self) -> bool:
        """True if this listing should be pushed to Telegram.

        Scope is the shared gate: a role that is out of scope is out of scope
        whichever lane looked at it. Above that gate the two lanes disagree on
        what matters — the prestige lane wants a recognisable employer, the
        floor lane deliberately does not care who is hiring.
        """
        if self.scope != "in_scope":
            return False
        return self.lane == "floor" or self.prestige == "prestige"
