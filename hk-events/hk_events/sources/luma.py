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

THE STANDALONE-EVENT GAP IS CLOSED (2026-09-01), just not here. A standalone
Luma event belongs to no calendar, so no .ics feed can ever see it; that is now
`sources/luma_discover.py`, which reads the lu.ma/hong-kong city page. The two
overlap by design, which is what `_uid_canonical_id` below is for.
"""

from __future__ import annotations

import re

from hk_events.schema import Event
from hk_events.sources._ical_common import fetch_feed_group
from hk_events.sources.luma_discover import canonical_id

# Luma emits TWO VEVENT UID shapes on a calendar feed, and the difference matters:
#
#   evt-<api_id>@events.lu.ma    a real Luma-hosted event that happens to be on
#                                this calendar. `<api_id>` is the SAME id the
#                                city page reports, so this is the one that can
#                                collide with `luma_discover`.
#   calev-<row_id>@events.lu.ma  a calendar ROW for something not hosted on Luma
#                                (its DESCRIPTION just points back at the
#                                calendar page). No api_id exists, and no city-
#                                page event can ever be this — so returning None
#                                and falling back to the source-prefixed
#                                `dedup_key` is correct, not a gap.
#
# Both shapes confirmed against the live startupshk feed 2026-09-01: 142 `evt-`
# and 35 `calev-` UIDs.
# CASE-SENSITIVE on purpose. `re.IGNORECASE` here would be worse than inert:
# api_ids are case-sensitive, so matching `EVT-AbC@` would mint the key
# `luma-evt:EVT-AbC`, which can never collide with the city page's
# `luma-evt:evt-AbC` — a dedupe key that silently does not dedupe.
_EVT_UID_RE = re.compile(r"^(evt-[A-Za-z0-9]+)@")


def _uid_canonical_id(uid: str) -> str | None:
    """Recover the cross-source Luma identity from a VEVENT UID, if it has one."""
    m = _EVT_UID_RE.match(uid.strip())
    return canonical_id(m.group(1)) if m else None


def fetch_luma_events() -> list[Event]:
    return fetch_feed_group("luma", source="luma", canonical_id=_uid_canonical_id)
