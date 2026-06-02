"""Meetup per-group .ics feed adapter.

Meetup exposes a per-group iCal feed at:
    https://www.meetup.com/<group-slug>/events/ical/

Group slugs live in config/sources.yaml under ical_feeds.meetup. These feeds are
clean and preferred over scraping. Per-feed failures degrade to [] (the shared
_ical_common helper logs and skips).

CONFIRMED slug: data-science-andgenai-hk (Data Science & Generative AI Hong Kong)
TODO slug:      vLLM Hong Kong — exact Meetup group slug not yet verified.
"""

from __future__ import annotations

from hk_events.schema import Event
from hk_events.sources._ical_common import fetch_feed_group


def fetch_meetup_events() -> list[Event]:
    return fetch_feed_group("meetup", source="meetup")
