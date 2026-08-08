"""Data shapes for events + relevance verdicts.

Mirrors job-sift/schema.py — same dataclass + Literal + dedup_key idiom.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

# Feed sources are clean (iCal); scrape sources are brittle and must degrade
# cleanly per-source the way signal-brief's per-source try/except does.
Source = Literal[
    "meetup",       # per-group Meetup .ics feeds
    "luma",         # lu.ma calendar .ics feeds
    "aitinkerers",  # AI Tinkerers Hong Kong feed
    "cyberport",    # scrape — cyberport.hk events
    "startmeuphk",  # scrape — startmeup.hk events calendar
]

# Relevance bucket. job-sift uses prestige+scope; here the analogue is which
# "room" the event is — the two tiers the operator cares about.
#   founder_ai : funded-startup / AI / founder room (his peer network + signal)
#   sme_buyer  : SME-buyer room (the right rooms to sell into / find clients)
#   drop       : not relevant — precision bias means uncertain → drop
RelevanceTag = Literal["founder_ai", "sme_buyer", "drop"]


@dataclass
class Event:
    source: Source
    external_id: str  # stable per-source id (UID from .ics, or a slug/url hash for scrapes)
    title: str
    url: str  # registration / source page URL — goes in the calendar event body
    start: datetime | None = None  # tz-aware where the feed provides it
    end: datetime | None = None
    location: str | None = None
    description: str | None = None  # body text; classifier uses this to disambiguate the room
    organizer: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """Globally unique key across sources. Prefix source to avoid id collisions
        between platforms that reuse the same UID/hash format."""
        return f"{self.source}:{self.external_id}"

    @property
    def stable_hash(self) -> str:
        """Stable content hash used as the idempotency key for calendar writes.

        Keys off normalized title + start-date + source so the SAME event seen
        on two consecutive daily runs hashes identically and is NOT re-inserted.
        Deliberately excludes url/description (those can drift slightly between
        scrape runs without it being a different event).
        """
        title_norm = re.sub(r"\s+", " ", self.title.strip().lower())
        day = self.start.date().isoformat() if self.start else "nodate"
        basis = f"{self.source}|{title_norm}|{day}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def start_date(self) -> date | None:
        return self.start.date() if self.start else None


@dataclass
class RelevanceResult:
    tag: RelevanceTag
    reason: str  # short human-readable explanation from the LLM

    @property
    def surface(self) -> bool:
        """True if this event should be pushed + calendared (i.e. not dropped)."""
        return self.tag != "drop"
