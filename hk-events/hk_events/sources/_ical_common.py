"""Shared scaffolding for iCal/.ics feed adapters (Meetup, Luma, AI Tinkerers).

These sources all expose a clean `.ics` feed. This module hosts the parts that
DON'T vary across feeds: config loading, HTTP fetch, VEVENT → Event parsing,
and horizon filtering. Per-feed URL wiring lives in the individual adapters.

Mirrors job-sift/sources/_ats_common.py (config loader + shared filter).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from icalendar import Calendar

from hk_events.config import HK_EVENTS_HORIZON_DAYS, PROJECT_ROOT
from hk_events.schema import Event, Source

log = logging.getLogger(__name__)

_TIMEOUT = 25.0
_CFG_CACHE: dict | None = None

# A polite UA — Meetup/Luma sometimes 403 a bare python-httpx client.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 hk-events/0.1"
    ),
    "Accept": "text/calendar, text/plain, */*",
}


def _load_sources_yaml() -> dict:
    """Memoized read of config/sources.yaml."""
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    cfg_path = PROJECT_ROOT / "config" / "sources.yaml"
    if not cfg_path.exists():
        log.warning("no sources.yaml at %s — feed sources return 0 events", cfg_path)
        _CFG_CACHE = {}
        return _CFG_CACHE
    with cfg_path.open() as f:
        _CFG_CACHE = yaml.safe_load(f) or {}
    return _CFG_CACHE


def load_feed_urls(group: str) -> list[str]:
    """Return the list of feed URLs configured under `ical_feeds.<group>`.

    Each entry may be a bare URL string, or a {name, url} mapping; we normalize
    to the URL. Entries whose URL is empty / starts with 'TODO' are skipped so
    an unverified feed never causes a fetch error.
    """
    cfg = _load_sources_yaml()
    feeds = (cfg.get("ical_feeds", {}) or {}).get(group, []) or []
    urls: list[str] = []
    for entry in feeds:
        url = entry.get("url") if isinstance(entry, dict) else entry
        if not url or str(url).strip().upper().startswith("TODO"):
            log.info("%s: skipping unverified/TODO feed entry: %r", group, entry)
            continue
        urls.append(str(url).strip())
    return urls


def fetch_ics(url: str) -> str | None:
    """GET a .ics feed. Returns text or None on any failure (degrade cleanly)."""
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            log.warning("ics fetch %s — HTTP %d", url, resp.status_code)
            return None
        return resp.text
    except httpx.HTTPError as exc:
        log.warning("ics fetch %s — network error: %s", url, exc)
        return None


def _to_datetime(value) -> datetime | None:
    """icalendar DTSTART/DTEND .dt can be a datetime or a date. Normalize to a
    tz-aware datetime where possible; return None for unparseable values."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, date):
        # All-day event — treat as local midnight (tz-naive→UTC for safety).
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def _within_horizon(start: datetime | None) -> bool:
    """Keep events starting from yesterday up to the rolling horizon. Events with
    no start time are kept (let the classifier / human decide)."""
    if start is None:
        return True
    now = datetime.now(timezone.utc)
    lo = now - timedelta(days=1)
    hi = now + timedelta(days=HK_EVENTS_HORIZON_DAYS)
    return lo <= start <= hi


_DESC_URL_RE = re.compile(r"https?://(?:lu\.ma|luma\.com)/[^\s\\]+", re.IGNORECASE)


def _link_from_description(description: str | None) -> str:
    """Pull the canonical event link from a feed DESCRIPTION when the URL
    property is empty. Luma leaves URL blank and embeds the real link as
    'Get up-to-date information at: https://luma.com/XXXX'."""
    if not description:
        return ""
    m = _DESC_URL_RE.search(description)
    return m.group(0) if m else ""


def parse_ics(text: str, *, source: Source, organizer_default: str | None = None) -> list[Event]:
    """Parse VEVENTs from an .ics document into horizon-filtered Event objects.

    The VEVENT UID is used as the stable per-source external_id. Past events and
    events beyond the horizon are filtered out here so downstream stages only see
    relevant candidates.
    """
    try:
        cal = Calendar.from_ical(text)
    except Exception as exc:
        log.warning("%s: failed to parse ics: %s", source, exc)
        return []

    events: list[Event] = []
    for comp in cal.walk("VEVENT"):
        uid = str(comp.get("UID", "")).strip()
        summary = str(comp.get("SUMMARY", "")).strip()
        if not uid or not summary:
            continue

        start = _to_datetime(getattr(comp.get("DTSTART"), "dt", None))
        if not _within_horizon(start):
            continue
        end = _to_datetime(getattr(comp.get("DTEND"), "dt", None))

        url = str(comp.get("URL", "")).strip()
        location = str(comp.get("LOCATION", "")).strip() or None
        description = str(comp.get("DESCRIPTION", "")).strip() or None
        organizer = str(comp.get("ORGANIZER", "")).strip() or organizer_default

        # Link priority: VEVENT URL → an http(s) LOCATION (some Luma events put
        # the real registration URL there, e.g. leapeast.com) → a lu.ma link
        # mined from the DESCRIPTION. NEVER a plain-text address — a street
        # address rendered as a [register] link is the dead link the operator hit.
        loc_link = location if (location and location.lower().startswith("http")) else ""
        link = url or loc_link or _link_from_description(description)

        events.append(
            Event(
                source=source,
                external_id=uid,
                title=summary,
                url=link,
                start=start,
                end=end,
                location=location,
                description=description,
                organizer=organizer,
                raw={"uid": uid},
            )
        )

    log.info("%s: parsed %d in-horizon events", source, len(events))
    return events


def fetch_feed_group(group: str, *, source: Source, organizer_default: str | None = None) -> list[Event]:
    """Fetch + parse every configured feed for a group. Per-feed degrade."""
    events: list[Event] = []
    for url in load_feed_urls(group):
        text = fetch_ics(url)
        if text is None:
            continue
        events.extend(parse_ics(text, source=source, organizer_default=organizer_default))
    return events
