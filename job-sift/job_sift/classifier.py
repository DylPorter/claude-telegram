"""LLM-driven prestige + scope classification for each new listing.

Uses the Claude CLI as a subprocess (same convention as signal-brief), so we
inherit auth/quota from Dylan's existing setup. Single call per listing
returns both verdicts in one structured response.
"""

from __future__ import annotations

import json
import logging
import subprocess
from textwrap import dedent

from job_sift.config import CLAUDE_BIN, JOB_SIFT_MODEL
from job_sift.schema import ClassifierResult, JobListing

log = logging.getLogger(__name__)


CLASSIFIER_SYSTEM_PROMPT = dedent("""
    You are a job-listing classifier for Dylan Porter, a 2nd-year HKU Computer
    Science undergrad who works as a contract AI engineer at Collective Global.
    He's looking for **prestige-tier internships or short-term contracts only**.

    For each listing, return STRICT JSON with three fields:

    {
      "prestige": "prestige" | "marginal" | "skip",
      "scope":    "in_scope" | "out_of_scope",
      "reason":   "<one short sentence, max 20 words>"
    }

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

    SCOPE rules — Dylan accepts:
    - Internships (any length)
    - Short-term contracts up to ~1 year
    - Rotational / co-op / graduate trainee programs that are explicitly time-boxed to ≤1 year
    - Summer / winter programs

    Dylan REJECTS:
    - Permanent full-time roles (he's still in school, won't graduate till 2028)
    - "Graduate Engineer" or "Associate" hires that are clearly permanent
    - Anything requiring a degree he doesn't yet have

    If the role type is unclear from the title (e.g. just "Software Engineer"), assume permanent FT and mark
    "out_of_scope" UNLESS the title contains intern/contract/rotational/co-op/summer/winter/trainee keywords.

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
        return ClassifierResult(
            prestige=data["prestige"],
            scope=data["scope"],
            reason=data.get("reason", ""),
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


def _scope_quick_classify(listing: JobListing) -> ClassifierResult | None:
    """Cheap keyword heuristic for obvious scope cases. Returns None if ambiguous."""
    title_l = listing.title.lower()
    if any(k in title_l for k in _SCOPE_KEYWORDS_IN):
        return ClassifierResult(prestige="prestige", scope="in_scope", reason="title contains intern/contract keyword")
    if any(k in title_l for k in _SCOPE_KEYWORDS_OUT):
        return ClassifierResult(prestige="prestige", scope="out_of_scope", reason="title indicates senior/perm role")
    return None


SCOPE_SYSTEM_PROMPT = dedent("""
    You are a scope classifier for Dylan Porter, a 2nd-year HKU Computer Science
    undergrad. The employer is already confirmed as a prestige target — you only
    need to classify SCOPE.

    Return STRICT JSON:
      { "scope": "in_scope" | "out_of_scope", "reason": "<one short sentence, max 20 words>" }

    Dylan accepts:
    - Internships (any length)
    - Short-term contracts up to ~1 year
    - Rotational / co-op / graduate trainee programs explicitly ≤1 year
    - Summer / winter programs

    Dylan REJECTS:
    - Permanent full-time roles (he's still in school until 2028)
    - "Graduate Engineer" or "Associate" titles that are clearly permanent
    - Anything requiring a degree he doesn't yet have

    If the role type is unclear from the title (e.g. just "Software Engineer"),
    assume permanent FT and mark "out_of_scope" UNLESS the title contains
    intern/contract/rotational/co-op/summer/winter/trainee keywords.

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
