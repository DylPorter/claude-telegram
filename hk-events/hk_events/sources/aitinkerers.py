"""AI Tinkerers Hong Kong adapter — schema.org JSON-LD off the chapter homepage.

WHAT CHANGED (2026-09-01). This module used to be a `fetch_feed_group` stub with
a TODO saying the homepage 403s a bare fetch and no public feed exists, so the
chapter was left unwired and never reached a digest. Both halves of that are now
wrong:

  * the 403 is GONE — `https://hong-kong.aitinkerers.org/` returns 200 to the
    same browser-like User-Agent the .ics adapters already send;
  * no .ics feed is needed. The homepage SERVER-RENDERS nine
    `<script type="application/ld+json">` blocks, and two of them are `ItemList`s
    whose members are full schema.org `Event` objects — name, description,
    startDate, endDate, a nested Place/PostalAddress, and `@id` as the event URL.
    That is a structured feed in all but content-type.

So this is a "scrape" only in the sense that it arrives inside an HTML document.
There are no CSS selectors to rot: one `<script>` type lookup and a `json.loads`.

SHAPE NOTES, from the live document:
  * The same blocks also carry Organization, WebSite, CollectionPage,
    BreadcrumbList, BlogPosting, TechArticle and CreativeWork. Filter on
    `@type == "Event"` and ignore the rest — a talk archive and a testimonial
    wall are not events.
  * `itemListElement` members are `ListItem` WRAPPERS: the Event hangs off
    `item`. Both shapes are accepted below (bare Event, or ListItem→item), so a
    future flattening of the markup does not break us.
  * Two ItemLists overlap — the same event is emitted by both, seven distinct
    events across eleven entries. De-duplicated here on `@id`. That is
    within-source de-duplication of a document that repeats itself, NOT filtering.

PAST EVENTS ARE NOT THE PARSER'S BUSINESS. The list is mostly a history: of the
seven events in the fixture, five have already happened. `_parse_jsonld_events`
returns all of them and `fetch_aitinkerers_events` applies `_within_horizon` —
the same rolling window `parse_ics` uses — so there is exactly one definition of
"too old" in this codebase.

Events from this source are auto-tagged founder_ai by the classifier
(AUTO_FOUNDER_SOURCES) — an AI-builder community by definition. Still true;
re-confirmed with this rewrite.
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

_SOURCE = "aitinkerers"
_ORGANIZER_DEFAULT = "AI Tinkerers Hong Kong"


def _parse_dt(value) -> datetime | None:
    """schema.org startDate/endDate → datetime. These arrive as offset-aware
    ISO-8601 ('2026-09-12T10:00:00+08:00'); anything else is dropped rather than
    guessed at, and a missing start is legal (the horizon filter keeps it)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        log.warning("%s: unparseable date %r", _SOURCE, value)
        return None


def _place_to_location(location) -> str | None:
    """Flatten a schema.org Place (+ nested PostalAddress) to one display string.

    Returns a plain-text address, never a URL — `render` links the event `url`,
    and a street address rendered as a [register] link is a dead link.
    """
    if isinstance(location, str):
        return location.strip() or None
    if not isinstance(location, dict):
        return None
    parts: list[str] = []
    name = location.get("name")
    if isinstance(name, str) and name.strip():
        parts.append(name.strip())
    addr = location.get("address")
    if isinstance(addr, str) and addr.strip():
        parts.append(addr.strip())
    elif isinstance(addr, dict):
        for key in ("streetAddress", "addressLocality", "addressRegion", "addressCountry"):
            val = addr.get(key)
            if isinstance(val, str) and val.strip() and val.strip() not in parts:
                parts.append(val.strip())
    return ", ".join(parts) or None


def _organizer_name(organizer) -> str | None:
    if isinstance(organizer, str):
        return organizer.strip() or None
    if isinstance(organizer, dict):
        name = organizer.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _iter_list_members(doc) -> list[dict]:
    """Yield the candidate objects of an ItemList, unwrapping ListItem members."""
    members = doc.get("itemListElement")
    if not isinstance(members, list):
        return []
    out: list[dict] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        # ListItem wrapper (what the live page emits) — the payload is `item`.
        inner = member.get("item")
        out.append(inner if isinstance(inner, dict) else member)
    return out


def _event_from_jsonld(obj: dict) -> Event | None:
    """One schema.org Event → one hk-events Event. None if it has no identity."""
    name = obj.get("name")
    event_id = obj.get("@id") or obj.get("url")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(event_id, str) or not event_id.strip():
        return None
    event_id = event_id.strip()

    url = obj.get("url")
    if not isinstance(url, str) or not url.strip():
        # `@id` on this site is the page URL plus a '#event' fragment — strip it
        # back to something a human can click.
        url = event_id.split("#", 1)[0]

    description = obj.get("description")
    return Event(
        source=_SOURCE,
        external_id=event_id,
        title=name.strip(),
        url=url.strip(),
        start=_parse_dt(obj.get("startDate")),
        end=_parse_dt(obj.get("endDate")),
        location=_place_to_location(obj.get("location")),
        description=description.strip() if isinstance(description, str) and description.strip() else None,
        organizer=_organizer_name(obj.get("organizer")) or _ORGANIZER_DEFAULT,
        raw={"jsonld_id": event_id},
    )


def _is_chapter_page(docs: list[dict]) -> bool:
    """True if these JSON-LD blocks came from an AI TINKERERS CHAPTER PAGE.

    Sibling of `luma_discover._discovery_container`, and here for the same
    reason: HTTP 200 plus valid JSON-LD is not proof we read the page we meant
    to. A redirect to a marketing page, a consent interstitial, or the
    platform-wide aitinkerers.org landing page all serve well-formed JSON-LD —
    the blocks parse, no `Event` is found, and `[]` gets scored a SUCCESS by
    `source_health`, resetting the failure streak and stamping a `last_success`
    for a page we never actually read.

    There is no observed host drift on this domain today. It is guarded anyway,
    because the whole point of this branch is that "I could not look" and
    "nothing found" must be impossible to confuse — and shipping that guarantee
    on one of two new adapters is exactly the inconsistency that let the sibling
    bot's CEDARS source stay broken for fifty days.

    THE ANCHOR, chosen from what the real page actually emits (9 ld+json blocks:
    Organization x2, WebSite, CollectionPage, BreadcrumbList, ItemList x3):

      * a `CollectionPage` — the chapter listing page's own identity
        (`@id` = ".../#city-page" live), or
      * an `Organization` carrying `parentOrganization` — the CHAPTER org
        (`@id` = ".../#chapter-organization"), as distinct from the platform-wide
        `https://aitinkerers.org/#organization`, which has no parent.

    Two independent signals, either sufficient, so a rename of one does not take
    the source down. Both are EVENT-INDEPENDENT: a chapter with nothing scheduled
    still emits them, so a genuinely quiet page returns `[]` rather than raising.

    Deliberately NOT "at least one ItemList". The three ItemLists live are
    #events, #recent-talks and #testimonials — all three are content the chapter
    accumulates, so a brand-new chapter could legitimately emit none, and a
    generic marketing page could easily emit one. That candidate is both too
    strict and too loose.
    """
    for doc in docs:
        doc_type = doc.get("@type")
        if doc_type == "CollectionPage":
            return True
        if doc_type == "Organization" and doc.get("parentOrganization"):
            return True
    return False


def _parse_jsonld_events(html: str) -> list[Event]:
    """Extract every schema.org Event from the page's JSON-LD blocks.

    NO horizon filtering — see the module docstring. Raises `SourceFetchError`
    if there is no JSON-LD at all, if every block we found failed to parse, or if
    the blocks parse but carry no chapter-page anchor (see `_is_chapter_page`):
    all three mean we did not read the page we think we read, which is "could not
    look", not "nothing on this week".
    """
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.find_all("script", attrs={"type": "application/ld+json"})
    if not blocks:
        raise SourceFetchError(
            _SOURCE,
            "no <script type=\"application/ld+json\"> block on the page — "
            "the homepage changed shape (or served an interstitial)",
        )

    docs: list[dict] = []
    bad = 0
    for block in blocks:
        text = block.string or block.get_text()
        try:
            doc = json.loads(text)
        except (ValueError, TypeError) as exc:
            bad += 1
            log.warning("%s: skipping unparseable ld+json block: %s", _SOURCE, exc)
            continue
        # A block may hold one object, a bare list of them, or a JSON-LD
        # @graph envelope — all three are legal and all three occur in the wild.
        for item in doc if isinstance(doc, list) else [doc]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                docs.extend(g for g in graph if isinstance(g, dict))
            else:
                docs.append(item)
    if bad == len(blocks):
        raise SourceFetchError(
            _SOURCE,
            f"all {len(blocks)} ld+json block(s) failed to parse as JSON",
        )

    if not _is_chapter_page(docs):
        raise SourceFetchError(
            _SOURCE,
            "ld+json parsed but carries no chapter-page anchor (no CollectionPage, "
            "no Organization with a parentOrganization) — this is not the chapter "
            "homepage. Returning zero events here would be scored a SUCCESS and "
            "stamp a last_success we never earned.",
        )

    # An Event can sit at the top level of its own block as easily as inside an
    # ItemList — schema.org does not require the list, and the site could drop it
    # at any time. Descending ONLY into ItemLists would turn that into a silent
    # zero: the blocks parse, we find nothing, and `[]` is scored as a successful
    # "quiet week". So collect both shapes.
    candidates: list[dict] = []
    for doc in docs:
        if doc.get("@type") == "ItemList":
            candidates.extend(_iter_list_members(doc))
        else:
            candidates.append(doc)

    events: dict[str, Event] = {}
    for obj in candidates:
        if obj.get("@type") != "Event":
            # Organization / WebSite / CollectionPage / BreadcrumbList at the top
            # level; BlogPosting / TechArticle / CreativeWork inside the lists.
            continue
        event = _event_from_jsonld(obj)
        # First writer wins: the two overlapping ItemLists carry the same events,
        # and the earlier block has the fuller description.
        if event is not None and event.external_id not in events:
            events[event.external_id] = event

    log.info("%s: parsed %d distinct Event objects from %d ld+json block(s)",
             _SOURCE, len(events), len(blocks))
    return list(events.values())


def fetch_aitinkerers_events() -> list[Event]:
    """Public entry point. Raises on "could not look"; returns [] only on a
    genuinely empty page (see `_html_common` for the full contract)."""
    url = load_page_url("aitinkerers", source=_SOURCE)
    html = fetch_html(url, source=_SOURCE)
    events = _parse_jsonld_events(html)
    in_horizon = [e for e in events if _within_horizon(e.start)]
    log.info("%s: %d of %d events are in horizon", _SOURCE, len(in_horizon), len(events))
    return in_horizon
