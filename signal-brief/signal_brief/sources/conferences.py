"""Tech conference calendar source. Surfaces conferences happening 'this week'
or 'today' as high-priority Items. Specifically targets the failure mode of
missing Google I/O / WWDC / re:Invent live.

Config: config/conferences.json
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from signal_brief.config import CONFIG_DIR
from signal_brief.schema import Item

log = logging.getLogger(__name__)

CONFERENCES_FILE = CONFIG_DIR / "conferences.json"

# How far ahead to look. 7 days = surface anything happening in the next week.
LOOKAHEAD_DAYS = 7


def _load_conferences() -> list[dict]:
    if not CONFERENCES_FILE.exists():
        log.warning("no conferences.json at %s", CONFERENCES_FILE)
        return []
    return json.loads(CONFERENCES_FILE.read_text())


def fetch_conferences(*, today: date | None = None) -> list[Item]:
    """Return Items for conferences happening today or in the next 7 days.

    Args:
        today: Override for testing; defaults to date.today() in local tz.
    """
    today = today or date.today()
    horizon = today + timedelta(days=LOOKAHEAD_DAYS)

    confs = _load_conferences()
    items: list[Item] = []
    for c in confs:
        try:
            start = date.fromisoformat(c["start"])
            end = date.fromisoformat(c.get("end", c["start"]))
        except (KeyError, ValueError) as e:
            log.warning("bad conference entry %r: %s", c, e)
            continue

        # Surface if: (a) currently running, (b) starting within lookahead, or
        # (c) ending today (last-day urgency).
        currently_running = start <= today <= end
        upcoming = today < start <= horizon
        last_day = end == today
        if not (currently_running or upcoming or last_day):
            continue

        days_until = (start - today).days
        if days_until > 0:
            urgency = f"in {days_until} day{'s' if days_until != 1 else ''}"
        elif currently_running:
            urgency = f"LIVE NOW (day {(today - start).days + 1} of {(end - start).days + 1})"
        else:
            urgency = "TODAY (last day)"

        items.append(
            Item(
                title=f"{c['name']} — {urgency}",
                url=c.get("url", ""),
                source="conferences",
                source_kind="conference",
                published_at=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                excerpt=c.get("description", ""),
                domain="conference",
                meta={
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "currently_running": currently_running,
                    "days_until": days_until,
                    "tags": c.get("tags", []),
                    # Conferences ALWAYS get high priority — they're the
                    # Google-I/O failure-mode fix.
                    "priority_boost": True,
                },
            )
        )

    log.info("conferences: %d items in lookahead window", len(items))
    return items
