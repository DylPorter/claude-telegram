"""Luma DISCOVERY adapter — the lu.ma/hong-kong city page.

CLOSES A GAP THE REPO CALLED UNSOLVABLE. `luma.py` subscribes to *calendars*
(`api.lu.ma/ics/get?entity=calendar&id=cal-…`). A Luma event that belongs to no
calendar therefore appears in no feed we hold, and `config/sources.yaml` recorded
that as an architectural dead end needing Playwright. It does not: lu.ma is a
Next.js app, so the city page's props are server-rendered into
`<script id="__NEXT_DATA__">` in the initial HTML, before a line of JS runs. One
GET, one `json.loads`. CodeChella Week — two standalone events, the original
symptom — is exactly this shape.

This is a DIFFERENT source from `luma`, deliberately: separate health record,
separate failure streak, separate seen-set. The city page dying must not look
like the .ics feeds dying.

VERIFIED SHAPE (2026-09-01): 12 upcoming HK events at
`props.pageProps.initialData.data.events[].event`. The walk below does NOT hard-
code that path — it descends the whole tree collecting objects that carry both
`name` and `start_at`, because the brief's own instruction is to treat this shape
as unstable. On the live document that walk finds those 12 and nothing else.

Wrong turns already taken, so nobody repeats them:
  * `lu.ma/hk` is NOT the city page. It is a stale 2023 event whose slug happens
    to be "hk". The city page is `lu.ma/hong-kong`.
  * Every `api.lu.ma/discover/*` JSON endpoint 404s. There is no public discovery
    API to prefer over this.

OVERLAP WITH `luma` IS EXPECTED AND HANDLED. When a host later attaches a
standalone event to a followed calendar, both adapters carry it. See
`Event.identity_key` and `dedupe.collapse_cross_source` — the collision key is
the Luma `evt-…` api_id, written into `raw["canonical_id"]` by both adapters.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from bs4 import BeautifulSoup

from hk_events.errors import SourceFetchError
from hk_events.schema import Event
from hk_events.sources._html_common import _within_horizon, fetch_html, load_page_url

log = logging.getLogger(__name__)

_SOURCE = "luma_discover"
_NEXT_DATA_ID = "__NEXT_DATA__"


def canonical_id(api_id: str) -> str:
    """The cross-source identity for a Luma event, from its `evt-…` api_id.

    Shared with `luma.py`, which recovers the same api_id from its .ics UID
    (`evt-<api_id>@events.lu.ma`). Namespaced so it can never collide with a
    `dedup_key` from another source.
    """
    return f"luma-evt:{api_id}"


def _parse_dt(value) -> datetime | None:
    """Luma emits UTC ISO-8601 with a Z and milliseconds
    ('2026-09-02T11:00:00.000Z'). fromisoformat handles both on 3.11+."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        log.warning("%s: unparseable date %r", _SOURCE, value)
        return None


def _location(obj: dict) -> str | None:
    """Best plain-text address available. Luma hides the venue on some events
    (`geo_address_visibility` != public), in which case there is nothing to show
    and None is the honest answer."""
    geo = obj.get("geo_address_info")
    if isinstance(geo, dict):
        for key in ("full_address", "address", "city_state"):
            val = geo.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    if obj.get("location_type") == "online":
        return "Online"
    return None


def _discovery_container(node, _depth: int = 0):
    """Find the props container that proves this is the CITY DISCOVERY page.

    Without this, host drift fails silently in the most likely way. A hard
    cutover (404, or a page with no `__NEXT_DATA__`) already raises. But lu.ma is
    actively redirecting to luma.com — live, `lu.ma/hong-kong` 301s to
    `luma.com/hong-kong` and 307s again to `luma.com/hongkong` — and a redirect
    that lands on a DIFFERENT valid Next.js page (marketing, consent, a login
    wall) still serves well-formed `__NEXT_DATA__`. Its JSON parses, the event
    walk finds nothing, and the adapter returns `[]`: scored a SUCCESS by
    `source_health`, resetting the failure streak and stamping a `last_success`
    for a page we never actually read. That silent zero is the exact failure this
    whole branch exists to prevent, so the anchor is a hard requirement.

    Two independent signals, either sufficient, because `kind` is the more
    likely of the two to be renamed:
      * `kind` beginning "discover-" alongside a `data` object  (live value:
        "discover-place"), or
      * a `place` object whose api_id is a `discplace-…`.

    Deliberately NOT a fixed path. The anchor answers "is this the right PAGE";
    where the events themselves sit is left to `_iter_event_objects`, which stays
    structural.
    """
    if _depth > 24:
        return None
    if isinstance(node, dict):
        kind = node.get("kind")
        if isinstance(kind, str) and kind.startswith("discover-") and isinstance(node.get("data"), dict):
            return node
        place = node.get("place")
        if isinstance(place, dict) and str(place.get("api_id", "")).startswith("discplace-"):
            return node
        for value in node.values():
            found = _discovery_container(value, _depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _discovery_container(value, _depth + 1)
            if found is not None:
                return found
    return None


def _iter_event_objects(node, _depth: int = 0) -> list[dict]:
    """Walk the whole props tree for objects carrying both `name` and `start_at`.

    Structural, not path-based, on purpose: `__NEXT_DATA__` is an internal build
    artefact and Luma can move it without warning. The depth cap is a cheap guard
    against a self-referential blob, not a real constraint (the events sit ~5
    levels down).
    """
    if _depth > 24:
        return []
    found: list[dict] = []
    if isinstance(node, dict):
        if isinstance(node.get("name"), str) and node.get("start_at"):
            found.append(node)
        for value in node.values():
            found.extend(_iter_event_objects(value, _depth + 1))
    elif isinstance(node, list):
        for value in node:
            found.extend(_iter_event_objects(value, _depth + 1))
    return found


def _event_from_obj(obj: dict) -> Event | None:
    api_id = obj.get("api_id")
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(api_id, str) or not api_id.strip():
        # Without an api_id there is no stable id AND no way to collide with the
        # .ics adapter, so this event would be re-notified every run and
        # double-reported forever. Dropping it is the lesser wrong.
        log.warning("%s: skipping event with no api_id: %r", _SOURCE, name[:60])
        return None
    api_id = api_id.strip()
    # The walk keys off "has a name and a start_at", which is a SHAPE, not a type.
    # Luma hangs plenty of other things off ids in the same tree — calendars
    # (`cal-`), ticket types (`tix-`), discovery places (`discplace-`) — and some
    # carry both fields. Only `evt-` is an event, and only `evt-` can collide with
    # the .ics adapter, whose UIDs are `evt-<api_id>@events.lu.ma` (see
    # luma._uid_canonical_id). Anything else is a phantom that would reach the
    # digest AND the calendar.
    if not api_id.startswith("evt-"):
        log.debug("%s: skipping non-event object %s (%r)", _SOURCE, api_id, name[:60])
        return None

    slug = obj.get("url")
    slug = slug.strip() if isinstance(slug, str) and slug.strip() else ""
    # Luma's canonical short link. Falls back to the api_id path, which also
    # resolves, so `url` is never empty.
    link = f"https://lu.ma/{slug}" if slug else f"https://lu.ma/{api_id}"

    return Event(
        source=_SOURCE,
        external_id=api_id,
        title=name.strip(),
        url=link,
        start=_parse_dt(obj.get("start_at")),
        end=_parse_dt(obj.get("end_at")),
        location=_location(obj),
        # The city page carries no event body — only the listing card. The
        # classifier works from title + location here; the .ics twin, when there
        # is one, carries the description.
        description=None,
        organizer=None,
        raw={
            "api_id": api_id,
            "slug": slug,
            "calendar_api_id": obj.get("calendar_api_id"),
            "canonical_id": canonical_id(api_id),
        },
    )


def _parse_next_data(html: str) -> list[Event]:
    """Extract events from `__NEXT_DATA__`. No horizon filtering — that is
    `fetch_luma_discover_events`' job, using the shared window.

    RAISES if the script tag is missing or its JSON does not parse: both mean the
    page changed shape and we could not look. Returns [] if it parses and simply
    holds no events — a genuinely quiet week in Hong Kong is not a failure.
    """
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id=_NEXT_DATA_ID)
    if tag is None:
        raise SourceFetchError(
            _SOURCE,
            f"no <script id={_NEXT_DATA_ID}> on the page — lu.ma changed shape, "
            "or served a challenge/interstitial instead of the city page",
        )
    text = tag.string or tag.get_text()
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise SourceFetchError(_SOURCE, f"{_NEXT_DATA_ID} did not parse as JSON: {exc}") from exc

    if _discovery_container(data) is None:
        raise SourceFetchError(
            _SOURCE,
            f"{_NEXT_DATA_ID} parsed but carries no discovery-listing container "
            "(no discover-* kind, no discplace- place) — we were almost certainly "
            "redirected to a different lu.ma/luma.com page. Returning zero events "
            "here would be scored a SUCCESS and stamp a last_success we never earned.",
        )

    events: dict[str, Event] = {}
    for obj in _iter_event_objects(data):
        event = _event_from_obj(obj)
        # The tree nests each event under a wrapper that repeats api_id, so the
        # same event can be reached twice. First writer wins.
        if event is not None and event.external_id not in events:
            events[event.external_id] = event

    if not events:
        log.info("%s: %s parsed cleanly but listed no events", _SOURCE, _NEXT_DATA_ID)
    else:
        log.info("%s: parsed %d distinct events", _SOURCE, len(events))
    return list(events.values())


def fetch_luma_discover_events() -> list[Event]:
    """Public entry point. Raises on "could not look"; [] only on a real zero."""
    url = load_page_url("luma_discover", source=_SOURCE)
    html = fetch_html(url, source=_SOURCE)
    events = _parse_next_data(html)
    in_horizon = [e for e in events if _within_horizon(e.start)]
    log.info("%s: %d of %d events are in horizon", _SOURCE, len(in_horizon), len(events))
    return in_horizon
