"""Load the operator profile that parameterises the classifier prompts.

Why this exists: the classifier prompts used to hard-code who the digest was
for — name, university, year of study, and the exact accept/reject criteria for
roles. That made the public repo a personal document rather than a framework,
and it published a specific person's job-hunting criteria to anyone who cloned
it.

Now the identity lives in `config/profile.yaml`, which is gitignored. The
committed `config/profile.yaml.example` is a generic template, and is also the
fallback if no real profile exists — so a fresh clone runs out of the box with
sensible defaults instead of crashing.

Keep personal specifics OUT of the example file. That is the whole point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from job_sift.config import PROJECT_ROOT

log = logging.getLogger(__name__)

PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"
PROFILE_EXAMPLE_PATH = PROJECT_ROOT / "config" / "profile.yaml.example"


@lru_cache(maxsize=1)
def load_profile() -> dict:
    """Return the operator profile, falling back to the committed example.

    Never raises on a missing profile: an unconfigured clone should still run.
    """
    for path, label in ((PROFILE_PATH, "profile"), (PROFILE_EXAMPLE_PATH, "example profile")):
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:
            # error, not warning: a malformed OWN profile.yaml falls through to
            # the generic example and the digest keeps running — looking
            # completely normal while classifying against someone else's
            # accept/reject criteria. That is exactly the silent-degradation
            # shape this project has spent several commits closing off
            # elsewhere (empty location_allowlist, a tableless CEDARS page);
            # a config file that fails to parse deserves the same volume.
            log.error(
                "failed to parse %s at %s — falling through to the next profile "
                "in the chain, which changes what the classifier accepts: %s",
                label,
                path,
                exc,
            )
            continue
        if path is PROFILE_EXAMPLE_PATH:
            log.warning(
                "no config/profile.yaml found — running with the generic example profile. "
                "Copy it and edit to tune the classifier to you."
            )
        return data
    log.warning("no profile config found at all — classifier will use built-in defaults")
    return {}


def _bullets(items) -> str:
    """Render a YAML list as prompt bullet lines, tolerating a bare string."""
    if not items:
        return "    - (none specified)"
    if isinstance(items, str):
        items = [items]
    return "\n".join(f"    - {str(i).strip()}" for i in items)


def profile_block() -> dict[str, str]:
    """Prompt-ready fragments built from the profile config."""
    p = load_profile()
    return {
        "identity": str(p.get("identity") or "a candidate").strip(),
        "seeking": str(p.get("seeking") or "relevant roles").strip(),
        "accepts": _bullets(p.get("accepts")),
        "rejects": _bullets(p.get("rejects")),
        "unclear_role_default": str(
            p.get("unclear_role_default")
            or "assume permanent full-time and mark out_of_scope"
        ).strip(),
    }


# ---------------------------------------------------------------------------
# Classifier heuristics config
#
# Both of the structures below are TERM LISTS, and they live here rather than in
# classifier.py on purpose: classifier.py owns the *mechanism* (how a term is
# matched, what a match implies, which lane wins a tie), and this module owns
# *who the digest is for*. An operator retunes the sift by editing YAML, not by
# editing the matcher.
#
# The defaults are a generic engineering-vs-business role taxonomy — true of any
# software candidate, personal to none — so an unconfigured clone still behaves
# sensibly. The one genuinely personal axis, geography, has NO code default; see
# `_default_floor_locations`.
# ---------------------------------------------------------------------------

# Non-technical business functions. A title matching one of these is never
# admitted by the cheap keyword path — "Summer Analyst" at a bank passes the
# intern-keyword test while being nothing like an engineering role.
_DEFAULT_NEGATIVE_TITLES = (
    "strategy",
    "business develop*",
    "sales",
    "talent acquisition",
    "trading",
    "trainee",
    "asset management",
    "wealth management",
    "risk",
    "finance",
    "accounting",
    "marketing",
    "human resources",
    "recruit*",
    "analyst",
)

# Words that RESCUE a negative title. "Analyst" alone is a finance role;
# "Technology Analyst" is not. The carve-out is applied to every negative term,
# not just "analyst", because several of them ("trading", "risk", "finance")
# legitimately qualify an engineering role — "Software Engineer, Trading
# Systems" must not be thrown away by a substring match on "trading".
_DEFAULT_TECHNICAL_QUALIFIERS = (
    "technology",
    "technolog*",
    # "technolog*" alone misses the bare noun "Tech" — "SocGen — TRAINEE:
    # Securities Financing Tech" was a real false negative under the guard
    # (rejected outright as a finance trainee with nothing to rescue it).
    # Adding "tech" only ever RESCUES a negative title into an LLM call —
    # rescuing wrongly costs one extra classification, not a false admit —
    # so the asymmetry that makes the guard safe is unaffected.
    "tech",
    "engineer*",
    "software",
    # NOT bare "developer": it is a substring of "Business Developer", so it
    # cancelled the very negative term it sits next to. A qualifier must be
    # narrower than the negatives it overrides. "Software Developer" is caught
    # by "software" already.
    "software developer",
    "development engineer",
    "data scien*",
    "data engineer*",
    "machine learning",
    "computer scien*",
    # NOT "programm*": that matches "Programme", the British spelling used by
    # every graduate scheme in Hong Kong, so "Graduate Trainee Programme" would
    # cancel its own negative term and walk straight through the guard.
    "programmer*",
    "programming",
)

# What makes a role technical enough for the floor lane. Judged on the TITLE
# only — a sales listing whose body mentions "our engineering team" is still a
# sales listing.
#
# Deliberately WITHOUT bare "ai", "technical", "automation" and "research
# assistant" — each one was demonstrated to over-admit through the floor
# lane's own door: "Legal Counsel, AI Policy (Contract)", "AI Content
# Moderator, Contract", "AI Data Annotator - Freelance" (bare "ai"),
# "Technical Writer (6-month contract)" (bare "technical"), "Office
# Automation Assistant (Part time)" (bare "automation"), and "Research
# Assistant (History Department), Temporary" (bare "research assistant") all
# satisfied criterion (a) on the word alone despite none of them being an
# engineering-shaped role. Every issue-#2 acceptance example still admits
# without these — each one also carries a more specific term below
# ("engineer*", "data scien*", "bioinformatic*") — so this is a precision
# fix, not a recall cut against anything the lane was built to catch. Kept
# bare "ml": word-boundary matching means it only fires on a standalone "ML"
# token ("ML Engineer", "ML Ops"), and no realistic false positive for that
# was found the way there was for the other four.
_DEFAULT_TECHNICAL_TERMS = (
    "engineer*",
    "software",
    "developer",
    "programmer*",
    "programming",
    "data scien*",
    "data engineer*",
    "machine learning",
    "deep learning",
    "a.i.",
    "artificial intelligence",
    "ml",
    "llm",
    "nlp",
    "computer vision",
    "bioinformatic*",
    "devops",
    "backend",
    "back-end",
    "frontend",
    "front-end",
    "full stack",
    "full-stack",
    "python",
    "technolog*",
    "it support",
    "platform support",
    "qa engineer",
    "test engineer",
)

# What makes an engagement flexible enough for the floor lane: the part-time /
# contract / rolling / RA shapes the prestige lane was never built to catch.
_DEFAULT_ENGAGEMENT_TERMS = (
    "part time",
    "part-time",
    "parttime",
    "contract",
    "contractor",
    "contractual",
    "rolling",
    "temporary",
    "temp",
    "fixed term",
    "fixed-term",
    "freelance",
    "casual",
    "research assistant",
    "intern",
    "internship",
    "placement",
    "secondment",
    "locum",
    "month contract",
    "months contract",
)


def _terms(value, default: tuple[str, ...]) -> tuple[str, ...]:
    """Normalise a YAML term list, falling back to `default` when unset.

    An explicitly EMPTY list is honoured as empty — that is how an operator
    turns a term class off — but a missing key falls back. `None` and a bare
    string are both tolerated because hand-edited YAML produces both.
    """
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(t for t in (str(v).strip().lower() for v in value) if t)


@dataclass(frozen=True)
class ScopeGuardConfig:
    """Terms for the cheap pre-LLM scope guard (see classifier._negative_title)."""

    negative_titles: tuple[str, ...] = _DEFAULT_NEGATIVE_TITLES
    technical_qualifiers: tuple[str, ...] = _DEFAULT_TECHNICAL_QUALIFIERS


@dataclass(frozen=True)
class FloorLaneConfig:
    """Terms for the brand-agnostic floor lane (see classifier.floor_reason).

    `locations` is the load-bearing field: with nothing to match on, the lane
    admits nothing and the digest keeps its pre-existing prestige-only shape.
    That is the intended fail-safe — a floor lane with no geography would be an
    unbounded firehose, not a useful second net.
    """

    enabled: bool = True
    locations: tuple[str, ...] = ()
    technical_terms: tuple[str, ...] = _DEFAULT_TECHNICAL_TERMS
    engagement_terms: tuple[str, ...] = _DEFAULT_ENGAGEMENT_TERMS

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.locations) and bool(self.technical_terms)


def _default_floor_locations() -> tuple[str, ...]:
    """Geography for the floor lane when `profile.yaml` does not set it.

    Falls back to `config/companies.yaml`'s `location_allowlist`, which is the
    repo's ONE existing place-of-work config and is already what every ATS
    source filters on. Reusing it means the operator's geography is stated
    once, no city name gets hardcoded into the classifier, and — this is the
    part worth being precise about — BOTH a fresh clone and an existing
    deployment pick the floor lane up without touching a second file. That
    only holds because `config/profile.yaml.example` ships with its
    `floor_lane.locations` key commented out; an earlier draft of this file
    set it to a remote-only example value, which shadowed this fallback for
    every fresh clone and made the floor lane inert against issue #2's own
    acceptance examples. If you ever add a `locations:` example back to the
    committed file, it silences this function again — see the comment on
    that key in profile.yaml.example.

    Read here with a local loader rather than importing
    `sources._ats_common.load_location_allowlist`: the config surface should not
    depend on a source adapter, and this way the fallback keeps working if the
    ATS layer is refactored. Same defensive parsing as that function — blank
    entries are dropped, because `"" in anything` is True and one stray empty
    string would silently disable the whole location criterion.
    """
    path = PROJECT_ROOT / "config" / "companies.yaml"
    if not path.exists():
        return ()
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        log.warning("failed to parse companies.yaml for floor-lane locations: %s", exc)
        return ()
    raw = data.get("location_allowlist") or []
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(t for t in (str(s).strip().lower() for s in raw) if t)


@lru_cache(maxsize=1)
def scope_guard_config() -> ScopeGuardConfig:
    cfg = load_profile().get("scope_guard") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return ScopeGuardConfig(
        negative_titles=_terms(cfg.get("negative_titles"), _DEFAULT_NEGATIVE_TITLES),
        technical_qualifiers=_terms(
            cfg.get("technical_qualifiers"), _DEFAULT_TECHNICAL_QUALIFIERS
        ),
    )


@lru_cache(maxsize=1)
def floor_lane_config() -> FloorLaneConfig:
    cfg = load_profile().get("floor_lane") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    enabled = bool(cfg.get("enabled", True))
    locations = _terms(cfg.get("locations"), ())
    if not locations:
        locations = _default_floor_locations()
    result = FloorLaneConfig(
        enabled=enabled,
        locations=locations,
        technical_terms=_terms(cfg.get("technical_terms"), _DEFAULT_TECHNICAL_TERMS),
        engagement_terms=_terms(cfg.get("engagement_terms"), _DEFAULT_ENGAGEMENT_TERMS),
    )
    # Loud on purpose. `enabled: false` is an operator's deliberate choice and
    # stays quiet — but `enabled: true` with nowhere to look (no
    # `floor_lane.locations` AND an empty/missing companies.yaml
    # `location_allowlist`) is a misconfiguration masquerading as a normal
    # empty digest section: the register renders "No open floor-lane roles"
    # every single day and nothing says why. Same failure shape as an empty
    # `location_allowlist` silently filtering every ATS listing — that one
    # got a raised `SourceNotConfiguredError` upstream; this is the read-side
    # equivalent, a log line loud enough to explain an empty section that
    # will otherwise look like "nothing matched today" forever.
    if enabled and not result.active:
        log.warning(
            "floor_lane is enabled but has no locations to match — set "
            "floor_lane.locations in config/profile.yaml or a "
            "location_allowlist in config/companies.yaml. The floor lane "
            "will admit nothing and render an empty section until then."
        )
    return result


def reset_config_cache() -> None:
    """Drop every memoized profile read. For tests and one-off reconfiguration.

    Tolerant of a reader having been replaced by something uncached — a test
    that monkeypatches `load_profile` with a plain function still needs the
    DOWNSTREAM caches dropped, and blowing up on the one that no longer has a
    `cache_clear` would leave them stale.
    """
    for reader in (load_profile, scope_guard_config, floor_lane_config):
        clear = getattr(reader, "cache_clear", None)
        if clear is not None:
            clear()
