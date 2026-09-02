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

    CLOSED   the posting page came back 200 and says so, in those words
    OPEN     the posting page came back 200 and does not
    UNKNOWN  anything else at all

UNKNOWN is the default for every unhappy path — a transport error, a timeout, a
403 behind the auth wall, a 429, a 5xx, an empty or stub-sized body, a redirect
chain that ends up anywhere other than a posting page — and for a 404. That last
one looks like a judgement call and is not, and it is worth writing down what
was actually checked rather than assumed: a genuinely closed posting answers
**200 with the banner** (verified against three real closed rows), while 404 is
what a nonexistent job id returns. 404 is not LinkedIn's expiry signal, so
reading it as one would only ever delete rows we could not look at.

TWO GUARDS EARN THEIR KEEP HERE, both found by executing this against the real
site rather than by reasoning about it:

1. THE VERDICT MUST COME OFF THE POSTING ITSELF. LinkedIn 301s an unknown job
   id to a company jobs-index page on a different host — observed:
   `/jobs/view/3500000000/` → `br.linkedin.com/jobs/escale-vagas?trk=
   expired_jd_redirect` — and that page carries expiry-flavoured prose. Reading
   a verdict off it retires a live role permanently. So the terminal URL is
   checked, and anything that did not land on a `/jobs/view/` page is UNKNOWN.
   This is also what makes the "a redirect is UNKNOWN" claim true: without it a
   302 to the auth wall returned OPEN and bought a week of silence off a request
   that never saw the posting.

2. ONE MARKER, NOT THREE. The earlier list also held "this job is no longer
   available" and the bare "no longer available". The bare one subsumed the
   other (making it dead code) and contributed ZERO true positives — all three
   real closed rows match "no longer accepting applications" on its own — while
   matching ordinary LinkedIn error prose: "This page is no longer available",
   "Sorry, this content is no longer available", "That profile is no longer
   available on LinkedIn". Every one of those would have retired a live role.
   A marker that catches nothing real and plenty unreal is not a marker.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

CLOSED = "closed"
OPEN = "open"
UNKNOWN = "unknown"

# EXACTLY ONE marker, and see guard 2 in the module docstring before adding a
# second. Matched against the casefolded page, so it must stay lowercase.
_CLOSED_MARKER = "no longer accepting applications"

# The path every real posting page carries. A terminal URL without it is not a
# posting, whatever it says about itself.
_POSTING_PATH = "/jobs/view/"

# Enough of a page to have been a page. A 200 with a two-line body is a
# redirect stub or an interstitial, not a posting — and short bodies are
# exactly where generic error prose lives, so this is checked FIRST, before
# any marker gets a chance to fire on it.
_MIN_BODY_CHARS = 200

_TIMEOUT_S = 10.0
# httpx's own ceiling is 20. A posting page needs at most a couple of hops, and
# `_TIMEOUT_S` is per socket operation rather than per request, so the redirect
# count is what actually bounds a probe's worst case — 20 hops at 10s each is
# 200 seconds for ONE check. See `orchestrator._liveness_pass` for the
# wall-clock budget over the pass as a whole.
_MAX_REDIRECTS = 3
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


def is_posting_url(url: str | None) -> bool:
    """True only for a URL that is still a LinkedIn posting page.

    Host and path are both checked: the observed bad redirect lands on
    `br.linkedin.com`, which IS a linkedin host, so the host alone would have
    let it through — and a `/jobs/view/` path on some other domain is not ours.
    """
    if not url:
        return False
    try:
        parsed = httpx.URL(url)
    except Exception:  # noqa: BLE001 — an unparseable URL is not a posting
        return False
    host = (parsed.host or "").casefold()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return False
    return _POSTING_PATH in parsed.path


def classify_page(html: str | None) -> str:
    """Read one fetched posting body. Pure — no I/O, so it is directly testable.

    THE LENGTH GUARD RUNS BEFORE THE MARKER, deliberately. The other order let
    `classify_page("no longer available")` return CLOSED off nineteen
    characters, which made `_MIN_BODY_CHARS` inert against precisely the bodies
    it exists to reject: short ones carrying a generic error string.
    """
    if not html or len(html.strip()) < _MIN_BODY_CHARS:
        return UNKNOWN
    if _CLOSED_MARKER in html.casefold():
        return CLOSED
    return OPEN


def _get(url: str, *, transport: httpx.BaseTransport | None = None) -> tuple[int, str, str]:
    """One GET. Returns `(status, terminal_url, body)`.

    The TERMINAL url is returned, not the requested one — the caller has to be
    able to tell that a redirect took it somewhere else. `transport` is injected
    so tests can drive this function for real against `httpx.MockTransport`
    instead of monkeypatching it away; the redirect handling is the part that
    went wrong, and stubbing `_get` out is what hid that.
    """
    with httpx.Client(
        follow_redirects=True,
        max_redirects=_MAX_REDIRECTS,
        timeout=_TIMEOUT_S,
        headers=_HEADERS,
        transport=transport,
    ) as client:
        resp = client.get(url)
        return resp.status_code, str(resp.url), resp.text


def probe_linkedin(job_id: str, *, transport: httpx.BaseTransport | None = None) -> str:
    """Check one LinkedIn posting. Returns CLOSED / OPEN / UNKNOWN.

    Never raises. Every failure mode collapses to UNKNOWN, because the caller
    treats UNKNOWN as "no change to the register" and that is what an
    unanswered question deserves.
    """
    try:
        status, final_url, body = _get(job_url(job_id), transport=transport)
    except Exception as exc:  # noqa: BLE001 — every transport failure is UNKNOWN
        log.info("liveness: could not reach linkedin:%s (%s)", job_id, type(exc).__name__)
        return UNKNOWN
    if status != 200:
        log.info("liveness: linkedin:%s answered HTTP %s — treating as unknown", job_id, status)
        return UNKNOWN
    if not is_posting_url(final_url):
        # Deliberately does not log the query string: LinkedIn hangs tracking
        # params off these and there is no reason to write them down.
        log.info(
            "liveness: linkedin:%s redirected off the posting page (%s) — treating as unknown",
            job_id,
            httpx.URL(final_url).copy_with(query=None, fragment=None),
        )
        return UNKNOWN
    verdict = classify_page(body)
    log.info("liveness: linkedin:%s → %s", job_id, verdict)
    return verdict
