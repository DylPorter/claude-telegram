"""Shared scaffolding for HTML-page adapters (AI Tinkerers, Luma discovery).

The sibling `_ical_common` covers sources that hand us a clean `.ics`. These two
hand us a *page*, but not a scraped-DOM page in the brittle cyberport sense: both
server-render a machine-readable island inside the HTML (schema.org JSON-LD, and
Next.js `__NEXT_DATA__`). So the "scrape" here is one `<script>` lookup plus a
`json.loads`, not a pile of CSS selectors — which is why these can be trusted
where `cyberport`'s placeholder selectors could not.

This module holds the parts that don't vary: config lookup, the HTTP fetch, and
the raise-vs-empty contract. Parsing lives in the individual adapters.

THE CONTRACT (the whole reason this module exists as more than a `requests.get`):

    raise SourceNotConfiguredError — no URL configured. Nobody asked us anything,
        so the run learnt nothing about this source. The orchestrator puts it in
        NEITHER `succeeded` nor `errors`, and `update_health` prunes it.
    raise SourceFetchError        — we tried to look and could not: HTTP error,
        network error, the script island missing, or its JSON unparseable. A
        page that changed shape under us is a source we cannot read, not a quiet
        week.
    return []                     — we read the island, and it held no events.

Returning `[]` for either of the first two is the exact bug the sibling branch
was cut for: `source_health` scores a non-raising adapter as a SUCCESS, zeroing
a real failure streak and stamping a `last_success` that never happened.
"""

from __future__ import annotations

import logging

import httpx

from hk_events.errors import SourceFetchError, SourceNotConfiguredError
from hk_events.sources._ical_common import _HEADERS, _TIMEOUT, _load_sources_yaml, _within_horizon

log = logging.getLogger(__name__)

# Same browser-like User-Agent the feed adapters send — both hong-kong.aitinkerers.org
# and lu.ma refuse a bare python-httpx client. Only the Accept header differs: we
# want HTML here, not text/calendar.
_HTML_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_page_url(group: str, *, source: str) -> str:
    """Return the single page URL configured under `scrape_pages.<group>`.

    Same parking convention as `_ical_common.load_feed_urls`: an empty value or
    one starting with "TODO" counts as unconfigured, so an unverified page can
    sit in the YAML without being fetched.
    """
    cfg = _load_sources_yaml()
    entry = (cfg.get("scrape_pages", {}) or {}).get(group)
    url = entry.get("url") if isinstance(entry, dict) else entry
    if not url or str(url).strip().upper().startswith("TODO"):
        raise SourceNotConfiguredError(
            source,
            f"no usable page URL configured under scrape_pages.{group} "
            "(missing, empty, or still marked TODO)",
        )
    return str(url).strip()


def fetch_html(url: str, *, source: str) -> str:
    """GET a page and return its HTML.

    Raises `SourceFetchError` on any failure. Deliberately does NOT mirror
    `_ical_common.fetch_ics`, which returns None and lets its caller count
    failures across a *group* of feeds — these sources have exactly one URL
    each, so a failure here is already a total failure for the source and
    escalating immediately is the honest reading.
    """
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HTML_HEADERS) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        raise SourceFetchError(source, f"network error fetching {url}: {exc}") from exc
    if resp.status_code != 200:
        raise SourceFetchError(source, f"HTTP {resp.status_code} fetching {url}")
    return resp.text


__all__ = ["fetch_html", "load_page_url", "_within_horizon"]
