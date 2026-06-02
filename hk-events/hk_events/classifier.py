"""LLM-driven relevance classification for each new event.

Mirrors job-sift/classifier.py: hard keyword rules run BEFORE the LLM to catch
obvious cases cheaply, then the Claude CLI is shelled out for the ambiguous
remainder (same auth/quota inheritance as signal-brief + job-sift).

PRECISION BIAS (per feedback_job_sift_precision): bias HARD toward precision
over recall. Click-through / calendar-clutter cost > missed-event cost. The
DEFAULT for anything uncertain is DROP, not surface. "uncertain → surface
anyway" is the WRONG default — same philosophy job-sift's classifier encodes.
"""

from __future__ import annotations

import json
import logging
import subprocess
from textwrap import dedent

import yaml

from hk_events.config import CLAUDE_BIN, HK_EVENTS_MODEL, PROJECT_ROOT
from hk_events.schema import Event, RelevanceResult

log = logging.getLogger(__name__)


CLASSIFIER_SYSTEM_PROMPT = dedent("""
    You are an event-relevance classifier for Dylan Porter, a Hong Kong-based
    full-stack + AI engineer, founding-team AI dev at an Antler startup, and
    aspiring entrepreneur. He attends HK events for exactly two reasons:

    1. FOUNDER / AI ROOM — funded startups, AI builders, founders, VCs,
       hackathons, technical AI/ML meetups, demo nights, accelerator events.
       (His peer network + technical signal + co-founder/hiring surface.)
    2. SME-BUYER ROOM — events where Hong Kong SMEs, traditional businesses,
       trade/import-export operators, or non-technical company owners gather.
       (These are the rooms where he can find customers for software/AI
       services — the right buyers, not the builders.)

    Everything else is noise and must be DROPPED.

    For each event, return STRICT JSON with two fields:

    {
      "tag":    "founder_ai" | "sme_buyer" | "drop",
      "reason": "<one short sentence, max 20 words>"
    }

    "founder_ai" — clearly an AI/tech/startup/founder/VC/hackathon event.
      Examples: AI Tinkerers meetup, a GenAI talk, a YC/Antler demo day, a
      Cyberport startup pitch night, an HKSTP deep-tech showcase, a vLLM /
      LLM-infra meetup.

    "sme_buyer" — clearly a room full of HK SME owners / traditional-business
      decision-makers who could BUY software or AI services. Examples: an HKTDC
      SME digitalisation seminar, a trade-association mixer, a chamber-of-commerce
      business-growth event, an "AI for SMEs" / "digital transformation for your
      business" workshop aimed at non-technical owners.

    "drop" — anything else, INCLUDING (be ruthless):
      - generic corporate conferences with no clear founder OR SME-buyer angle
      - pure marketing/sales pitches, MLM, crypto-shilling, get-rich-quick
      - recruiting/career fairs, university info sessions
      - social-only events with no professional substance
      - finance/web3/crypto trading meetups that aren't AI/builder-focused
      - wellness, lifestyle, arts, networking-for-its-own-sake
      - anything where you genuinely can't tell which room it is

    PRECISION BIAS — this is the most important rule: when you are UNSURE which
    bucket an event falls in, return "drop". A missed good event costs Dylan
    almost nothing (events recur, sources overlap). A false-positive clutters
    his calendar and erodes his trust in this digest. DROP when uncertain.

    Return ONLY the JSON — no prose, no markdown fences.
""").strip()


def _build_user_prompt(event: Event) -> str:
    lines = [
        f"Source: {event.source}",
        f"Title: {event.title}",
    ]
    if event.organizer:
        lines.append(f"Organizer: {event.organizer}")
    if event.location:
        lines.append(f"Location: {event.location}")
    if event.start:
        lines.append(f"Start: {event.start.isoformat()}")
    if event.description:
        # Truncate to keep classifier context tight — the body is mainly for
        # disambiguating which room a thin title belongs to.
        lines.append("")
        lines.append("Description:")
        lines.append(event.description[:3000])
    return "\n".join(lines)


# Sources whose curation already implies the founder/AI room — we can short-
# circuit to founder_ai without an LLM call (analogue of job-sift's auto-
# prestige sources). AI Tinkerers is, by definition, an AI-builder room.
AUTO_FOUNDER_SOURCES: set[str] = {"aitinkerers"}

# Hard keyword lists. Defaults below; overridden by config/sources.yaml
# filter_keywords if present (so Dylan can tune without touching code). Keep
# these tight: precision bias means a borderline title should go to the LLM
# (which itself defaults to drop), not get hard-classified here.
_DEFAULT_HARD_DROP = {
    "career fair", "job fair", "recruitment", "info session", "open day",
    "mlm", "forex", "get rich", "passive income",
    "yoga", "wellness retreat", "wine tasting", "speed dating",
}
_DEFAULT_HARD_FOUNDER = {
    "hackathon", "ai tinkerers", "demo day", "demo night",
    "genai", "llm", "vllm", "machine learning meetup",
    "founders", "founder mixer", "pitch night", "startup grind",
}


def _load_keywords() -> tuple[set[str], set[str]]:
    """Return (hard_founder, hard_drop) keyword sets, merging config over defaults."""
    cfg_path = PROJECT_ROOT / "config" / "sources.yaml"
    founder = set(_DEFAULT_HARD_FOUNDER)
    drop = set(_DEFAULT_HARD_DROP)
    if cfg_path.exists():
        try:
            kw = (yaml.safe_load(cfg_path.read_text()) or {}).get("filter_keywords", {}) or {}
            founder = {s.lower() for s in (kw.get("hard_founder_ai") or [])} or founder
            drop = {s.lower() for s in (kw.get("hard_drop") or [])} or drop
        except Exception as exc:
            log.warning("could not read filter_keywords from config: %s — using defaults", exc)
    return founder, drop


_HARD_FOUNDER_SUBSTRINGS, _HARD_DROP_SUBSTRINGS = _load_keywords()


def _hard_check(event: Event) -> RelevanceResult | None:
    """Cheap keyword pass for obvious cases. Returns None if ambiguous (→ LLM)."""
    t = event.title.lower()
    if any(k in t for k in _HARD_DROP_SUBSTRINGS):
        return RelevanceResult(tag="drop", reason="title matches hard-drop keyword (non-relevant room)")
    if any(k in t for k in _HARD_FOUNDER_SUBSTRINGS):
        return RelevanceResult(tag="founder_ai", reason="title contains unambiguous AI/founder keyword")
    return None


def _parse_llm_json(stdout: str) -> RelevanceResult:
    stdout = stdout.strip()
    if stdout.startswith("```"):
        stdout = stdout.strip("`")
        if stdout.lower().startswith("json"):
            stdout = stdout[4:].lstrip()
    try:
        data = json.loads(stdout)
        tag = data["tag"]
        if tag not in ("founder_ai", "sme_buyer", "drop"):
            raise ValueError(f"unexpected tag {tag!r}")
        return RelevanceResult(tag=tag, reason=data.get("reason", ""))
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        # Precision bias: an unparseable verdict defaults to DROP, never surface.
        log.warning("classifier returned bad JSON: %s", stdout[:200])
        return RelevanceResult(tag="drop", reason=f"parse error → dropped: {exc}")


def classify(event: Event, *, timeout: float = 60.0) -> RelevanceResult:
    """Classify one event into founder_ai / sme_buyer / drop.

    Source short-circuit → hard keyword rules → LLM. On ANY failure path the
    verdict is "drop" (precision bias).
    """
    if event.source in AUTO_FOUNDER_SOURCES:
        return RelevanceResult(tag="founder_ai", reason="source is a curated AI-builder community")

    hard = _hard_check(event)
    if hard is not None:
        return hard

    user_prompt = _build_user_prompt(event)
    cmd = [
        CLAUDE_BIN,
        "--model", HK_EVENTS_MODEL,
        "--system-prompt", CLASSIFIER_SYSTEM_PROMPT,
        "--print",
        user_prompt,
    ]
    log.debug("classifier cmd: %s ...", " ".join(cmd[:3]))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        log.warning("classifier timed out for %s", event.title[:40])
        return RelevanceResult(tag="drop", reason="classifier timeout → dropped")

    if proc.returncode != 0:
        log.warning("classifier exited %d: %s", proc.returncode, proc.stderr[:200])
        return RelevanceResult(tag="drop", reason="classifier error → dropped")

    return _parse_llm_json(proc.stdout)
