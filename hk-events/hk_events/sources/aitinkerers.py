"""AI Tinkerers Hong Kong feed adapter.

AI Tinkerers HK lives at https://hong-kong.aitinkerers.org/ . The chapter runs
its RSVPs through Luma (individual events seen at lu.ma/<id>), but the platform
also appears to expose a per-chapter feed.

TODO: confirm whether hong-kong.aitinkerers.org exposes a stable .ics/RSS feed
(the homepage returns 403 to a bare fetch, so this needs a browser/manual check
of the "Subscribe"/"Add to calendar" affordance, or use the chapter's Luma
calendar .ics once that slug is confirmed). Put the confirmed feed URL in
config/sources.yaml under ical_feeds.aitinkerers — until then it's a TODO entry
and skipped.

Events from this source are auto-tagged founder_ai by the classifier
(AUTO_FOUNDER_SOURCES) — it's an AI-builder community by definition.
"""

from __future__ import annotations

from hk_events.schema import Event
from hk_events.sources._ical_common import fetch_feed_group


def fetch_aitinkerers_events() -> list[Event]:
    return fetch_feed_group("aitinkerers", source="aitinkerers", organizer_default="AI Tinkerers Hong Kong")
