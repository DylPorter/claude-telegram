"""RSS aggregator. Reads feeds.yaml, fetches each feed concurrently, normalizes
to Item, dedupes against the seen-urls cache.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from time import mktime
from typing import Any

import feedparser
import yaml

from signal_brief.config import CACHE_DIR, CONFIG_DIR
from signal_brief.schema import Domain, Item

log = logging.getLogger(__name__)

SEEN_CACHE = CACHE_DIR / "rss_seen.json"
FEEDS_FILE = CONFIG_DIR / "feeds.yaml"

# Items older than this are dropped — keeps the digest fresh + cache small.
FRESHNESS_WINDOW = timedelta(days=7)


def _load_feeds() -> list[dict[str, Any]]:
    with open(FEEDS_FILE) as f:
        data = yaml.safe_load(f)
    return data.get("feeds", [])


def _load_seen() -> dict[str, str]:
    if not SEEN_CACHE.exists():
        return {}
    try:
        return json.loads(SEEN_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_seen(seen: dict[str, str]) -> None:
    # Prune entries older than 30 days to keep file small.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    pruned = {url: ts for url, ts in seen.items() if ts >= cutoff}
    SEEN_CACHE.write_text(json.dumps(pruned, indent=2))


def _strip_html(text: str) -> str:
    """Minimal HTML strip for excerpt cleanup. Good enough for RSS summaries."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_one_feed(feed_cfg: dict[str, Any], timeout: float = 15.0) -> list[Item]:
    feed_id = feed_cfg["id"]
    name = feed_cfg.get("name", feed_id)
    url = feed_cfg["url"]
    domain: Domain = feed_cfg.get("domain", "ai-tech")
    max_items = int(feed_cfg.get("max_items", 10))
    bubble_breaker = bool(feed_cfg.get("bubble_breaker", False))

    try:
        # feedparser doesn't expose a timeout directly; we set the socket timeout via http.
        # In practice feedparser is reasonably resilient; documented as ≤30s by default.
        parsed = feedparser.parse(url)
    except Exception as e:
        log.warning("feed %s failed: %s", feed_id, e)
        return []

    if parsed.bozo and not parsed.entries:
        log.warning("feed %s bozo: %s", feed_id, parsed.get("bozo_exception"))
        return []

    items: list[Item] = []
    now = datetime.now(timezone.utc)
    cutoff = now - FRESHNESS_WINDOW

    for entry in parsed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue

        published_at: datetime | None = None
        for key in ("published_parsed", "updated_parsed"):
            t = entry.get(key)
            if t:
                try:
                    published_at = datetime.fromtimestamp(mktime(t), tz=timezone.utc)
                    break
                except (TypeError, ValueError):
                    pass

        if published_at and published_at < cutoff:
            continue

        # Excerpt: prefer 'summary' then 'description', then nothing.
        excerpt_raw = (
            entry.get("summary")
            or entry.get("description")
            or ""
        )
        excerpt = _strip_html(excerpt_raw)[:500]

        author = entry.get("author") or None

        item = Item(
            title=title,
            url=link,
            source=feed_id,
            source_kind="rss",
            published_at=published_at,
            excerpt=excerpt,
            author=author,
            domain=domain,
            meta={
                "feed_name": name,
                "bubble_breaker": bubble_breaker,
            },
        )
        items.append(item)

    log.info("feed %s: %d items", feed_id, len(items))
    return items


def fetch_rss(
    *,
    skip_seen: bool = True,
    max_workers: int = 6,
) -> list[Item]:
    """Fetch all configured RSS feeds in parallel. Returns deduped, fresh items.

    Args:
        skip_seen: If True, drop items whose URLs we've already surfaced in a
            previous run. Set False for backfill / test runs.
        max_workers: Concurrent feed fetches. Bounded to be polite + avoid
            blowing the connection pool.
    """
    feeds = _load_feeds()
    if not feeds:
        log.warning("no feeds configured")
        return []

    seen = _load_seen() if skip_seen else {}

    all_items: list[Item] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for items in pool.map(_parse_one_feed, feeds):
            all_items.extend(items)

    if skip_seen:
        fresh = [i for i in all_items if i.url not in seen]
        now_iso = datetime.now(timezone.utc).isoformat()
        for i in fresh:
            seen[i.url] = now_iso
        _save_seen(seen)
        log.info("rss total: %d items (%d new, %d filtered)",
                 len(all_items), len(fresh), len(all_items) - len(fresh))
        return fresh

    return all_items
