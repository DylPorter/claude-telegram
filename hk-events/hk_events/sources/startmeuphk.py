"""StartmeupHK events-calendar scraper.

Source page: https://www.startmeup.hk/upcoming-events/events-calendar/ — InvestHK's
HK startup-ecosystem events calendar. No clean public feed, so this is a brittle
HTML scraper that MUST degrade cleanly — but degrading means raising
`SourceFetchError`, NEVER returning `[]`. An empty list from here is a claim
that the page was read and held nothing.

**v0 STATUS: stub** — returns hardcoded sample events for pipeline testing.
Gated by HK_EVENTS_STUB, same pattern as cyberport + job-sift's cedars.

TODO: inspect the live calendar markup. StartmeupHK runs on WordPress; the
calendar is likely "The Events Calendar" plugin, which often exposes an iCal
export (e.g. a /?ical=1 or /events/?ical=1 URL) — if confirmed, this source
should MOVE to the clean iCal tier (add it to config/sources.yaml
ical_feeds.luma-or-new-group and drop the scraper). Verify before relying on it.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

from hk_events.errors import SourceFetchError
from hk_events.schema import Event

log = logging.getLogger(__name__)

_EVENTS_URL = "https://www.startmeup.hk/upcoming-events/events-calendar/"
# TODO: probe for The-Events-Calendar iCal export, e.g.:
#   https://www.startmeup.hk/upcoming-events/events-calendar/?ical=1
# If it returns text/calendar, prefer it over this scraper.
_TIMEOUT = 25.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hk-events/0.1"}


def _stub_events() -> list[Event]:
    now = datetime.now(timezone.utc)
    return [
        Event(
            source="startmeuphk",
            external_id="smuhk-stub-sme-ai",
            title="AI for SMEs: Practical Digital Transformation Workshop",
            url="https://www.startmeup.hk/upcoming-events/stub-sme-ai",
            start=now + timedelta(days=7, hours=14),
            end=now + timedelta(days=7, hours=17),
            location="Central, Hong Kong",
            description="For HK SME owners. How to adopt AI tools in your business. Non-technical.",
            organizer="StartmeupHK / InvestHK",
        ),
        Event(
            source="startmeuphk",
            external_id="smuhk-stub-fintech-festival",
            title="Hong Kong FinTech Founders Networking Mixer",
            url="https://www.startmeup.hk/upcoming-events/stub-fintech",
            start=now + timedelta(days=12, hours=18, minutes=30),
            location="Wan Chai, Hong Kong",
            description="Founders and investors in fintech. Pitch + network.",
            organizer="StartmeupHK",
        ),
    ]


def _parse_events_html(html: str) -> list[Event]:
    """TODO: PLACEHOLDER selectors — validate against the live DOM before use."""
    soup = BeautifulSoup(html, "lxml")
    events: list[Event] = []
    for card in soup.select(".tribe-events-calendar-list__event, .event-item, article"):
        title_el = card.select_one(".tribe-events-calendar-list__event-title, h3 a, h2 a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "") if title_el.name == "a" else ""
        ext_id = "smuhk-" + re.sub(r"[^a-z0-9]+", "-", title.lower())[:60]
        events.append(
            Event(
                source="startmeuphk",
                external_id=ext_id,
                title=title,
                url=href,
                start=None,  # TODO: parse date once selectors confirmed
                location="Hong Kong",
                organizer="StartmeupHK",
            )
        )
    log.info("startmeuphk: parsed %d events", len(events))
    return events


def fetch_startmeuphk_events() -> list[Event]:
    """Public entry point.

    RAISES `SourceFetchError` when it could not look; returns `[]` only when it
    DID look and the page held no events. This adapter is commented out of
    `orchestrator._source_tasks`, and the comment there advertises re-enabling
    it as a one-line change — so it has to already honour the contract the rest
    of the branch runs on. Under the old `return []`-on-anything behaviour that
    one line would have handed `source_health` a fabricated success on every
    403, resetting the failure streak and stamping a `last_success` nobody
    observed. That is the exact shape of the fifty-day CEDARS outage.

    This has only ONE endpoint, so there is no partial degrade to preserve:
    every failure here is a total failure for the source.
    """
    if os.environ.get("HK_EVENTS_STUB") == "1":
        log.info("startmeuphk: STUB mode — returning sample events")
        return _stub_events()

    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
            resp = client.get(_EVENTS_URL)
    except Exception as exc:
        raise SourceFetchError(
            "startmeuphk", f"fetching {_EVENTS_URL} failed: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise SourceFetchError(
            "startmeuphk",
            f"{_EVENTS_URL} returned HTTP {resp.status_code} — the events listing "
            "was not read (this source was disabled in 2026-08 for exactly this: a "
            "403 at the edge on every run).",
        )
    try:
        return _parse_events_html(resp.text)
    except Exception as exc:
        raise SourceFetchError(
            "startmeuphk", f"parsing {_EVENTS_URL} failed: {exc}"
        ) from exc
