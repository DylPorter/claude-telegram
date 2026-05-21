"""Newsletter parser. Reads selected newsletters from Gmail via the `gws` CLI
(npm-global Google Workspace tool). Extracts subjects + bodies, normalizes to Item.

Newsletters list lives in config/newsletters.yaml. Each entry specifies:
    - id:     short id
    - name:   human-readable
    - query:  gmail search query (e.g. "from:newsletter@latent.space newer_than:2d")
    - domain: anti-bubble hint

If gws auth has expired, the source degrades cleanly — logs the error and
returns []. Run `gws auth login` to refresh.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone

import yaml

from signal_brief.config import CONFIG_DIR
from signal_brief.schema import Item

log = logging.getLogger(__name__)

NEWSLETTERS_FILE = CONFIG_DIR / "newsletters.yaml"
GWS_TIMEOUT = 30.0


def _load_newsletters() -> list[dict]:
    if not NEWSLETTERS_FILE.exists():
        log.info("no newsletters.yaml — skipping newsletter source")
        return []
    with open(NEWSLETTERS_FILE) as f:
        return yaml.safe_load(f).get("newsletters", [])


def _run_gws(args: list[str]) -> str:
    """Call gws and return stdout. Raises RuntimeError on auth or process failure."""
    result = subprocess.run(
        ["gws", *args],
        capture_output=True,
        text=True,
        timeout=GWS_TIMEOUT,
        check=False,
    )
    out = result.stdout
    # gws emits JSON error responses on auth failures; surface those clearly.
    if result.returncode != 0:
        err = result.stderr.strip() or out.strip()
        if "invalid_grant" in err or "Token has been expired" in err:
            raise RuntimeError(
                "gws auth expired — run `gws auth login` to refresh"
            )
        raise RuntimeError(f"gws failed (rc={result.returncode}): {err[:300]}")
    return out


def _list_messages(query: str, max_results: int = 3) -> list[str]:
    """Return Gmail message IDs matching the query."""
    params = json.dumps({"userId": "me", "q": query, "maxResults": max_results})
    raw = _run_gws(["gmail", "users", "messages", "list", "--params", params, "--format", "json"])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("could not parse gws list response: %s", e)
        return []
    messages = data.get("messages", [])
    return [m["id"] for m in messages if m.get("id")]


def _read_message(msg_id: str) -> dict | None:
    """Return parsed message with subject/body/date. None on failure."""
    try:
        raw = _run_gws(["gmail", "+read", "--id", msg_id, "--headers", "--format", "json"])
        return json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as e:
        log.warning("read failed for %s: %s", msg_id, e)
        return None


def _parse_email_date(s: str | None) -> datetime | None:
    if not s:
        return None
    # gws +read --format json typically returns ISO-ish date string or RFC2822
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def _fetch_one_newsletter(cfg: dict) -> list[Item]:
    nl_id = cfg["id"]
    name = cfg.get("name", nl_id)
    query = cfg["query"]
    domain = cfg.get("domain", "ai-tech")

    try:
        msg_ids = _list_messages(query, max_results=cfg.get("max_results", 3))
    except RuntimeError as e:
        log.warning("newsletter %s list failed: %s", nl_id, e)
        return []

    items: list[Item] = []
    for msg_id in msg_ids:
        msg = _read_message(msg_id)
        if not msg:
            continue

        # Schema from gws +read --headers --format json:
        #   { headers: {from, to, subject, date}, body: "...", id: ..., snippet: ... }
        # Fall back to top-level keys if structure differs.
        headers = msg.get("headers", {}) if isinstance(msg.get("headers"), dict) else {}
        subject = headers.get("subject") or msg.get("subject", "")
        date_str = headers.get("date") or msg.get("date")
        body = msg.get("body") or msg.get("snippet") or ""
        snippet = re.sub(r"\s+", " ", body).strip()[:800]

        url = f"https://mail.google.com/mail/u/0/#all/{msg_id}"

        items.append(
            Item(
                title=f"[{name}] {subject}",
                url=url,
                source=nl_id,
                source_kind="newsletter",
                published_at=_parse_email_date(date_str),
                excerpt=snippet,
                domain=domain,
                meta={"gmail_id": msg_id, "newsletter_name": name},
            )
        )

    log.info("newsletter %s: %d items", nl_id, len(items))
    return items


def fetch_newsletters() -> list[Item]:
    """Fetch configured newsletters via gws. Per-newsletter degrade on failure."""
    newsletters = _load_newsletters()
    items: list[Item] = []
    for cfg in newsletters:
        items.extend(_fetch_one_newsletter(cfg))
    return items
