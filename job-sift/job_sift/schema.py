"""Data shapes for listings + classifier verdicts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


Source = Literal["cedars", "linkedin", "greenhouse", "lever", "ashby"]
PrestigeVerdict = Literal["prestige", "marginal", "skip"]
ScopeVerdict = Literal["in_scope", "out_of_scope"]


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

    @property
    def surface(self) -> bool:
        """True if this listing should be pushed to Telegram."""
        return self.prestige == "prestige" and self.scope == "in_scope"
