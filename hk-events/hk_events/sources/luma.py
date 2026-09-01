"""Luma (lu.ma) calendar .ics feed adapter.

Luma calendars expose an iCal subscription. The URL shape was VERIFIED
2026-08-09 and the live feeds use it:

    https://api.lu.ma/ics/get?entity=calendar&id=<calendar-api-id>

Find `<calendar-api-id>` by curling the calendar's public page and grepping for
`cal-XXXX`, then confirm `X-WR-CALNAME` matches the intended calendar. The URL
goes into config/sources.yaml verbatim so this adapter stays dumb; entries whose
url is empty or starts with "TODO" are skipped by the shared loader, which is
how an unverified feed is parked without breaking a run.

A single dead feed degrades; EVERY configured feed failing raises
SourceFetchError, because an empty return has to mean "I looked". No usable feed
URL at all raises SourceNotConfiguredError — nobody asked us anything, so the
run is not evidence about this source either way.

Configured calendars (all verified): startupshk, lunatechs, moomeetup,
codechella. hkweb3 is deliberately commented out — see sources.yaml.

KNOWN GAP: standalone Luma events belong to no calendar, so no iCal feed can
ever see them. Catching those needs a discovery-page scrape. See sources.yaml.
"""

from __future__ import annotations

from hk_events.schema import Event
from hk_events.sources._ical_common import fetch_feed_group


def fetch_luma_events() -> list[Event]:
    return fetch_feed_group("luma", source="luma")
