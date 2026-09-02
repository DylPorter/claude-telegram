"""Is this posting still taking applications?

Issue #1c. LinkedIn alert emails carry no deadline and list a posting exactly
once, so nothing in the register ever moved a LinkedIn row off `open` except the
30-day "haven't seen it in a while" rule — and that rule is measuring the wrong
thing, because a LinkedIn row is never seen twice by design. A posting that shut
two days after it was mailed sat in the register as open for a month.

WHY A LIVENESS CHECK RATHER THAN PARSING A DEADLINE OUT OF THE JD. The other
option was to read a closing date out of the job description. It loses on both
counts. Cost: the digest email has no description at all, so extracting one
means fetching the posting page — the same request this module makes — and then
mining prose on top. Coverage: most LinkedIn JDs state no deadline whatsoever,
so even a perfect parser would leave the majority of rows exactly as they are,
including every row that closes early because the req was filled. The closed
banner, by contrast, appears on precisely the rows that need retiring, and it
says what we actually want to know rather than what someone predicted in March.

EVERYTHING HERE FAILS SAFE, and that is the whole design. Three verdicts:

    CLOSED   the page came back 200 and says so, in those words
    OPEN     the page came back 200 and does not
    UNKNOWN  anything else at all

UNKNOWN is the default for every unhappy path — a transport error, a timeout, a
redirect, a 403 behind the auth wall, a 429, a 5xx, an empty body. It is also
the verdict for a 404, which is the one that looks like a judgement call and is
not: LinkedIn 404s a removed posting, but it also 404s a geo bounce and a
consent redirect, and a wrong "closed" silently deletes a live job from the
register. Only the banner closes a row. A failed request is not evidence about
a job.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

CLOSED = "closed"
OPEN = "open"
UNKNOWN = "unknown"

# LinkedIn's own wording on a guest job view, plus the variants it has used.
# Matched against the casefolded page, so these must stay lowercase.
_CLOSED_MARKERS = (
    "no longer accepting applications",
    "this job is no longer available",
    "no longer available",
)

# Enough of a page to have been a page. A 200 with a two-line body is a
# redirect stub or an interstitial, not a posting.
_MIN_BODY_CHARS = 200

_TIMEOUT_S = 10.0
_HEADERS = {
    # A plain default UA gets an immediate interstitial. This is not an attempt
    # to defeat anything: a blocked request just returns UNKNOWN and the row is
    # left alone, which is the same outcome as not asking.
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def classify_page(html: str | None) -> str:
    """Read one fetched page body. Pure — no I/O, so it is directly testable.

    A body we could not make sense of is UNKNOWN, never CLOSED.
    """
    if not html or len(html.strip()) < 1:
        return UNKNOWN
    text = html.casefold()
    if any(marker in text for marker in _CLOSED_MARKERS):
        return CLOSED
    if len(html.strip()) < _MIN_BODY_CHARS:
        return UNKNOWN
    return OPEN


def _get(url: str) -> tuple[int, str]:
    """One GET. Split out so tests can replace the transport without a socket."""
    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT_S, headers=_HEADERS) as client:
        resp = client.get(url)
        return resp.status_code, resp.text


def probe_linkedin(job_id: str) -> str:
    """Check one LinkedIn posting. Returns CLOSED / OPEN / UNKNOWN.

    Never raises. Every failure mode collapses to UNKNOWN, because the caller
    treats UNKNOWN as "no change to the register" and that is what an
    unanswered question deserves.
    """
    try:
        status, body = _get(job_url(job_id))
    except Exception as exc:  # noqa: BLE001 — every transport failure is UNKNOWN
        log.info("liveness: could not reach linkedin:%s (%s)", job_id, type(exc).__name__)
        return UNKNOWN
    if status != 200:
        log.info("liveness: linkedin:%s answered HTTP %s — treating as unknown", job_id, status)
        return UNKNOWN
    verdict = classify_page(body)
    log.info("liveness: linkedin:%s → %s", job_id, verdict)
    return verdict
