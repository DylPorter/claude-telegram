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

from job_sift.errors import SourceAuthError, SourceFetchError
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

    Returns [] ONLY for a genuinely empty result. Every path where we could not
    ask — gws missing, gws timing out, a non-zero exit, an unparseable
    response — raises, because "I could not look" and "there was nothing" must
    not reach the digest as the same empty list.

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
        # NOT an empty inbox — we never got to ask. Escalate, or source_health
        # scores this run as a success and resets the failure streak.
        log.warning("linkedin: gws unavailable or timed out: %s", exc)
        raise SourceFetchError(
            "linkedin", f"gws is unavailable or timed out ({type(exc).__name__}) — is the CLI installed and on PATH?"
        ) from exc
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
        # Deliberately does NOT echo `err` into the raised message: that
        # string reaches Telegram and the on-disk state file, and gws stderr is
        # not guaranteed to be free of token material. It is already in the
        # journal for whoever is debugging.
        log.warning("linkedin: gws list failed: %s", err)
        raise SourceFetchError(
            "linkedin",
            f"gws Gmail list failed (exit {proc.returncode}) — see `journalctl --user -u job-sift` for the stderr",
        )
    try:
        return json.loads(proc.stdout).get("messages", []) or []
    except (json.JSONDecodeError, AttributeError) as exc:
        log.warning("linkedin: gws returned non-JSON")
        raise SourceFetchError("linkedin", "gws Gmail list returned a non-JSON response") from exc


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

    Thin wrapper over `_parse_alert_email_detailed` — see there for the card
    count the total-failure guard needs. Kept because "iterate the listings" is
    what almost every caller wants.
    """
    yield from _parse_alert_email_detailed(html)[0]


def _parse_alert_email_detailed(html: str) -> tuple[list[JobListing], int]:
    """Parse one digest email. Returns `(listings, job_cards_offered)`.

    THE SECOND VALUE IS THE POINT, and it is what makes a template change
    reportable. `listings` alone cannot answer "was there nothing in this email,
    or did I fail to read it?" — both are an empty list, and this source is the
    one issue #1c depends on. `job_cards` counts the distinct `/jobs/view/{id}`
    anchors the email contained, which is evidence about the EMAIL rather than
    about this parser:

      * cards == 0             → the email offered no job at all. LEGITIMATE:
                                 LinkedIn's "your job alert has been created"
                                 confirmations come from the same senders and
                                 carry no job links (see the module docstring).
      * cards > 0, listings 0  → the email IS a digest, and not one of its cards
                                 survived the logo/title selectors. That is a
                                 parse failure, and `fetch_linkedin_listings`
                                 escalates it instead of returning a quiet [].

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

    listings: list[JobListing] = []
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

        listings.append(
            JobListing(
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
        )

    return listings, len(by_id)


# ---------- Public entry point ----------


def fetch_linkedin_listings() -> list[JobListing]:
    """Public entry point. Fetches recent LinkedIn alert emails and parses them.

    Returns combined listings across all unread emails in the last 2 days,
    deduplicated by LinkedIn job_id within this run (the higher-level dedupe
    against seen-set happens in orchestrator).

    Raises `SourceAuthError` when the OAuth token is dead and `SourceFetchError`
    when gws could not be asked at all — an empty return means the mailbox was
    genuinely empty.

    TWO TOTAL-FAILURE GUARDS, NOT ONE, because there are two halves that can
    die independently. The transport half — gws, auth, the message bodies — has
    been hardened since day one. The PARSER half had not been, and it is just as
    load-bearing: LinkedIn owns this email template and can change it whenever
    it likes. Before this, a template change meant every selector missed,
    `unreadable` stayed at 0 so the guard below never fired, and the adapter
    returned `[]` → `succeeded` → failure streak reset, `last_success` stamped
    today. A dead parser scored as a healthy quiet day, in the one source issue
    #1c depends on, which is the identical shape as the fifty-day CEDARS outage.

    So: reading the emails and finding no jobs is only believed when the emails
    genuinely contained no job cards. See `_parse_alert_email_detailed` for the
    card count that separates the two, and for the residual — an email whose
    job-link URLs changed shape entirely reads as a confirmation email and is
    still a quiet zero. Closing that would need a positive test for "this is a
    digest" that does not depend on the link format; the card count closes the
    realistic case (selector drift) without guessing.
    """
    messages = _gws_list_messages()
    if not messages:
        log.info("linkedin: 0 alert emails in last 2 days")
        return []

    log.info("linkedin: %d alert emails to parse", len(messages))
    by_jid: dict[str, JobListing] = {}
    unreadable = 0
    readable = 0
    carded = 0        # readable emails that offered at least one job card
    yielded = 0       # ...of those, the ones at least one card parsed out of
    for msg in messages:
        mid = msg.get("id")
        if not mid:
            continue
        html = _gws_fetch_html(mid)
        if not html:
            log.warning("linkedin: failed to fetch body for %s", mid)
            unreadable += 1
            continue
        readable += 1
        parsed, cards = _parse_alert_email_detailed(html)
        if cards:
            carded += 1
            if parsed:
                yielded += 1
            else:
                log.error(
                    "linkedin: msg %s offered %d job card(s) and NONE parsed — "
                    "the alert-email template has probably changed",
                    mid, cards,
                )
        else:
            log.info(
                "linkedin: msg %s has no job cards — confirmation email, or a "
                "changed link format",
                mid,
            )
        count = 0
        # Within-run dedup: same job can appear in multiple alert emails. Note
        # this counts NEW ids, so it is not a parse-health signal — `parsed` is.
        for listing in parsed:
            if listing.external_id not in by_jid:
                by_jid[listing.external_id] = listing
                count += 1
        log.info(
            "linkedin: msg %s — %d job card(s), %d parsed, %d new",
            mid, cards, len(parsed), count,
        )

    if unreadable and unreadable == len(messages):
        raise SourceFetchError(
            "linkedin",
            f"listed {len(messages)} alert email(s) but could not read the body of any of them",
        )

    if carded and yielded == 0:
        raise SourceFetchError(
            "linkedin",
            f"read {readable} alert email(s), {carded} of them carrying job "
            f"cards, and parsed ZERO listings out of any of them — the digest "
            f"template has changed; re-check _parse_alert_email_detailed's "
            f"logo/title selectors against a fresh sample",
        )

    listings = list(by_jid.values())
    log.info("linkedin: %d unique listings after within-run dedup", len(listings))
    return listings
