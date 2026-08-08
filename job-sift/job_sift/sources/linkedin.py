"""LinkedIn job-alert email parser.

LinkedIn doesn't expose a usable jobs API and aggressively blocks scrapers,
so we use LinkedIn's *own* delivery channel: digest emails. the operator sets up
saved searches on LinkedIn, LinkedIn emails digests to a dedicated Gmail
label, and this module reads/parses them via the gws CLI.

Email format (confirmed 2026-05-21 with a real sample):
  - Senders: jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com
  - Each email contains MULTIPLE job listings (typically 6+) — the digest
    is intentional, not single-job-per-email.
  - Each listing card is a <tr> with two <td>s:
    - Left: <a><img alt="{Company Name}">  (logo, alt has company)
    - Right: <a>{Job Title}</a>  +  text "{Company} · {Location} [badges]"
  - All anchors point to https://www.linkedin.com/comm/jobs/view/{id}?...
    with heavy tracking-param noise. We extract the {id} and canonicalize.
  - Confirmation emails ("the operator: your job alert for ... has been created")
    have NO job-view links and are skipped by the parser naturally.

Listings from LinkedIn run through the FULL classifier path (prestige + scope)
because LinkedIn surfaces everything: prestige + no-name + marginal alike.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
from typing import Iterator

from bs4 import BeautifulSoup

from job_sift.errors import SourceAuthError
from job_sift.schema import JobListing

log = logging.getLogger(__name__)


LINKEDIN_LABEL = os.environ.get("LINKEDIN_GMAIL_LABEL", "LinkedIn Jobs")
# LinkedIn uses BOTH of these senders for job-alert digests; match either.
LINKEDIN_SENDERS = (
    os.environ.get("LINKEDIN_SENDER", "jobalerts-noreply@linkedin.com"),
    os.environ.get("LINKEDIN_SENDER_2", "jobs-noreply@linkedin.com"),
)

_JOB_URL_RE = re.compile(r"jobs/view/(\d+)")
_BADGE_RE = re.compile(
    r"\s+(?:Actively recruiting|Easy Apply|Fast growing|Promoted|Reposted|"
    r"\d+\s*connections?|\d+\s*school alum(?:ni)?|\d+\s*alumni|Be an early applicant)",
    re.IGNORECASE,
)


# ---------- Gmail fetch ----------


def _gws_list_messages() -> list[dict]:
    """List recent LinkedIn job-alert messages via gws CLI.

    Env overrides (for a one-time deep catch-up after an outage, without
    changing the cheap daily defaults of 2 days / 50 messages):
      - JOB_SIFT_LINKEDIN_DAYS : Gmail `newer_than` window in days (default 2).
      - JOB_SIFT_LINKEDIN_MAX  : maxResults cap (default 50).
    """
    days = os.environ.get("JOB_SIFT_LINKEDIN_DAYS", "2")
    max_results = int(os.environ.get("JOB_SIFT_LINKEDIN_MAX", "50"))
    sender_q = " OR ".join(f"from:{s}" for s in LINKEDIN_SENDERS)
    query = f"({sender_q}) newer_than:{days}d"
    cmd = [
        "gws", "gmail", "users", "messages", "list",
        "--params", json.dumps({"userId": "me", "q": query, "maxResults": max_results}),
        "--format", "json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("linkedin: gws unavailable or timed out: %s", exc)
        return []
    if proc.returncode != 0:
        err = proc.stderr[:300] or proc.stdout[:300]
        # An expired/revoked OAuth token is an auth failure, not "no jobs" —
        # raise so the orchestrator surfaces a ⚠️ health line instead of a
        # silent empty digest. gws tokens die ~weekly while the Google OAuth
        # app is in Testing status (permanent fix: publish to Production).
        if any(k in err.lower() for k in ("invalid_grant", "expired", "revoked", "token has been")):
            raise SourceAuthError(
                "linkedin",
                "gws Gmail auth expired (invalid_grant) — run `gws auth login` "
                "to re-authenticate. Permanent fix: publish the Google OAuth "
                "app Testing→Production so refresh tokens stop expiring weekly",
            )
        log.warning("linkedin: gws list failed: %s", err)
        return []
    try:
        return json.loads(proc.stdout).get("messages", []) or []
    except (json.JSONDecodeError, AttributeError):
        log.warning("linkedin: gws returned non-JSON")
        return []


def _gws_fetch_html(message_id: str) -> str | None:
    """Fetch one message and walk its parts to find the text/html body."""
    cmd = [
        "gws", "gmail", "users", "messages", "get",
        "--params", json.dumps({"userId": "me", "id": message_id, "format": "full"}),
        "--format", "json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return _walk_for_html(data.get("payload") or {})


def _walk_for_html(part: dict) -> str | None:
    if part.get("mimeType") == "text/html":
        data = (part.get("body") or {}).get("data")
        if data:
            try:
                return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
            except Exception:
                return None
    for sub in part.get("parts") or []:
        r = _walk_for_html(sub)
        if r:
            return r
    return None


# ---------- HTML parser ----------


def _canonical_apply_url(job_id: str) -> str:
    """Stable URL without LinkedIn's tracking-param soup."""
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def _parse_alert_email(html: str) -> Iterator[JobListing]:
    """Extract JobListing objects from one LinkedIn digest email.

    Strategy:
      1. Find every <a href> that points to /jobs/view/{id}. Each unique id
         typically appears 2-3 times in a card (logo wrap, title wrap,
         sometimes a CTA button).
      2. For each unique id:
           - Logo anchor: contains <img alt="{Company Name}">
           - Title anchor: contains the job title as plain text
           - Walk up the DOM until we find a parent whose text contains " · ";
             everything after " · " is the location (strip trailing badges).
    """
    soup = BeautifulSoup(html, "lxml")
    anchors = soup.find_all("a", href=_JOB_URL_RE)
    by_id: dict[str, list] = {}
    for a in anchors:
        m = _JOB_URL_RE.search(a.get("href", ""))
        if m:
            by_id.setdefault(m.group(1), []).append(a)

    for jid, ans in by_id.items():
        logo_a = next((a for a in ans if a.find("img")), None)
        title_a = next((a for a in ans if not a.find("img") and a.get_text(strip=True)), None)
        if not logo_a or not title_a:
            continue

        company = (logo_a.find("img").get("alt") or "").strip()
        title = title_a.get_text(" ", strip=True)
        if not company or not title:
            continue

        # Location: walk ancestors for a node whose text contains "{company} · "
        location = None
        cur = title_a
        for _ in range(8):
            cur = cur.parent
            if cur is None:
                break
            full = cur.get_text(" ", strip=True)
            if " · " in full and company in full:
                after_dot = full.split(" · ", 1)[1]
                location = _BADGE_RE.split(after_dot, maxsplit=1)[0].strip()
                break

        yield JobListing(
            source="linkedin",
            external_id=jid,
            employer=company,
            title=title,
            apply_url=_canonical_apply_url(jid),
            posting_date=None,  # not in the digest; could parse "1 day ago" badge later
            deadline=None,
            location=location,
            raw={"linkedin_job_id": jid},
        )


# ---------- Public entry point ----------


def fetch_linkedin_listings() -> list[JobListing]:
    """Public entry point. Fetches recent LinkedIn alert emails and parses them.

    Returns combined listings across all unread emails in the last 2 days,
    deduplicated by LinkedIn job_id within this run (the higher-level dedupe
    against seen-set happens in orchestrator).
    """
    messages = _gws_list_messages()
    if not messages:
        log.info("linkedin: 0 alert emails in last 2 days")
        return []

    log.info("linkedin: %d alert emails to parse", len(messages))
    by_jid: dict[str, JobListing] = {}
    for msg in messages:
        mid = msg.get("id")
        if not mid:
            continue
        html = _gws_fetch_html(mid)
        if not html:
            log.warning("linkedin: failed to fetch body for %s", mid)
            continue
        count = 0
        for listing in _parse_alert_email(html):
            # Within-run dedup: same job can appear in multiple alert emails
            if listing.external_id not in by_jid:
                by_jid[listing.external_id] = listing
                count += 1
        log.info("linkedin: msg %s — extracted %d new listings", mid, count)

    listings = list(by_jid.values())
    log.info("linkedin: %d unique listings after within-run dedup", len(listings))
    return listings
