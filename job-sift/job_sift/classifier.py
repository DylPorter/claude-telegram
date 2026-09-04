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
from job_sift.tags import clean_bool, clean_tag, derive_role_type

log = logging.getLogger(__name__)


_PROFILE = profile_block()

CLASSIFIER_SYSTEM_PROMPT = dedent(f"""
    You are a job-listing classifier for {_PROFILE['identity']}.
    They are looking for **{_PROFILE['seeking']}**.

    For each listing, return STRICT JSON with five fields:

    {{
      "prestige":     "prestige" | "marginal" | "skip",
      "scope":        "in_scope" | "out_of_scope",
      "reason":       "<one short sentence, max 20 words>",
      "industry":     "<1-3 words naming the EMPLOYER's industry, or null>",
      "is_technical": true | false | null
    }}

    `industry` and `is_technical` are TAGS, not filters — they never decide
    whether a listing is kept, only how a reader can slice the board later.
    So answer them honestly and use null when you genuinely cannot tell from
    the employer and title. A guess is worse than a null here: null shows up
    as "untagged" and stays visible, a wrong guess files the role under a
    label the reader may have filtered away.

    `industry` names what the EMPLOYER does (e.g. "investment banking",
    "semiconductors", "university", "recruitment agency"), not what the role
    does. `is_technical` is about the ROLE: true for engineering, software,
    data, research and quantitative work; false for sales, marketing, HR,
    admin, legal and general business functions.

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


def _employer_gated_result(prestige: str, reason: str, listing: JobListing) -> ClassifierResult:
    """Free-rejection verdict for an employer the prestige lane excludes on
    brand/domain grounds (hard-skip / hard-marginal).

    `scope` here used to be hardcoded to `out_of_scope` — a convenience,
    because nothing downstream cared what it said as long as `prestige`
    wasn't "prestige". That stopped being harmless once `assign_lane` started
    reading `scope` to decide floor-lane eligibility: a prestige/domain
    opinion about the EMPLOYER was being recorded as a scope verdict about
    the ROLE, and silently vetoed the floor lane for it — Coinbase, Binance,
    Hermes and every other hard-skip/hard-marginal employer never reached
    `floor_reason` even when the listing itself was a perfectly good
    technical/contract match. Issue #2 wants the floor lane to run
    "regardless of employer brand", which has to include these employers too.

    So scope is now decided the same free way the full LLM lane's no-LLM
    branch decides it in `_route` — `negative_title` only. A non-technical
    title is still out_of_scope for both lanes. A title with no free reason
    to reject stays a scope CANDIDATE (`in_scope`), not a confirmed yes — but
    that is exactly the epistemic state `assign_lane` already expects to
    hand to `floor_reason`, which does its own independent technical /
    reachable / engagement check before admitting anything. Prestige stays
    exactly "skip"/"marginal" either way, so the prestige lane's behaviour
    for these employers is unchanged.
    """
    negative = negative_title(listing.title)
    if negative is not None:
        return ClassifierResult(
            prestige, "out_of_scope", f"{reason}; non-technical function in title ({negative})"
        )
    return ClassifierResult(prestige, "in_scope", reason)


def classify(listing: JobListing, *, timeout: float = 60.0) -> ClassifierResult:
    """Run one classifier pass against a listing. Returns ClassifierResult.

    Hard-skip / hard-marginal checks run before LLM to catch domain-wrong or
    non-prestige companies that the LLM might misclassify on brand recognition alone.
    Boost-list short-circuit skips the prestige LLM call for known targets.
    """
    employer = listing.employer
    if _hard_skip_check(employer):
        log.debug("hard-skip hit for %s", employer)
        return assign_lane(
            listing, _employer_gated_result("skip", "domain-wrong employer (non-tech sector)", listing)
        )
    if _hard_marginal_check(employer):
        log.debug("hard-marginal hit for %s", employer)
        return assign_lane(
            listing, _employer_gated_result("marginal", "crypto/non-prestige-fintech employer", listing)
        )
    if _boost_check(listing.employer):
        log.debug("boost-list hit for %s — running scope-only path", listing.employer)
        return classify_scope_only(listing, timeout=timeout)
    negative = negative_title(listing.title)
    if negative is not None:
        # Same free rejection the batched path applies in `_route` — kept in
        # step deliberately, because the two entry points must not disagree
        # about what the sift admits.
        log.debug("negative-title hit (%s) for %s", negative, listing.title)
        return assign_lane(
            listing,
            ClassifierResult(
                prestige="skip",
                scope="out_of_scope",
                reason=f"non-technical function in title ({negative})",
            ),
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
                industry=clean_tag(data.get("industry")),
                is_technical=clean_bool(data.get("is_technical")),
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


def _leftmost_match(text: str, terms) -> tuple[str, int] | None:
    """The term whose match starts EARLIEST in `text`, and that position.

    Unlike `_first_term` (first in LIST order), this is first in STRING
    order — needed anywhere a term's *position relative to another match*
    is what decides the verdict, not just whether it matches at all. See
    `_negative_title_no_subject_rescue`.
    """
    if not text:
        return None
    low = text.lower()
    best: tuple[str, int] | None = None
    for term in terms:
        m = _term_pattern(term).search(low)
        if m is not None and (best is None or m.start() < best[1]):
            best = (term, m.start())
    return best


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


def _negative_title_no_subject_rescue(title: str, cfg: ScopeGuardConfig | None = None) -> str | None:
    """The floor lane's own, STRICTER negative-title check. Not the same
    guarantee as `negative_title` — read both docstrings before touching
    either.

    `negative_title`'s qualifier rescue is safe everywhere else it runs
    (`_scope_quick_classify`, `_route`'s full-lane guard): escaping it only
    trades a free reject for a paid LLM call, never a false admit, because
    an LLM verdict still sits downstream. `floor_reason` has no LLM
    downstream — its criterion (a) IS the final word — and
    `technical_qualifiers` / `technical_terms` deliberately share most of
    their vocabulary (`engineer*`, `software`, `data scien*`, …), so the
    same word that rescues a negative title here also satisfies the
    technical criterion two lines later. One word doing double duty as both
    "not really a sales role" and "is technical" is how "Sales Executive,
    Software Solutions", "Business Development Manager, Software" and
    "Recruitment Consultant, Software Engineering" were admitting to the
    floor lane: none of them are technical roles, they just mention a
    technical PRODUCT or department after naming a non-technical one.

    The distinguishing fact in every genuine case ("Software Engineer,
    Trading Systems", "Risk Engineering Intern") is that the qualifier IS
    (or leads) the role's own head noun — it appears AT OR BEFORE the
    negative term, not trailing after it as a subject-matter descriptor. So:
    a qualifier rescues here only if its leftmost match starts at or before
    the negative term's leftmost match. "Software Engineer, Trading
    Systems" — qualifier at position 0, negative ("trading") later — still
    rescues. "Sales Executive, Software Solutions" — negative ("sales") at
    position 0, qualifier later — does not.

    THE RULE IS ONE-DIRECTIONAL, AND THE COST IS A FALSE POSITIVE, NOT ONLY A
    FALSE NEGATIVE. Reverse the word order on the very titles above and they
    admit again, because position is all this looks at: "Software Sales
    Executive (Contract)", "Technology Sales Manager, Part Time" and
    "Engineering Recruitment Consultant (Contract)" all reach the floor lane —
    verified by execution — while "Sales Executive, Software Solutions
    (Contract)" and "Business Development Manager, Software (Contract)" do not.
    Same roles, opposite verdicts, decided by which noun the recruiter put
    first.

    Left as-is on purpose. The pre-positional rule admitted BOTH orderings, so
    this is strictly an improvement and never a regression, and the alternative
    — demanding the qualifier be the head noun — needs real grammar, not offsets.
    But do not read the paragraph above as a guarantee: it says a leading
    qualifier is USUALLY the head noun, and "Software Sales Executive" is the
    counter-example it does not catch.

    The symmetric false NEGATIVE is real too and documented in the same spirit:
    "Analyst Programmer (Contract)" is a genuine software title that this
    rejects, negative ("analyst") at position 0 and the qualifier trailing.
    """
    cfg = cfg or scope_guard_config()
    neg = _leftmost_match(title, cfg.negative_titles)
    if neg is None:
        return None
    _, neg_pos = neg
    qual = _leftmost_match(title, cfg.technical_qualifiers)
    if qual is not None and qual[1] <= neg_pos:
        return None
    return neg[0]


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

    This function is only reached at all for a boosted/auto-prestige
    employer — most of a real day's titles never touch it. Measured
    correctly (through `_route`, not this function called in isolation as
    if every title were boosted — an earlier draft made that mistake and
    reported a cost increase that wasn't real), the free-resolution rate
    goes UP on both a small mixed corpus and a real 45-entry register; see
    README "Two admission lanes" for the numbers. Most of that saving
    actually comes from the OTHER half of this change — the negative-title
    guard `_route` applies to non-boosted employers too, where most of a
    real day's volume lives. The quick path still earns its place for a
    cheaper reason than "net win, measured": rejecting for free is safe
    regardless of the net, because a missed listing costs nothing the
    operator would have acted on anyway, while every dollar spent asking the
    LLM about a title this function could answer for free is dollars
    unavailable for the listings that actually need judgment.
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
      {{
        "scope":        "in_scope" | "out_of_scope",
        "reason":       "<one short sentence, max 20 words>",
        "industry":     "<1-3 words naming the EMPLOYER's industry, or null>",
        "is_technical": true | false | null
      }}

    `industry` and `is_technical` are TAGS, not filters — they never decide
    whether a listing is kept, only how a reader can slice the board later.
    Use null rather than guessing; null renders as "untagged" and stays
    visible, a wrong guess misfiles the role.

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
        return assign_lane(listing, quick)

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
        return assign_lane(
            listing,
            ClassifierResult(
                prestige="prestige",  # source-curated
                scope=data["scope"],
                reason=data.get("reason", ""),
                industry=clean_tag(data.get("industry")),
                is_technical=clean_bool(data.get("is_technical")),
            ),
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

# "Research assistant" is the one entry in `technical_terms` that names an
# EMPLOYMENT ARRANGEMENT, not a field — a Research Assistant in a History
# department is exactly as valid a title as one in Computer Science, unlike
# every other entry ("engineer*", "data scien*", …), which is unambiguous on
# its own. So it only counts as technical when the title ALSO names one of
# these domains — kept separate from `technical_terms` itself (rather than
# just re-adding bare "ai" etc. there) because these words are only a safe
# signal in combination with "research assistant"; standalone, several of
# them over-admitted through the floor lane's own door (see the technical_terms
# comment for the corpus that caught it).
_RESEARCH_ASSISTANT_DOMAIN_HINTS = (
    "ai", "a.i.", "artificial intelligence",
    "machine learning", "deep learning", "ml", "llm", "nlp", "computer vision",
    "computer scien*", "data scien*", "data engineer*", "bioinformatic*",
    "software", "engineer*",
)


def floor_reason(listing: JobListing, cfg: FloorLaneConfig | None = None) -> str | None:
    """Why this listing qualifies for the floor lane, or None if it does not.

    Three criteria, all required:

    (a) TECHNICAL — judged on the title alone. A sales listing whose body
        mentions "you'll work with our engineering team" is still a sales
        listing, so the description does not get a vote here. A title naming a
        non-technical business function is disqualified outright, whatever
        else it says — but note this uses `_negative_title_no_subject_rescue`,
        NOT `negative_title`: this criterion is the final word (no LLM sits
        downstream of it), so it cannot afford `negative_title`'s qualifier
        rescue, which only being safe when an LLM call follows. See that
        function's docstring for why "Sales Executive, Software Solutions"
        and "Software Engineer, Trading Systems" need different answers here.
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
    if _negative_title_no_subject_rescue(title) is not None:
        return None
    tech = _first_term(title, cfg.technical_terms)
    if tech is None:
        return None
    if tech == "research assistant" and _first_term(title, _RESEARCH_ASSISTANT_DOMAIN_HINTS) is None:
        # Unlike every other entry in `technical_terms`, "research assistant"
        # alone doesn't imply a technical field — a Research Assistant in a
        # History department is exactly as valid a title as one in Computer
        # Science. It only counts here when the title ALSO names a technical
        # domain — "Research Assistant (AI)", "…, Computer Science Dept" —
        # the same signal issue #2's own fifth acceptance example carries.
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

    THE THIRD LANE, "broad", is what the capture inversion added. Neither lane
    CLAIMS most of what is now captured — an in-scope role at an unremarkable
    employer that is not a short technical engagement is simply a role, and
    since prestige stopped being a gate those reach the register in bulk.
    Stamping them "prestige" would print a claim about the employer that
    nothing checked. So `lane` still answers one question — "which lane, if
    any, actively claimed this?" — and "broad" is the honest answer for most
    rows.

    This function is also the single place tags are stamped, because it is
    already the single place every verdict passes through — heuristic and LLM
    alike. Tags are stamped BEFORE the scope check so an out-of-scope verdict
    still carries them; they cost nothing, and a caller that logs a rejection
    can say what it rejected.
    """
    result = stamp_tags(listing, result)
    if result.scope != "in_scope":
        return result  # out of scope is out of scope in every lane
    if result.prestige == "prestige":
        return result  # prestige lane already has it
    if result.lane in ("floor", "broad"):
        return result  # already stamped; re-stamping would duplicate the reason
    reason = floor_reason(listing, cfg)
    if reason is None:
        return replace(result, lane="broad")
    combined = f"{result.reason} · {reason}" if result.reason else reason
    return replace(result, lane="floor", reason=combined)


def stamp_tags(listing: JobListing, result: ClassifierResult) -> ClassifierResult:
    """Attach the derived `role_type` to a verdict. Idempotent.

    Only `role_type` is derived here — it is a keyword lookup over the title
    and body, so paying an LLM for it would add cost and variance to something
    deterministic. `industry` and `is_technical` ride along on the verdict from
    the classifier call that was already being made, and are left exactly as
    they arrived.

    A derivation that finds nothing leaves the tag None. It does NOT fall back
    to "full-time": the commonest untyped title is a bare "Software Engineer",
    and filing every one of those under the one role type the reader is most
    likely to have filtered out would hide roles behind a tag nobody asserted.
    """
    role_type = derive_role_type(listing.title, listing.description)
    if role_type is None or role_type == result.role_type:
        return result
    return replace(result, role_type=role_type)


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


def _coerce(prestige: str, scope: str, reason: str, obj: dict | None = None) -> ClassifierResult:
    """Clamp model output to valid values.

    The two VERDICT fields keep their precision-biased defaults: an
    unrecognised value there is a model that did not follow instructions, and
    the safe direction for a gate is to reject.

    The TAG fields default the other way, to None. A tag is not a gate, so
    there is no "safe direction" to fail toward — only an honest one. `None`
    renders as "untagged" and leaves the row visible under every filter; a
    fabricated `false` would hide it from a reader filtering `technical = yes`
    on the strength of a value the model never produced.
    """
    if prestige not in ("prestige", "marginal", "skip"):
        prestige = "skip"
    if scope not in ("in_scope", "out_of_scope"):
        scope = "out_of_scope"
    obj = obj or {}
    return ClassifierResult(
        prestige=prestige,
        scope=scope,
        reason=reason or "",
        industry=clean_tag(obj.get("industry")),
        is_technical=clean_bool(obj.get("is_technical")),
    )


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
            # scope decided by `_employer_gated_result`, not hardcoded — see its
            # docstring. classify_batch applies assign_lane to every "done"
            # result, this one included, so scope has to leave room for that.
            return _employer_gated_result("skip", "domain-wrong employer (non-tech sector)", listing), "done"
        if _hard_marginal_check(employer):
            return _employer_gated_result("marginal", "crypto/non-prestige-fintech employer", listing), "done"
        if _boost_check(employer):
            auto_prestige = True  # known prestige → fall through to scope-only routing

    if auto_prestige:
        quick = _scope_quick_classify(listing)
        if quick is not None:
            return quick, "done"
        return None, "scope"

    # The scope guard is not a property of prestige, so it applies to the full
    # lane as well: a sales or talent-acquisition title is out of scope whoever
    # posted it, and paying for an LLM call to be told so is waste. This
    # branch — not `_scope_quick_classify` — is where MOST of the real cost
    # saving actually lives: the full lane is the busy one (CEDARS +
    # LinkedIn, mostly non-boosted employers that never touched
    # `_scope_quick_classify` either way), and measured against both a small
    # mixed corpus and a real employer register the net effect of this
    # change is fewer LLM calls, not more. See the cost note on
    # `_scope_quick_classify` and README "Two admission lanes" for the
    # numbers.
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
) -> list[ClassifierResult | None]:
    """One CLI call classifying all `listings`. Results aligned to input order.

    `None` MEANS "NO VERDICT EXISTS FOR THIS LISTING", and that is the whole
    point of this function's return type. It is not a verdict, not a default,
    and not a value any caller may render.

    This used to return a real `ClassifierResult` — `skip / out_of_scope /
    "batch fallback"` — whenever the CLI timed out, exited non-zero or returned
    something that was not a JSON array. That is a judgement recorded for a
    call that never happened, and it is the same failure shape that let CEDARS
    print "Surfaced: none today" for fifty days: one value meaning both
    "nothing there" and "I could not look". It was worse here than upstream,
    because the fabricated verdict then travelled: `assign_lane` short-circuits
    on `scope != "in_scope"`, so a fake `out_of_scope` also vetoed the FLOOR
    lane — which is pure string matching and needs no LLM at all — and the
    orchestrator had already written every id into the seen-set, so the
    listings were consumed forever on the strength of a call that never ran.

    A missing index in an otherwise-valid array is `None` for the same reason:
    the model was asked about that listing and did not answer, so there is no
    verdict for it either.

    Callers must handle `None` explicitly. `classify_batch` propagates it; the
    orchestrator holds those listings back out of the seen-set and surfaces the
    count as a ⚠️ health line.
    """
    n = len(listings)
    if n == 0:
        return []
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
        log.error("batch classifier timed out for %d listings — no verdicts", n)
        return [None] * n
    if proc.returncode != 0:
        log.error(
            "batch classifier exited %d for %d listings — no verdicts: %s",
            proc.returncode, n, proc.stderr[:200],
        )
        return [None] * n

    arr = _extract_json_array(proc.stdout)
    if arr is None:
        log.error(
            "batch classifier returned a non-array for %d listings — no verdicts: %s",
            n, proc.stdout[:200],
        )
        return [None] * n

    out: list[ClassifierResult | None] = [None] * n
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
        out[i] = _coerce(
            prestige, str(obj.get("scope", "out_of_scope")), str(obj.get("reason", "")), obj
        )
    missing = sum(1 for r in out if r is None)
    if missing:
        log.error(
            "batch classifier answered %d of %d listings — %d left unclassified",
            n - missing, n, missing,
        )
    return out


def classify_batch(listings: list[JobListing]) -> list[ClassifierResult | None]:
    """Classify many listings with ≤1 LLM call per chunk per route.

    Drop-in replacement for calling _classify_one in a loop. Heuristics resolve
    most listings for free; the rest are batched into the full / scope-only paths.
    Returns one entry per input listing, in order — a `ClassifierResult`, or
    `None` where the LLM call that was supposed to judge it never produced an
    answer (see `_batch_llm`).

    `None` IS NOT A VERDICT AND THE FLOOR LANE DOES NOT GET TO RUN ON IT. That
    is a deliberate call, and it is the narrower of the two options.

    The floor lane is pure string matching (`floor_reason`) and needs no LLM, so
    it *could* be evaluated for an unclassified listing. The reason it is not:
    the orchestrator holds unclassified listings OUT of the seen-set so the next
    run retries them. Surfacing one anyway would mean either (a) pushing it now
    and pushing it again next run when a real verdict arrives, or (b) consuming
    it now on the strength of a lane that never asked the scope question the
    `full` route was routed to the LLM to answer. (a) is a duplicate digest,
    (b) reintroduces exactly what this change deletes — a listing that reads as
    judged and was not.

    So the invariant is one line long: **a listing is either judged or retried,
    never both and never neither.** The cost is bounded and visible — a
    floor-eligible role is delayed by one run, and the digest says how many were
    held. That is a strictly better trade than a third state nobody can read off
    the output.
    """
    # `None` here starts out meaning "not routed yet" and ends up meaning
    # "no verdict"; the two coincide because every index is written exactly once
    # by the loops below, and an LLM route writes `None` only when it genuinely
    # produced no answer.
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
    # A `None` passes straight through: there is no verdict to stamp a lane onto.
    return [
        None if r is None else assign_lane(listing, r)
        for listing, r in zip(listings, results)
    ]
