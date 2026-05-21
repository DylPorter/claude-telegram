"""Source adapters. Every adapter exposes a `fetch() -> list[Item]` function."""

from signal_brief.sources.rss import fetch_rss
from signal_brief.sources.conferences import fetch_conferences
from signal_brief.sources.newsletters import fetch_newsletters
from signal_brief.sources.twitter import fetch_twitter

__all__ = ["fetch_rss", "fetch_conferences", "fetch_newsletters", "fetch_twitter"]
