"""X/Twitter source — best-effort.

Twitter's official API is hostile/expensive. We use RSSHub public instances
as the primary route, and skip gracefully if all instances fail.

Config: config/twitter_accounts.yaml
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from time import mktime

import feedparser
import yaml

from signal_brief.config import CONFIG_DIR
from signal_brief.schema import Item

log = logging.getLogger(__name__)

TWITTER_FILE = CONFIG_DIR / "twitter_accounts.yaml"

# Tried in order — first one that responds with entries wins.
# These rotate frequently; expect breakage and degrade gracefully.
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rss.shab.fun",
]

# Items older than this are dropped.
FRESHNESS_WINDOW = timedelta(hours=36)


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _load_accounts() -> list[dict]:
    if not TWITTER_FILE.exists():
        log.info("no twitter_accounts.yaml — skipping X source")
        return []
    with open(TWITTER_FILE) as f:
        return yaml.safe_load(f).get("accounts", [])


def _fetch_one_account(handle: str, domain: str = "ai-tech") -> list[Item]:
    """Try each RSSHub instance until one returns entries. Returns [] if all fail."""
    handle = handle.lstrip("@")
    now = datetime.now(timezone.utc)
    cutoff = now - FRESHNESS_WINDOW

    for instance in RSSHUB_INSTANCES:
        url = f"{instance}/twitter/user/{handle}"
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            log.debug("twitter %s @ %s: %s", handle, instance, e)
            continue

        if not parsed.entries:
            continue

        items: list[Item] = []
        for entry in parsed.entries[:10]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue

            published_at = None
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

            excerpt = _strip_html(entry.get("summary") or "")[:400]

            items.append(
                Item(
                    title=f"@{handle}: {title[:120]}",
                    url=link,
                    source=f"twitter:{handle}",
                    source_kind="twitter",
                    published_at=published_at,
                    excerpt=excerpt,
                    author=f"@{handle}",
                    domain=domain,
                    meta={"handle": handle, "via": instance},
                )
            )

        log.info("twitter @%s: %d items via %s", handle, len(items), instance)
        return items

    log.warning("twitter @%s: all instances failed", handle)
    return []


def fetch_twitter() -> list[Item]:
    """Fetch tweets for configured accounts. Best-effort — degrades cleanly."""
    accounts = _load_accounts()
    items: list[Item] = []
    for acc in accounts:
        handle = acc.get("handle") or acc.get("id")
        if not handle:
            continue
        domain = acc.get("domain", "ai-tech")
        items.extend(_fetch_one_account(handle, domain=domain))
    return items
