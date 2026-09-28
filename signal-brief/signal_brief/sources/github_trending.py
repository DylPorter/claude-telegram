"""GitHub trending (daily) — scraped from github.com/trending.

There is no official trending API. The page is server-rendered HTML with one
`<article class="Box-row">` per repo, which is stable enough to regex. If GitHub
changes the markup this returns [] and logs, it never raises.
"""

from __future__ import annotations

import logging
import re
from html import unescape

import requests

from signal_brief.schema import Item

log = logging.getLogger(__name__)

TRENDING_URL = "https://github.com/trending?since=daily"
UA = "Mozilla/5.0 (X11; Linux x86_64) signal-brief/0.1"

_ROW_SPLIT = re.compile(r'<article class="Box-row">')
_REPO = re.compile(r'<h2[^>]*>.*?href="/([\w.-]+/[\w.-]+)"', re.DOTALL)
_DESC = re.compile(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', re.DOTALL)
_LANG = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
_TODAY = re.compile(r"([\d,]+)\s+stars today")
_TOTAL = re.compile(r'href="/[\w.-]+/[\w.-]+/stargazers"[^>]*>.*?</svg>\s*([\d,]+)', re.DOTALL)


def _int(s: str | None) -> int:
    return int(s.replace(",", "")) if s else 0


def parse_trending(html: str) -> list[Item]:
    items: list[Item] = []
    for row in _ROW_SPLIT.split(html)[1:]:
        m = _REPO.search(row)
        if not m:
            continue
        repo = m.group(1)
        desc = _DESC.search(row)
        lang = _LANG.search(row)
        today = _TODAY.search(row)
        total = _TOTAL.search(row)
        items.append(Item(
            title=repo,
            url=f"https://github.com/{repo}",
            source="github-trending",
            source_kind="github",
            excerpt=re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", desc.group(1)))).strip()[:500]
            if desc else "",
            domain="ai-tech",
            meta={
                "repo": repo,
                "language": lang.group(1).strip() if lang else None,
                "stars_today": _int(today.group(1)) if today else 0,
                "stars": _int(total.group(1)) if total else 0,
            },
        ))
    return items


def fetch_github_trending(*, timeout: float = 15.0) -> list[Item]:
    try:
        r = requests.get(TRENDING_URL, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("github trending failed: %s", e)
        return []
    items = parse_trending(r.text)
    if not items:
        log.warning("github trending: 0 repos parsed — markup may have changed")
    log.info("github trending: %d repos", len(items))
    return items
