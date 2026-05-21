"""Smoke test for the non-LLM pipeline: render → chunk → daily-note → Telegram.

Builds a hand-crafted Digest that mimics what the LLM would produce, then
exercises rendering, daily-note upsert, and (optionally) Telegram push.

Use this to verify the full delivery pipeline without spending an LLM call.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Allow running directly from the tests/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_brief.daily_note import upsert_signal_section
from signal_brief.exposure import record_digest
from signal_brief.render import render_for_daily_note, render_for_telegram
from signal_brief.schema import Digest, DigestSection, Item
from signal_brief.telegram_client import push_messages


def build_mock_digest() -> Digest:
    today = date.today().isoformat()
    now = datetime.now(timezone.utc)

    conference_item = Item(
        title="Sample Conference 2026 — LIVE NOW (day 2 of 3)",
        url="https://example.com/conference",
        source="conferences",
        source_kind="conference",
        published_at=now,
        excerpt="Major keynote happening now. Track for product launches.",
        domain="conference",
        meta={"currently_running": True, "priority_boost": True},
    )

    competitor_item = Item(
        title="Example Corp raises Series A in your space",
        url="https://example.com/competitor",
        source="hn-frontpage",
        source_kind="rss",
        published_at=now,
        excerpt="Direct competitor — pitch differentiation gets more urgent.",
        domain="ai-tech",
    )

    bubble_item = Item(
        title="The cognitive science of intuition",
        url="https://aeon.co/example",
        source="aeon-essays",
        source_kind="rss",
        published_at=now,
        excerpt="Outside-set: cog-sci adjacent to design intuition.",
        domain="philosophy",
        meta={"bubble_breaker": True},
    )

    return Digest(
        date=today,
        headline="Sample headline: a competitor moved + a conference is live.",
        sections=[
            DigestSection(
                title="Today's Signal",
                body="*Example Corp raises Series A* — direct competitor in your space. "
                     "Differentiation just got more urgent.\n\n"
                     "[Read →](https://example.com/competitor)",
                items=[competitor_item],
            ),
            DigestSection(
                title="Happening Now",
                body="*Sample Conference 2026 — LIVE day 2/3.* Keynote in progress; "
                     "track the announcements thread.\n\n"
                     "[Conference →](https://example.com/conference)",
                items=[conference_item],
            ),
            DigestSection(
                title="Bubble Breaker",
                body="🫧 _Outside your usual feed:_ *The cognitive science of intuition* "
                     "(Aeon). Adjacent to design / decision-making.\n\n"
                     "[Aeon essay →](https://aeon.co/example)",
                items=[bubble_item],
            ),
            DigestSection(
                title="Quiet rest",
                body="Routine arxiv / platform-blog posts. Nothing identity-level. "
                     "Full list in daily note.",
            ),
        ],
        all_items=[conference_item, competitor_item, bubble_item],
        rationale="Mock digest for pipeline smoke test. No real LLM call was made.",
    )


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--push", action="store_true",
                        help="Actually push to Telegram (default: print only)")
    parser.add_argument("--write-note", action="store_true",
                        help="Actually upsert into daily note (default: print only)")
    args = parser.parse_args()

    digest = build_mock_digest()
    messages = render_for_telegram(digest)
    daily_md = render_for_daily_note(digest)

    print("=" * 60)
    print(f"Mock digest for {digest.date} — {len(messages)} Telegram messages")
    print("=" * 60)
    for i, m in enumerate(messages, 1):
        print(f"\n--- bubble {i} ({len(m)} chars) ---")
        print(m)

    print("\n" + "=" * 60)
    print("Daily note section")
    print("=" * 60)
    print(daily_md)

    if args.write_note:
        path = upsert_signal_section(digest.date, daily_md)
        print(f"\nwrote {path}")

    if args.push:
        result = push_messages(messages)
        print(f"\npushed: sent={result.get('sent')} failed={result.get('failed')}")
        record_digest(digest)
        print("exposure log updated")

    return 0


if __name__ == "__main__":
    sys.exit(main())
