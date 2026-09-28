"""Hacker News front page via the official Firebase API.

Used by the x-drafts job, which needs live points/comment counts to judge what
the builder scene is actually talking about today. The morning brief keeps using
hnrss.org through `rss.py`; this module does not touch its seen-urls cache.

API: https://github.com/HackerNews/API (free, no key, no auth).
"""

from __future__ import annotations

import concurrent.futures
import logging
from datetime import datetime, timedelta, timezone
from html import unescape
import re

import requests

from signal_brief.schema import Item

log = logging.getLogger(__name__)

API = "https://hacker-news.firebaseio.com/v0"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"

# Front page is ~30 stories; anything older than this has already had its day.
FRESHNESS_WINDOW = timedelta(hours=36)


def _get_json(url: str, timeout: float):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def story_to_item(story: dict, *, now: datetime | None = None) -> Item | None:
    """Normalise one HN API story dict. Returns None for anything that is not a
    live, fresh story (jobs, dead/deleted, stale)."""
    if not story or story.get("type") != "story":
        return None
    if story.get("dead") or story.get("deleted"):
        return None
    title = (story.get("title") or "").strip()
    if not title:
        return None
    now = now or datetime.now(timezone.utc)
    published = None
    if story.get("time"):
        published = datetime.fromtimestamp(int(story["time"]), tz=timezone.utc)
        if published < now - FRESHNESS_WINDOW:
            return None
    hn_url = HN_ITEM_URL.format(id=story["id"])
    return Item(
        title=title,
        # Ask/Show HN posts have no external url — the discussion IS the story.
        url=story.get("url") or hn_url,
        source="hn",
        source_kind="hn",
        published_at=published,
        excerpt=_strip_html(story.get("text", ""))[:500],
        author=story.get("by"),
        domain="ai-tech",
        meta={
            "hn_id": story["id"],
            "hn_url": hn_url,
            "points": int(story.get("score") or 0),
            "comments": int(story.get("descendants") or 0),
        },
    )


def fetch_hn_front_page(
    *, limit: int = 30, min_points: int = 50, timeout: float = 10.0
) -> list[Item]:
    """Top `limit` HN stories with at least `min_points`. Never raises — a dead
    API is one missing source, not a failed run."""
    try:
        ids = _get_json(f"{API}/topstories.json", timeout)[:limit]
    except Exception as e:  # noqa: BLE001
        log.warning("hn: topstories failed: %s", e)
        return []

    def _one(story_id: int):
        try:
            return _get_json(f"{API}/item/{story_id}.json", timeout)
        except Exception as e:  # noqa: BLE001
            log.warning("hn: item %s failed: %s", story_id, e)
            return None

    now = datetime.now(timezone.utc)
    items: list[Item] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for story in pool.map(_one, ids):
            item = story_to_item(story, now=now) if story else None
            if item and item.meta["points"] >= min_points:
                items.append(item)
    log.info("hn: %d front-page stories", len(items))
    return items
