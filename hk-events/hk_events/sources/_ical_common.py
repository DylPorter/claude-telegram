"""Shared scaffolding for iCal/.ics feed adapters (Meetup, Luma, AI Tinkerers).

These sources all expose a clean `.ics` feed. This module hosts the parts that
DON'T vary across feeds: config loading, HTTP fetch, VEVENT → Event parsing,
and horizon filtering. Per-feed URL wiring lives in the individual adapters.

Mirrors job-sift/sources/_ats_common.py (config loader + shared filter).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from icalendar import Calendar

from hk_events.config import HK_EVENTS_HORIZON_DAYS, PROJECT_ROOT
from hk_events.errors import SourceFetchError, SourceNotConfiguredError
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
    no start time are kept (let the classifier / human decide).

    ⚠️ `start is None` therefore returns True, and that policy is now reachable
    from a HISTORY-SHAPED source. It was written for .ics feeds, where a dateless
    VEVENT is rare and usually imminent. The AI Tinkerers homepage is mostly an
    archive of past meetups, so an entry with a missing or unparseable `startDate`
    never ages out: it is kept every run, and `Event.stable_hash` buckets it under
    "nodate". It still only notifies ONCE (the seen-set is keyed on the schema.org
    @id, which is stable), and `_is_soon` returns False for it so it never fires a
    reminder — so this is a permanent classifier cost, not a repeat-push bug.
    Left as-is deliberately: dropping dateless events would silently discard a
    real upcoming event whose date we merely failed to parse, which is worse.
    """
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


def parse_ics(
    text: str,
    *,
    source: Source,
    organizer_default: str | None = None,
    canonical_id: Callable[[str], str | None] | None = None,
) -> list[Event] | None:
    """Parse VEVENTs from an .ics document into horizon-filtered Event objects.

    The VEVENT UID is used as the stable per-source external_id. Past events and
    events beyond the horizon are filtered out here so downstream stages only see
    relevant candidates.

    `canonical_id`, when given, maps a UID to a CROSS-source identity for the same
    real-world event (or None when it cannot). It is how the Luma .ics feeds
    collide with the Luma city-page adapter instead of double-reporting — see
    `Event.identity_key`. Feeds with no twin (Meetup) pass nothing and fall back
    to the source-prefixed `dedup_key`.

    RETURNS None vs `[]`, and the difference is load-bearing:

      * `None` — the document is not a calendar. `Calendar.from_ical` refused
        it, which in practice means the feed host answered HTTP **200** with an
        HTML error page, a Cloudflare interstitial, or a login wall. `fetch_ics`
        cannot catch that by construction: the failure IS a 200.
      * `[]` — it parsed as a calendar and held no VEVENT inside the horizon.
        A real, observed "nothing on this feed".

    Collapsing the two is the same bug that killed CEDARS: `fetch_feed_group`
    never counted an unparseable feed toward `failed`, so four Luma feeds all
    serving interstitials gave `failed == 0`, skipped the total-failure raise,
    and returned `[]` — which the orchestrator scored a SUCCESS, zeroing the
    failure streak and stamping a `last_success` nobody observed.
    """
    try:
        cal = Calendar.from_ical(text)
    except Exception as exc:
        log.warning(
            "%s: response did not parse as a calendar (%s) — treating as a failed "
            "feed, not an empty one",
            source,
            exc,
        )
        return None

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

        raw: dict = {"uid": uid}
        if canonical_id is not None:
            canon = canonical_id(uid)
            if canon:
                raw["canonical_id"] = canon

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
                raw=raw,
            )
        )

    log.info("%s: parsed %d in-horizon events", source, len(events))
    return events


def fetch_feed_group(
    group: str,
    *,
    source: Source,
    organizer_default: str | None = None,
    canonical_id: Callable[[str], str | None] | None = None,
) -> list[Event]:
    """Fetch + parse every configured feed for a group.

    PARTIAL degrade, TOTAL escalation. One dead feed out of four is a partial
    success: it is logged, skipped, and the other three still report. But if
    EVERY configured feed failed we raise `SourceFetchError` instead of
    returning `[]`.

    A feed counts as FAILED whether it failed to fetch (`fetch_ics` → None) or
    fetched and did not parse as a calendar (`parse_ics` → None). The second
    half matters more than it looks: `fetch_ics` only checks the status code,
    so a host answering 200 with an HTML error page or a Cloudflare
    interstitial is invisible to it — the failure IS a 200.

    That distinction is the whole point of this branch. `fetch_ics` swallows
    `httpx.HTTPError`, and `httpx` wraps `socket.gaierror` in `ConnectError`,
    which is an `HTTPError` — so a total DNS outage used to come back as a clean
    empty list. The orchestrator would then find this source ABSENT from the
    error map, `source_health` would read that absence as proof of a successful
    fetch, reset an accumulated failure streak to 0, and write today as
    `last_success`. A fabricated fact, persisted to disk, later rendered to a
    human as "nothing today". Returning zero must mean we looked.

    NOT CONFIGURED is a THIRD outcome. With no usable feed URL for the group —
    the key is missing, the list is empty, or every entry is still marked TODO —
    there is nothing to fetch, so the run learnt nothing about this source
    either way. The old escalation guard read `if urls and failed == len(urls)`,
    so that case skipped the raise and returned `[]`, which `source_health`
    scored as a success exactly like the outage above. It now raises
    `SourceNotConfiguredError`, which the orchestrator scores as neither a
    success nor a failure and therefore PRUNES — see errors.py.
    """
    urls = load_feed_urls(group)
    if not urls:
        raise SourceNotConfiguredError(
            str(source),
            f"no usable feed URL configured under ical_feeds.{group} "
            "(missing, empty, or every entry still marked TODO)",
        )
    events: list[Event] = []
    failed = 0
    for url in urls:
        text = fetch_ics(url)
        if text is None:
            failed += 1
            continue
        parsed = parse_ics(
            text,
            source=source,
            organizer_default=organizer_default,
            canonical_id=canonical_id,
        )
        # A 200 that is not a calendar is a FAILED feed, not an empty one. It
        # has to increment the same counter as a transport failure, or the
        # total-failure raise below can never fire on the realistic outage
        # (every feed answering 200 with an HTML interstitial).
        if parsed is None:
            failed += 1
            continue
        events.extend(parsed)
    if failed == len(urls):
        raise SourceFetchError(
            str(source),
            f"all {len(urls)} configured feed(s) failed to fetch or parse — "
            "network/DNS outage, every feed URL is dead, or every host answered "
            "200 with something that is not a calendar (see log for per-feed detail)",
        )
    return events
