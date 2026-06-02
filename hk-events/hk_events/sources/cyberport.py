"""Cyberport events scraper.

Source page: https://www.cyberport.hk/en/news/events/events_calendar (events list).
There is no clean public feed, so this is a brittle HTML scraper — it MUST
degrade cleanly per the signal-brief pattern (catch everything, return [] on
failure, never break the run).

**v0 STATUS: stub** — returns hardcoded sample events for end-to-end pipeline
testing. The real implementation needs HTML structure inspection of the live
events listing to write the parser. Switching from stub to real is gated by the
HK_EVENTS_STUB env var, same as job-sift's cedars source.

TODO: confirm the exact events-listing URL and inspect the card markup. The base
URL below is the documented public events area; the precise listing path may
differ (a /events_calendar JSON endpoint may exist — check the network tab).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

from hk_events.schema import Event

log = logging.getLogger(__name__)

# TODO: verify the exact listing URL. cyberport.hk/en/news/events/ is the public
# events area; the calendar listing may live at a sub-path or a JSON endpoint.
_EVENTS_URL = "https://www.cyberport.hk/en/news/events/"
_TIMEOUT = 25.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hk-events/0.1"}


def _stub_events() -> list[Event]:
    now = datetime.now(timezone.utc)
    return [
        Event(
            source="cyberport",
            external_id="cyberport-stub-startup-pitch",
            title="Cyberport Venture Capital Forum — Startup Pitch Night",
            url="https://www.cyberport.hk/en/news/events/stub-pitch",
            start=now + timedelta(days=5, hours=18),
            end=now + timedelta(days=5, hours=21),
            location="Cyberport, Pok Fu Lam, Hong Kong",
            description="Funded startups pitch to VCs. AI and deep-tech founders showcase.",
            organizer="Cyberport",
        ),
        Event(
            source="cyberport",
            external_id="cyberport-stub-gala",
            title="Cyberport Annual Members Gala Dinner",
            url="https://www.cyberport.hk/en/news/events/stub-gala",
            start=now + timedelta(days=9, hours=19),
            location="Cyberport, Hong Kong",
            description="Black-tie networking dinner. Social only.",
            organizer="Cyberport",
        ),
    ]


def _parse_events_html(html: str) -> list[Event]:
    """Parse the Cyberport events listing.

    TODO: selectors below are PLACEHOLDERS inferred from a typical event-card
    layout — they have NOT been validated against the live DOM. Inspect the page
    and fix the selectors before flipping off stub mode.
    """
    soup = BeautifulSoup(html, "lxml")
    events: list[Event] = []
    # PLACEHOLDER selector — replace after inspecting the live markup.
    for card in soup.select(".event-item, .event-card, article.event"):
        title_el = card.select_one(".event-title, h3, h2 a")
        link_el = card.select_one("a[href]")
        if not title_el or not link_el:
            continue
        title = title_el.get_text(strip=True)
        href = link_el.get("href", "")
        if href and href.startswith("/"):
            href = "https://www.cyberport.hk" + href
        ext_id = "cyberport-" + re.sub(r"[^a-z0-9]+", "-", title.lower())[:60]
        events.append(
            Event(
                source="cyberport",
                external_id=ext_id,
                title=title,
                url=href,
                start=None,  # TODO: parse date element once selectors confirmed
                location="Cyberport, Hong Kong",
                organizer="Cyberport",
            )
        )
    log.info("cyberport: parsed %d events", len(events))
    return events


def fetch_cyberport_events() -> list[Event]:
    """Public entry point. Degrades to [] on any failure."""
    if os.environ.get("HK_EVENTS_STUB") == "1":
        log.info("cyberport: STUB mode — returning sample events")
        return _stub_events()

    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
            resp = client.get(_EVENTS_URL)
        if resp.status_code != 200:
            log.warning("cyberport: HTTP %d — skipping", resp.status_code)
            return []
        return _parse_events_html(resp.text)
    except Exception as exc:
        log.warning("cyberport: fetch/parse failed: %s — skipping", exc)
        return []
