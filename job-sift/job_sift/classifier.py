"""LLM-driven prestige + scope classification for each new listing.

Uses the Claude CLI as a subprocess (same convention as signal-brief), so we
inherit auth/quota from the operator's existing setup. Single call per listing
returns both verdicts in one structured response.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import replace
from functools import lru_cache
from textwrap import dedent

from job_sift.config import CLAUDE_BIN, JOB_SIFT_MODEL
from job_sift.profile import (
    FloorLaneConfig,
    ScopeGuardConfig,
    floor_lane_config,
    profile_block,
    scope_guard_config,
)
from job_sift.schema import ClassifierResult, JobListing

log = logging.getLogger(__name__)


_PROFILE = profile_block()

CLASSIFIER_SYSTEM_PROMPT = dedent(f"""
    You are a job-listing classifier for {_PROFILE['identity']}.
    They are looking for **{_PROFILE['seeking']}**.

    For each listing, return STRICT JSON with three fields:

    {{
      "prestige": "prestige" | "marginal" | "skip",
      "scope":    "in_scope" | "out_of_scope",
      "reason":   "<one short sentence, max 20 words>"
    }}

    PRESTIGE rules:
    - "prestige": the employer is globally recognisable in **tech, software, AI, quantitative finance, or
      adjacent engineering fields** — top AI lab (Anthropic, OpenAI, DeepMind, NVIDIA, xAI),
      top big tech (Google, Meta, Microsoft, Amazon, Apple, Bytedance, Tencent, Alibaba),
      top HFT firm (Jane Street, Citadel, Citadel Securities, Two Sigma, Optiver, Jump Trading, IMC, HRT),
      top bank tech org (Goldman Sachs, JP Morgan, Morgan Stanley, Bloomberg),
      or a clearly globally-known tech/software brand. If borderline, return "marginal" or "skip".
    - "marginal": regional but well-known, credible mid-tier tech name, or crypto/fintech that isn't top-tier;
      doesn't move the resume bullet much.
    - "skip": no-name, niche, generic HK corporate, insurance/logistics/architecture firm, or any company
      whose primary industry is fashion, luxury goods, retail, hospitality, FMCG, real estate, or
      manufacturing. Global brand recognition in a non-tech sector does NOT make a company prestige here.

    SCOPE rules — ACCEPT:
{_PROFILE['accepts']}

    REJECT:
{_PROFILE['rejects']}

    If the role type is unclear from the title (e.g. just "Software Engineer"),
    {_PROFILE['unclear_role_default']}.

    Return ONLY the JSON — no prose, no markdown fences.
""").strip()


def _build_user_prompt(listing: JobListing) -> str:
    lines = [
        f"Source: {listing.source}",
        f"Employer: {listing.employer}",
        f"Title: {listing.title}",
    ]
    if listing.location:
        lines.append(f"Location: {listing.location}")
    if listing.posting_date:
        lines.append(f"Posted: {listing.posting_date.isoformat()}")
    if listing.deadline:
        lines.append(f"Deadline: {listing.deadline.isoformat()}")
    if listing.description:
        # Truncate to keep classifier context tight. The body is mainly for
        # disambiguating scope (intern/contract/FT) on titles that don't reveal it.
        lines.append("")
        lines.append("Description:")
        lines.append(listing.description[:3000])
    return "\n".join(lines)


# Companies that should ALWAYS be skipped — domain-wrong regardless of brand fame.
# Luxury fashion, retail, hospitality, FMCG etc. Substring match, lowercase.
_PRESTIGE_HARD_SKIP_SUBSTRINGS = {
    "hermes", "hermès", "lvmh", "gucci", "prada", "chanel", "dior", "burberry",
    "louis vuitton", "tiffany", "cartier", "rolex",
}

# Companies that should ALWAYS be marginal — recognizable but not prestige-tier
# for a software/AI career. Crypto exchanges, mid-tier fintech, etc.
_PRESTIGE_HARD_MARGINAL_SUBSTRINGS = {
    "binance", "crypto.com", "okx", "bybit", "kraken", "coinbase", "kucoin",
    "huobi", "bitget",
}

# Hardcoded prestige boost: companies that should ALWAYS classify as prestige
# regardless of LLM variance. Lowercase-substring match against employer field.
# Add freely; precision-bias makes false-positives (mistakenly boosted no-name
# company) more costly than false-negatives (genuine prestige briefly demoted).
_PRESTIGE_BOOST_SUBSTRINGS = {
    # Top AI labs
    "anthropic", "openai", "deepmind", "google deepmind", "xai",
    "nvidia", "mistral", "cohere", "perplexity",
    "hugging face", "huggingface", "runway", "elevenlabs",
    # Big tech
    "google", "microsoft", "apple", "amazon", "meta", "facebook",
    "bytedance", "tencent", "alibaba", "baidu",
    # HFT
    "jane street", "citadel", "two sigma", "optiver", "jump trading",
    "hudson river trading", "imc", "hrt",
    # Tier-2 prestige with strong brand
    "stripe", "airbnb", "databricks", "scale ai", "figma",
    "bloomberg",
}


def _boost_check(employer: str) -> bool:
    """True if employer is on the hardcoded prestige boost list."""
    e = employer.lower()
    return any(needle in e for needle in _PRESTIGE_BOOST_SUBSTRINGS)


def _hard_skip_check(employer: str) -> bool:
    """True if employer is domain-wrong and should always be skipped."""
    e = employer.lower()
    return any(needle in e for needle in _PRESTIGE_HARD_SKIP_SUBSTRINGS)


def _hard_marginal_check(employer: str) -> bool:
    """True if employer is recognizable but not prestige-tier for a software/AI career."""
    e = employer.lower()
    return any(needle in e for needle in _PRESTIGE_HARD_MARGINAL_SUBSTRINGS)


def classify(listing: JobListing, *, timeout: float = 60.0) -> ClassifierResult:
    """Run one classifier pass against a listing. Returns ClassifierResult.

    Hard-skip / hard-marginal checks run before LLM to catch domain-wrong or
    non-prestige companies that the LLM might misclassify on brand recognition alone.
    Boost-list short-circuit skips the prestige LLM call for known targets.
    """
    employer = listing.employer
    if _hard_skip_check(employer):
        log.debug("hard-skip hit for %s", employer)
        return ClassifierResult(prestige="skip", scope="out_of_scope", reason="domain-wrong employer (non-tech sector)")
    if _hard_marginal_check(employer):
        log.debug("hard-marginal hit for %s", employer)
        return ClassifierResult(prestige="marginal", scope="out_of_scope", reason="crypto/non-prestige-fintech employer")
    if _boost_check(listing.employer):
        log.debug("boost-list hit for %s — running scope-only path", listing.employer)
        return classify_scope_only(listing, timeout=timeout)
    negative = negative_title(listing.title)
    if negative is not None:
        # Same free rejection the batched path applies in `_route` — kept in
        # step deliberately, because the two entry points must not disagree
        # about what the sift admits.
        log.debug("negative-title hit (%s) for %s", negative, listing.title)
        return ClassifierResult(
            prestige="skip",
            scope="out_of_scope",
            reason=f"non-technical function in title ({negative})",
        )

    user_prompt = _build_user_prompt(listing)
    cmd = [
        CLAUDE_BIN,
        "--model", JOB_SIFT_MODEL,
        "--system-prompt", CLASSIFIER_SYSTEM_PROMPT,
        "--print",
        user_prompt,
    ]
    log.debug("classifier cmd: %s", " ".join(cmd[:3]) + " ...")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("classifier timed out for %s — %s", listing.employer, listing.title)
        return ClassifierResult(prestige="skip", scope="out_of_scope", reason="classifier timeout")

    stdout = proc.stdout.strip()
    if proc.returncode != 0:
        log.warning("classifier exited %d: %s", proc.returncode, proc.stderr[:200])
        return ClassifierResult(prestige="skip", scope="out_of_scope", reason="classifier error")

    # Strip optional markdown fence the model sometimes adds despite the prompt.
    if stdout.startswith("```"):
        stdout = stdout.strip("`")
        if stdout.lower().startswith("json"):
            stdout = stdout[4:].lstrip()

    try:
        data = json.loads(stdout)
        return assign_lane(
            listing,
            ClassifierResult(
                prestige=data["prestige"],
                scope=data["scope"],
                reason=data.get("reason", ""),
            ),
        )
    except (json.JSONDecodeError, KeyError) as exc:
        log.warning("classifier returned non-JSON for %s: %s", listing.employer, stdout[:200])
        return ClassifierResult(prestige="skip", scope="out_of_scope", reason=f"parse error: {exc}")


# Scope-only path used for sources where prestige is already established by
# curation (Greenhouse with hand-picked companies). Saves one half of the LLM
# work per listing — and for sources with hundreds of listings (Anthropic has
# 389 right now), that adds up.

_SCOPE_KEYWORDS_IN = {
    "intern", "internship", "interns", "co-op", "coop",
    "summer", "winter",
    "graduate trainee", "trainee",
    "rotational", "rotation",
    "1-year contract", "one-year contract", "12-month",
}
_SCOPE_KEYWORDS_OUT = {
    "senior", "staff", "principal", "lead", "director", "vp", "head of",
    "manager,", "manager - ",
}


# ---------------------------------------------------------------------------
# Term matching
#
# Every term list in this module is matched with a leading word boundary, and a
# trailing one unless the term ends in `*`. Plain substring matching was not
# good enough once the lists grew short tokens: "ai" hits "aid", "Retail" and
# "Maintenance"; "ml" hits "HTML". A term class that fires on a third of the
# corpus is not a signal.
#
# `*` means prefix — "engineer*" covers engineer / engineers / engineering,
# "business develop*" covers development / developer — which is how a list stays
# readable without enumerating every inflection.
# ---------------------------------------------------------------------------

_BOUNDARY_BEFORE = r"(?<![a-z0-9])"
_BOUNDARY_AFTER = r"(?![a-z0-9])"


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern[str]:
    t = term.strip().lower()
    prefix = t.endswith("*")
    if prefix:
        t = t[:-1]
    return re.compile(_BOUNDARY_BEFORE + re.escape(t) + ("" if prefix else _BOUNDARY_AFTER))


def _first_term(text: str, terms) -> str | None:
    """The first term in `terms` that matches `text`, or None."""
    if not text:
        return None
    low = text.lower()
    for term in terms:
        if _term_pattern(term).search(low):
            return term
    return None


# A named monthly rate in the title — "30-50K P/M", "HK$25,000/month". In the
# observed CEDARS corpus this is a recruiter/contract posting convention, and it
# is the single strongest positive signal the floor lane has: an employer who
# publishes the rate up front is quoting for a defined engagement.
_RATE_RE = re.compile(
    r"(?<![a-z0-9])"
    r"(?:hk\$|us\$|\$)?\s*"
    r"\d[\d,]*(?:\.\d+)?\s*k?"
    r"(?:\s*(?:[-\u2013\u2014]|to)\s*\d[\d,]*(?:\.\d+)?\s*k?)?"
    r"\s*(?:p\s*/\s*m|/\s*month|per\s+month|pm(?![a-z])|monthly)",
    re.IGNORECASE,
)
# A match must look like MONEY, not like a clock. A currency symbol, a "k"
# suffix or a four-figure sum all qualify; "interviews at 3pm" does not.
_RATE_MONEY_RE = re.compile(
    r"[$\u00a3\u20ac]"      # a currency symbol
    r"|\d\s*k(?![a-z])"     # a "k" suffix \u2014 30K, 50 k
    r"|\d{4}"               # a four-figure sum \u2014 25000
    r"|\d,\d{3}",           # ...or its comma-grouped spelling \u2014 25,000
    re.IGNORECASE,
)


def named_monthly_rate(text: str) -> str | None:
    """Return the matched rate snippet if `text` quotes a monthly rate."""
    if not text:
        return None
    for m in _RATE_RE.finditer(text):
        snippet = m.group(0).strip()
        if _RATE_MONEY_RE.search(snippet):
            return snippet
    return None


def negative_title(title: str, cfg: ScopeGuardConfig | None = None) -> str | None:
    """The non-technical business function this title names, or None.

    A negative term is CANCELLED by any technical qualifier anywhere in the
    title. That carve-out is what separates "Summer Analyst" (finance, out) from
    "Technology Summer Analyst" (in), and it is applied to every negative term
    rather than to "analyst" alone — "Software Engineer, Trading Systems" and
    "Risk Engineering" are real engineering roles that a bare substring match on
    "trading" / "risk" would have discarded.
    """
    cfg = cfg or scope_guard_config()
    hit = _first_term(title, cfg.negative_titles)
    if hit is None:
        return None
    if _first_term(title, cfg.technical_qualifiers) is not None:
        return None
    return hit


def _scope_quick_classify(listing: JobListing) -> ClassifierResult | None:
    """Cheap keyword heuristic for obvious scope cases. Returns None if ambiguous.

    Returning None means "ask the LLM" — it is a CANDIDATE verdict, not an
    admission. That is the asymmetry this function is built around, and it was
    not always here: an intern/summer/contract keyword in the title used to
    return `in_scope` outright, so any title containing "Summer" at a boosted
    employer was surfaced with nothing ever asking whether the role was
    technical. Twenty of thirty-five entries in the live register were finance,
    BD and sales roles admitted exactly that way.

    So the two directions are deliberately NOT symmetric:

    - REJECTING for free is safe and stays. A negative title (sales, BD, talent
      acquisition, an unqualified "Analyst") and a seniority marker both resolve
      `out_of_scope` here with no LLM call. Being wrong costs one missed
      listing; the operator is not applying to a sales role either way.
    - ADMITTING for free is not. A false admit lands in the digest and the
      rolling register, and the whole point of the bot is to remove hand-
      scanning. So the admit direction returns None and pays for the LLM.

    Net cost is roughly a wash: the new free rejections roughly cancel the
    demoted free admits, which is why the quick path still earns its place.
    """
    title_l = listing.title.lower()

    negative = negative_title(listing.title)
    if negative is not None:
        return ClassifierResult(
            prestige="prestige",
            scope="out_of_scope",
            reason=f"non-technical function in title ({negative})",
        )

    if any(k in title_l for k in _SCOPE_KEYWORDS_OUT):
        return ClassifierResult(prestige="prestige", scope="out_of_scope", reason="title indicates senior/perm role")

    # An intern/contract keyword is a CANDIDATE only — fall through to the LLM.
    return None


SCOPE_SYSTEM_PROMPT = dedent(f"""
    You are a scope classifier for {_PROFILE['identity']}.
    The employer is already confirmed as a prestige target — you only need to
    classify SCOPE.

    Return STRICT JSON:
      {{ "scope": "in_scope" | "out_of_scope", "reason": "<one short sentence, max 20 words>" }}

    ACCEPT:
{_PROFILE['accepts']}

    REJECT:
{_PROFILE['rejects']}

    If the role type is unclear from the title (e.g. just "Software Engineer"),
    {_PROFILE['unclear_role_default']}.

    Return ONLY the JSON — no prose, no markdown fences.
""").strip()


def classify_scope_only(listing: JobListing, *, timeout: float = 60.0) -> ClassifierResult:
    """Curated-source classifier: auto-prestige, scope-only LLM check.

    Tries a cheap keyword path first; falls back to LLM for ambiguous titles.
    """
    quick = _scope_quick_classify(listing)
    if quick is not None:
        return quick

    user_prompt = _build_user_prompt(listing)
    cmd = [
        CLAUDE_BIN,
        "--model", JOB_SIFT_MODEL,
        "--system-prompt", SCOPE_SYSTEM_PROMPT,
        "--print",
        user_prompt,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return ClassifierResult(prestige="prestige", scope="out_of_scope", reason="scope-classifier timeout")

    stdout = proc.stdout.strip()
    if proc.returncode != 0:
        return ClassifierResult(prestige="prestige", scope="out_of_scope", reason="scope-classifier error")

    if stdout.startswith("```"):
        stdout = stdout.strip("`")
        if stdout.lower().startswith("json"):
            stdout = stdout[4:].lstrip()

    try:
        data = json.loads(stdout)
        return ClassifierResult(
            prestige="prestige",  # source-curated
            scope=data["scope"],
            reason=data.get("reason", ""),
        )
    except (json.JSONDecodeError, KeyError):
        return ClassifierResult(prestige="prestige", scope="out_of_scope", reason="scope parse error")


# ---------------------------------------------------------------------------
# The floor lane
#
# The prestige lane answers "would this employer move the resume bullet?", and
# it was the only question the sift asked. Over 87 digests that discarded 269
# listings which were IN SCOPE and failed on brand alone — a staffing firm
# advertising three AI platform engineers on a 12-month contract with the
# monthly rate printed in the title, a university hiring a temporary research
# assistant in data science, a small shop wanting a junior automation engineer
# on a rolling contract. None of them are resume bullets. All of them are paid
# technical work available now, which is a different thing the operator also
# wants, and the strict-prestige heuristic had no way to express it.
#
# So the floor lane runs in PARALLEL and asks a brand-agnostic question:
# technical, reachable, and a short/flexible engagement. It does not soften the
# prestige lane by one point — that lane is untouched — and the two render under
# separate headings so a floor match can never be mistaken for a prestige one.
#
# Deliberately looser than the prestige lane. A false positive here costs one
# extra line under a clearly-labelled heading; a false negative costs a job the
# operator could actually have taken.
# ---------------------------------------------------------------------------


def floor_reason(listing: JobListing, cfg: FloorLaneConfig | None = None) -> str | None:
    """Why this listing qualifies for the floor lane, or None if it does not.

    Three criteria, all required:

    (a) TECHNICAL — judged on the title alone. A sales listing whose body
        mentions "you'll work with our engineering team" is still a sales
        listing, so the description does not get a vote here. A title naming a
        non-technical business function (see `negative_title`) is disqualified
        outright, whatever else it says.
    (b) REACHABLE — the location matches the operator's configured geography,
        or the listing carries no location at all. Empty-location passes
        because that is already the convention upstream in the ATS adapters:
        an unstated location is a question for the human, not a rejection.
    (c) FLEXIBLE ENGAGEMENT — part-time / contract / rolling / temporary / RA,
        read from the title OR the body, since a title like "AI &
        Bioinformatics" often leaves the shape to the body.

    A named monthly rate in the title satisfies (c) on its own. That is a
    judgement call and worth stating: a permanent role can quote a monthly
    salary too. But in the corpus this lane was built from, a rate in the TITLE
    is a recruiter-posting convention for a defined engagement, and it is the
    one signal that reliably separates the contract listings a brand filter was
    throwing away. It is only ever an admission to the floor lane, never to the
    prestige one.
    """
    cfg = cfg or floor_lane_config()
    if not cfg.active:
        return None

    title = listing.title or ""

    # (a) technical
    if negative_title(title) is not None:
        return None
    tech = _first_term(title, cfg.technical_terms)
    if tech is None:
        return None

    # (b) reachable
    location = (listing.location or "").strip()
    if location and _first_term(location, cfg.locations) is None:
        return None

    # (c) flexible engagement
    rate = named_monthly_rate(title)
    body = f"{title}\n{listing.description or ''}"[:4000]
    engagement = _first_term(body, cfg.engagement_terms)
    if engagement is None and rate is None:
        return None

    shape = engagement or "named monthly rate"
    detail = f", {rate}" if rate else ""
    where = location or "location unstated"
    return f"floor lane: technical ({tech}) · {where} · {shape}{detail}"


def assign_lane(
    listing: JobListing, result: ClassifierResult, cfg: FloorLaneConfig | None = None
) -> ClassifierResult:
    """Stamp the admission lane onto a verdict. Idempotent.

    THIS IS THE OVERLAP RULE, and it is the reason a listing can never be
    rendered twice. The two lanes genuinely overlap — a prestige employer
    offering a contract role satisfies both — so `lane` is a single value
    assigned by precedence rather than a set of tags:

        prestige wins.

    A listing the prestige lane already admits stays in the prestige lane and
    is never re-examined here. Only a listing prestige REJECTED (marginal or
    skip) is offered to the floor lane. Rendering then partitions on `lane`, so
    every surfaced listing lands under exactly one heading.

    Prestige-wins rather than floor-wins because the two lanes are not equal in
    what they claim. "Anthropic is hiring" is strictly more informative than
    "someone in Hong Kong wants a contractor", and demoting it into the floor
    section would bury the signal the digest exists to deliver.
    """
    if result.scope != "in_scope":
        return result  # out of scope is out of scope in both lanes
    if result.prestige == "prestige":
        return result  # prestige lane already has it
    if result.lane == "floor":
        return result  # already stamped; re-stamping would duplicate the reason
    reason = floor_reason(listing, cfg)
    if reason is None:
        return result
    combined = f"{result.reason} · {reason}" if result.reason else reason
    return replace(result, lane="floor", reason=combined)


# ---------------------------------------------------------------------------
# Batched classification
#
# The single-listing path above spawns one `claude` CLI per listing. The CLI
# cold-start (~10-15s) dominates, so N listings = N × cold-start, which blew the
# service's 600s timeout once the daily backlog crossed ~30 (and never delivered
# / never advanced state → doom loop). Batching collapses the LLM work into one
# CLI call per chunk of ~20, turning ~10min into ~30s and surviving a CEDARS flood.
#
# Routing is identical to _classify_one in the orchestrator: cheap heuristics
# resolve most listings for free; the rest split into a full (prestige+scope)
# bucket and a scope-only (curated/boosted source) bucket.
# ---------------------------------------------------------------------------

# Sources whose curation already implies prestige (mirror of orchestrator's set).
_AUTO_PRESTIGE_SOURCES: set[str] = {"greenhouse", "lever", "ashby"}

# Listings per LLM call. Keeps output JSON small enough to stay valid and bounds
# the worst-case run time under a flood. 8 chunks for 150 listings ≈ ~100s.
_BATCH_CHUNK_SIZE = 20


def _coerce(prestige: str, scope: str, reason: str) -> ClassifierResult:
    """Clamp model output to valid enum values (precision-bias defaults)."""
    if prestige not in ("prestige", "marginal", "skip"):
        prestige = "skip"
    if scope not in ("in_scope", "out_of_scope"):
        scope = "out_of_scope"
    return ClassifierResult(prestige=prestige, scope=scope, reason=reason or "")


def _route(listing: JobListing) -> tuple[ClassifierResult | None, str]:
    """Apply the cheap heuristics. Returns (result, route).

    route is one of:
      'done'  — result is fully resolved without an LLM call
      'full'  — needs the prestige+scope LLM pass
      'scope' — needs the scope-only LLM pass (prestige forced to 'prestige')
    """
    auto_prestige = listing.source in _AUTO_PRESTIGE_SOURCES

    if not auto_prestige:
        employer = listing.employer
        if _hard_skip_check(employer):
            return ClassifierResult("skip", "out_of_scope", "domain-wrong employer (non-tech sector)"), "done"
        if _hard_marginal_check(employer):
            return ClassifierResult("marginal", "out_of_scope", "crypto/non-prestige-fintech employer"), "done"
        if _boost_check(employer):
            auto_prestige = True  # known prestige → fall through to scope-only routing

    if auto_prestige:
        quick = _scope_quick_classify(listing)
        if quick is not None:
            return quick, "done"
        return None, "scope"

    # The scope guard is not a property of prestige, so it applies to the full
    # lane as well: a sales or talent-acquisition title is out of scope whoever
    # posted it, and paying for an LLM call to be told so is waste. This is
    # where most of the cost that the demoted keyword-admits gave up comes back
    # — the full lane is the busy one (CEDARS + LinkedIn).
    negative = negative_title(listing.title)
    if negative is not None:
        return (
            ClassifierResult(
                "skip", "out_of_scope", f"non-technical function in title ({negative})"
            ),
            "done",
        )

    return None, "full"


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _extract_json_array(text: str) -> list | None:
    """Tolerantly pull the first JSON array out of model output."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _batch_system(base: str, *, scope_only: bool) -> str:
    fmt = (
        '{"i": <index>, "scope": "in_scope"|"out_of_scope", "reason": "<≤20 words>"}'
        if scope_only
        else '{"i": <index>, "prestige": "prestige"|"marginal"|"skip", "scope": "in_scope"|"out_of_scope", "reason": "<≤20 words>"}'
    )
    return base + "\n\n" + dedent(
        f"""
        BATCH MODE: You are given a numbered list of listings, each headed by [i].
        Classify EVERY listing. Return ONLY a JSON array, one object per listing:
          {fmt}
        Include each index 0..N-1 exactly once, in order. No prose, no markdown fences.
        """
    ).strip()


def _batch_user(listings: list[JobListing]) -> str:
    return "\n\n".join(f"[{i}]\n{_build_user_prompt(L)}" for i, L in enumerate(listings))


def _batch_llm(
    listings: list[JobListing], base_prompt: str, *, scope_only: bool, timeout: float = 180.0
) -> list[ClassifierResult]:
    """One CLI call classifying all `listings`. Returns results aligned to input order."""
    n = len(listings)
    if n == 0:
        return []
    default = (
        ClassifierResult("prestige", "out_of_scope", "scope batch fallback")
        if scope_only
        else ClassifierResult("skip", "out_of_scope", "batch fallback")
    )
    cmd = [
        CLAUDE_BIN,
        "--model", JOB_SIFT_MODEL,
        "--system-prompt", _batch_system(base_prompt, scope_only=scope_only),
        "--print",
        _batch_user(listings),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        log.warning("batch classifier timed out for %d listings", n)
        return [default] * n
    if proc.returncode != 0:
        log.warning("batch classifier exited %d: %s", proc.returncode, proc.stderr[:200])
        return [default] * n

    arr = _extract_json_array(proc.stdout)
    if arr is None:
        log.warning("batch classifier returned non-array: %s", proc.stdout[:200])
        return [default] * n

    out: list[ClassifierResult] = [default] * n
    for obj in arr:
        if not isinstance(obj, dict):
            continue
        try:
            i = int(obj["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i < n):
            continue
        prestige = "prestige" if scope_only else str(obj.get("prestige", "skip"))
        out[i] = _coerce(prestige, str(obj.get("scope", "out_of_scope")), str(obj.get("reason", "")))
    return out


def classify_batch(listings: list[JobListing]) -> list[ClassifierResult]:
    """Classify many listings with ≤1 LLM call per chunk per route.

    Drop-in replacement for calling _classify_one in a loop. Heuristics resolve
    most listings for free; the rest are batched into the full / scope-only paths.
    Returns one ClassifierResult per input listing, in order.
    """
    results: list[ClassifierResult | None] = [None] * len(listings)
    full_idx: list[int] = []
    scope_idx: list[int] = []

    for i, listing in enumerate(listings):
        res, route = _route(listing)
        if route == "done":
            results[i] = res
        elif route == "full":
            full_idx.append(i)
        else:
            scope_idx.append(i)

    for bucket, base, scope_only in (
        (full_idx, CLASSIFIER_SYSTEM_PROMPT, False),
        (scope_idx, SCOPE_SYSTEM_PROMPT, True),
    ):
        for chunk in _chunks(bucket, _BATCH_CHUNK_SIZE):
            verdicts = _batch_llm([listings[i] for i in chunk], base, scope_only=scope_only)
            for j, i in enumerate(chunk):
                results[i] = verdicts[j]

    # Lane assignment is the LAST step, and it runs over every verdict —
    # heuristic and LLM alike — so there is exactly one place that decides which
    # heading a listing renders under. See `assign_lane` for the overlap rule.
    return [
        assign_lane(listing, r if r is not None else ClassifierResult("skip", "out_of_scope", "unclassified"))
        for listing, r in zip(listings, results)
    ]
