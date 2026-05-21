"""Common data shapes flowing through the signal-brief pipeline.

Every source normalizes to `Item`. The filter pass ranks items into a `Digest`
with a fixed shape that the renderer turns into chunked Telegram messages and
a daily-note section.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

SourceKind = Literal[
    "rss",
    "newsletter",
    "conference",
    "twitter",
]

# Coarse content domains used for anti-bubble exposure tracking.
# Keep this list short and orthogonal — these are the axes along which we
# track over-representation, not topic tags. Add new ones only when a real
# axis is missing.
Domain = Literal[
    "ai-tech",
    "platform-engineering",
    "startups-funding",
    "research-papers",
    "policy-geopolitics",
    "biology-health",
    "design",
    "philosophy",
    "finance-markets",
    "hardware",
    "career-industry",
    "conference",
    "other",
]


@dataclass
class Item:
    """A single piece of content collected from a source."""

    title: str
    url: str
    source: str  # short label: "hn", "anthropic-blog", "latent-space", etc.
    source_kind: SourceKind
    published_at: datetime | None = None
    excerpt: str = ""  # short preview, ~200-500 chars
    author: str | None = None
    # Optional: coarse domain set by source or by the LLM filter.
    domain: Domain | None = None
    # Optional source-specific metadata (e.g. HN points, conference start date)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.published_at is not None:
            d["published_at"] = self.published_at.isoformat()
        return d


@dataclass
class DigestSection:
    """One logical section of the final digest. Each section becomes its own
    Telegram message bubble.
    """

    title: str  # e.g. "Today's Signal", "Happening This Week", "Bubble Breaker"
    body: str  # markdown, will be rendered to Telegram
    items: list[Item] = field(default_factory=list)  # underlying items for audit trail


@dataclass
class Digest:
    """The final, ranked, chunked output of the filter pass."""

    date: str  # YYYY-MM-DD
    sections: list[DigestSection]
    # The LLM's narrative summary — short, ~1-2 sentences, sets the day's frame.
    headline: str = ""
    # All items that went IN to the filter, for audit-trail reasons.
    all_items: list[Item] = field(default_factory=list)
    # Items the filter explicitly excluded (transparency for anti-bubble checks).
    suppressed: list[Item] = field(default_factory=list)
    # Reasoning trace from the LLM (optional, written to audit trail).
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "headline": self.headline,
            "sections": [
                {
                    "title": s.title,
                    "body": s.body,
                    "items": [i.to_dict() for i in s.items],
                }
                for s in self.sections
            ],
            "all_items": [i.to_dict() for i in self.all_items],
            "suppressed": [i.to_dict() for i in self.suppressed],
            "rationale": self.rationale,
        }
