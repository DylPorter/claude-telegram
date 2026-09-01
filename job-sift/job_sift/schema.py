"""Data shapes for listings + classifier verdicts."""

from __future__ import annotations

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
