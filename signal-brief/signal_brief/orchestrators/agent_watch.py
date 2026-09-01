"""Agent-identity trip-wire watcher.

Deliberately NOT part of the daily digest. The digest trains skimming; this
thing stays silent for months and then sends exactly one message when the
standard Dylan is waiting on actually lands. Rarity is the feature.

Exit codes: 0 always (a quiet run is a successful run). Failures log and exit 0
so a flaky feed never turns into a systemd failure notification.

Run manually:
    python -m signal_brief.orchestrators.agent_watch          # real run
    python -m signal_brief.orchestrators.agent_watch --test   # force a push
    python -m signal_brief.orchestrators.agent_watch --dry-run  # print, no push
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import unescape
from time import mktime

import feedparser
import yaml

from signal_brief.config import CACHE_DIR, CONFIG_DIR
from signal_brief.telegram_client import TelegramPushError, push_messages

log = logging.getLogger(__name__)

CONFIG_FILE = CONFIG_DIR / "agent_watch.yaml"
SEEN_CACHE = CACHE_DIR / "agent_watch_seen.json"

# Email is the second delivery leg. Telegram is fast but gets buried in the
# same chat as the digest; email survives being missed for a week and is
# searchable months later, which matters for a watcher that fires once.
EMAIL_TO = os.environ.get("AGENT_WATCH_EMAIL_TO", "you@example.com")

# Nothing older than this can trip the wire — stops a feed re-serving history
# on first run or after a cache wipe.
FRESHNESS_WINDOW = timedelta(days=14)

# Hard cap on messages per run. If ten things match at once, something is wrong
# with the matcher and spamming him is worse than truncating.
MAX_HITS = 4


def _load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def _load_seen() -> set[str]:
    if not SEEN_CACHE.exists():
        return set()
    try:
        return set(json.loads(SEEN_CACHE.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_seen(seen: set[str]) -> None:
    # Keep the cache bounded — these are URLs, but there's no reason to hold
    # them forever once they've aged out of the freshness window anyway.
    trimmed = list(seen)[-2000:]
    SEEN_CACHE.write_text(json.dumps(trimmed))


def _fetch(feed: dict) -> list[dict]:
    """Fetch one feed. Never raises — a dead feed must not kill the run."""
    try:
        parsed = feedparser.parse(feed["url"])
    except Exception as e:  # feedparser is broad about what it throws
        log.warning("feed %s failed: %s", feed["name"], e)
        return []

    cutoff = datetime.now(timezone.utc) - FRESHNESS_WINDOW
    out = []
    for entry in parsed.entries:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if stamp:
            published = datetime.fromtimestamp(mktime(stamp), tz=timezone.utc)
            if published < cutoff:
                continue
        url = entry.get("link", "")
        if not url:
            continue
        out.append({
            "source": feed["name"],
            "title": unescape(entry.get("title", "")).strip(),
            "summary": unescape(entry.get("summary", ""))[:1200],
            "url": url,
        })
    return out


def _classify(item: dict, core: list[str], escalation: list[str]) -> str | None:
    """Return 'TIER1', 'TIER2', or None.

    Matching is substring-on-lowercase rather than regex on purpose: the core
    phrases are already specific enough that word-boundary precision buys
    nothing, and regex here would just be a source of silent bugs.
    """
    haystack = f"{item['title']} {item['summary']}".lower()

    if not any(phrase in haystack for phrase in core):
        return None
    if any(phrase in haystack for phrase in escalation):
        return "TIER1"
    return "TIER2"


def _render(hits: list[tuple[str, dict]]) -> list[str]:
    tier1 = [h for h in hits if h[0] == "TIER1"]
    tier2 = [h for h in hits if h[0] == "TIER2"]
    messages: list[str] = []

    if tier1:
        messages.append(
            "🚨 *AGENT-IDENTITY TRIP-WIRE*\n\n"
            "Something just matched the enforcement pattern. This is the "
            "signal you asked to be interrupted for."
        )
        for _, item in tier1:
            messages.append(
                f"*{item['title']}*\n_{item['source']}_\n\n{item['url']}"
            )
        messages.append(
            "*Why you're seeing this:* you flagged that when agent "
            "verification becomes a default rather than an option, HK "
            "e-commerce suddenly needs someone who understands agent traffic "
            "policy — and you wanted to be the one who'd been writing about "
            "it first.\n\nIf this is the real thing: publish within the week."
        )

    if tier2:
        messages.append(
            "👀 *Agent-identity watch* — movement, not the switch:\n\n"
            + "\n\n".join(
                f"• *{item['title']}*\n  _{item['source']}_\n  {item['url']}"
                for _, item in tier2
            )
        )

    return messages


def _send_email(hits: list[tuple[str, dict]]) -> None:
    """Send via the gws CLI (already authenticated for Dylan's Gmail).

    Never raises — email is the backup leg, and a gws auth expiry (which
    happens roughly weekly) must not take the Telegram push down with it.
    """
    tier1 = [h for h in hits if h[0] == "TIER1"]
    subject = (
        "🚨 AGENT-IDENTITY TRIP-WIRE — the switch may have flipped"
        if tier1
        else "👀 Agent-identity watch — movement"
    )

    lines = []
    if tier1:
        lines.append(
            "Something matched the ENFORCEMENT pattern — this is the signal "
            "you asked to be interrupted for.\n"
        )
    for tier, item in hits:
        lines.append(f"[{tier}] {item['title']}")
        lines.append(f"  {item['source']}")
        lines.append(f"  {item['url']}\n")

    lines.append(
        "---\n"
        "Why you're seeing this: when agent verification becomes a default "
        "rather than an option, HK e-commerce needs someone who understands "
        "agent traffic policy. You wanted to be the one who'd already been "
        "writing about it.\n\n"
        "If this is the real thing: publish within the week.\n\n"
        "Tune or silence: signal-brief/config/agent_watch.yaml"
    )

    msg = EmailMessage()
    msg["To"] = EMAIL_TO
    msg["From"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content("\n".join(lines))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        subprocess.run(
            ["gws", "gmail", "users", "messages", "send",
             "--params", json.dumps({"userId": "me"}),
             "--json", json.dumps({"raw": raw})],
            check=True, capture_output=True, timeout=60,
        )
        log.info("email sent to %s", EMAIL_TO)
    except subprocess.CalledProcessError as e:
        log.error("email failed (gws auth expired? run `gws auth login`): %s",
                  e.stderr.decode()[:300])
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("email failed: %s", e)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't push")
    ap.add_argument("--test", action="store_true", help="send a fake TIER1 push")
    args = ap.parse_args()

    if args.test:
        fake = [("TIER1", {
            "title": "TEST — Cloudflare enables Web Bot Auth verification by default",
            "source": "agent-watch self-test",
            "url": "https://blog.cloudflare.com/signed-agents/",
        })]
        push_messages(_render(fake))
        _send_email(fake)
        log.info("test push + email sent")
        return 0

    cfg = _load_config()
    core = [p.lower() for p in cfg["core"]]
    escalation = [p.lower() for p in cfg["escalation"]]
    seen = _load_seen()

    items: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(_fetch, cfg["feeds"]):
            items.extend(result)

    hits: list[tuple[str, dict]] = []
    for item in items:
        if item["url"] in seen:
            continue
        tier = _classify(item, core, escalation)
        if tier:
            hits.append((tier, item))
        # Mark every item seen, matched or not — a story that didn't trip the
        # wire today shouldn't get re-evaluated tomorrow after a config tweak
        # and suddenly fire on week-old news.
        seen.add(item["url"])

    _save_seen(seen)

    if not hits:
        log.info("quiet run — %d items scanned, nothing tripped", len(items))
        return 0

    # TIER1 first, and cap the total.
    hits.sort(key=lambda h: 0 if h[0] == "TIER1" else 1)
    if len(hits) > MAX_HITS:
        log.warning("capping %d hits to %d", len(hits), MAX_HITS)
        hits = hits[:MAX_HITS]

    messages = _render(hits)
    if args.dry_run:
        print("\n\n---\n\n".join(messages))
        return 0

    # Two independent legs. Either can fail without taking the other down —
    # the whole point is that this alert cannot be missed.
    try:
        push_messages(messages)
        log.info("pushed %d hit(s)", len(hits))
    except TelegramPushError as e:
        log.error("push failed: %s", e)

    _send_email(hits)

    return 0


if __name__ == "__main__":
    sys.exit(main())
