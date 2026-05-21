"""Exposure log for anti-bubble enforcement.

Tracks which content domains have been surfaced over the past N days.
The filter pass reads this and is REQUIRED to surface >= 1 item from an
underrepresented domain in every digest, so personalization doesn't collapse
into a filter bubble.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from signal_brief.config import CACHE_DIR
from signal_brief.schema import Digest, Domain

log = logging.getLogger(__name__)

EXPOSURE_FILE = CACHE_DIR / "exposure_log.json"

# Track exposure over this rolling window.
WINDOW_DAYS = 7

# Canonical set of domains the filter rotates across.
ALL_DOMAINS: list[Domain] = [
    "ai-tech",
    "platform-engineering",
    "startups-funding",
    "research-papers",
    "policy-geopolitics",
    "biology-health",
    "design",
    "philosophy",
    "finance-markets",
    "hardware",
    "career-industry",
    "conference",
    "other",
]


def _load_log() -> list[dict]:
    if not EXPOSURE_FILE.exists():
        return []
    try:
        return json.loads(EXPOSURE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_log(entries: list[dict]) -> None:
    EXPOSURE_FILE.write_text(json.dumps(entries, indent=2))


def record_digest(digest: Digest) -> None:
    """Append a digest's surfaced items to the log; prune outside the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS * 2)
    log_entries = [e for e in _load_log()
                   if datetime.fromisoformat(e["ts"]) >= cutoff]

    now = datetime.now(timezone.utc).isoformat()
    for section in digest.sections:
        for item in section.items:
            log_entries.append({
                "ts": now,
                "date": digest.date,
                "title": item.title,
                "source": item.source,
                "domain": item.domain or "other",
            })

    _save_log(log_entries)
    log.info("exposure: recorded %d items, log size %d",
             sum(len(s.items) for s in digest.sections), len(log_entries))


def get_recent_distribution(window_days: int = WINDOW_DAYS) -> dict[str, int]:
    """Return a {domain: count} dict for items surfaced in the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    counts: Counter[str] = Counter()
    for e in _load_log():
        try:
            ts = datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        counts[e.get("domain", "other")] += 1
    return dict(counts)


def get_underrepresented_domains(window_days: int = WINDOW_DAYS) -> list[str]:
    """Domains that haven't appeared (or barely appeared) in the recent window.

    Returns domains sorted by ascending count — most under-represented first.
    Always includes domains with zero exposure.
    """
    counts = get_recent_distribution(window_days)
    ranked: list[tuple[str, int]] = [(d, counts.get(d, 0)) for d in ALL_DOMAINS]
    ranked.sort(key=lambda x: x[1])
    return [d for d, _ in ranked]


def format_for_prompt(window_days: int = WINDOW_DAYS) -> str:
    """Render exposure summary as a markdown table for the LLM filter prompt."""
    counts = get_recent_distribution(window_days)
    lines = [f"## Exposure log — past {window_days} days",
             "",
             "| Domain | Items surfaced |",
             "|---|---|"]
    for domain in ALL_DOMAINS:
        n = counts.get(domain, 0)
        marker = " 🚨 underexposed" if n == 0 else ""
        lines.append(f"| {domain} | {n}{marker} |")
    return "\n".join(lines)
