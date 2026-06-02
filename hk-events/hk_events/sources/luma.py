"""Luma (lu.ma) calendar .ics feed adapter.

Luma calendars expose an iCal subscription. The public-facing pattern is a
"Subscribe" / "Add to Calendar" button on each calendar page that yields a
webcal/ICS URL. Calendar slugs live in config/sources.yaml under
ical_feeds.luma. Per-feed failures degrade to [].

TODO: confirm the exact Luma .ics URL shape for a given calendar slug before
relying on it. Observed candidates (NOT yet verified against a live 200):
    https://lu.ma/<slug>/ics
    https://api.lu.ma/ics/get?entity=calendar&id=<calendar-api-id>
The config holds the chosen URL verbatim so this adapter stays dumb — verify the
URL once, paste it into sources.yaml, done. Until then the entries are marked
TODO and skipped by the shared loader.

Real HK Luma calendars seen in the wild (slugs): startupshk, hkweb3.
"""

from __future__ import annotations

from hk_events.schema import Event
from hk_events.sources._ical_common import fetch_feed_group


def fetch_luma_events() -> list[Event]:
    return fetch_feed_group("luma", source="luma")
