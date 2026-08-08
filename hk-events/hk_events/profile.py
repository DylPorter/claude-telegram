"""Load the operator profile that parameterises the event classifier prompt.

Same rationale as job-sift/job_sift/profile.py: the prompt used to hard-code who
the digest was for, which made a public repo carry one person's networking
strategy. Identity and bucket definitions now live in `config/profile.yaml`
(gitignored), with a generic committed example that doubles as the fallback.

The two bucket KEYS (founder_ai, sme_buyer) stay fixed — they're part of the
RelevanceTag Literal in schema.py — but what each bucket MEANS is configurable,
which is the part that's actually personal.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import yaml

from hk_events.config import PROJECT_ROOT

log = logging.getLogger(__name__)

PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"
PROFILE_EXAMPLE_PATH = PROJECT_ROOT / "config" / "profile.yaml.example"

_DEFAULTS = {
    "identity": "a technologist who attends events for networking and signal",
    "founder_ai": "startups, AI builders, founders, VCs, hackathons, technical meetups, demo nights",
    "sme_buyer": "small and medium businesses, traditional industry, non-technical company owners",
}


@lru_cache(maxsize=1)
def load_profile() -> dict:
    """Return the operator profile, falling back to the committed example."""
    for path in (PROFILE_PATH, PROFILE_EXAMPLE_PATH):
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:
            log.warning("failed to parse profile at %s: %s", path, exc)
            continue
        if path is PROFILE_EXAMPLE_PATH:
            log.warning(
                "no config/profile.yaml found — using the generic example profile. "
                "Copy it and edit to tune the classifier to you."
            )
        return data
    log.warning("no profile config found — using built-in defaults")
    return {}


def profile_block() -> dict[str, str]:
    """Prompt-ready fragments, falling back per-key so a partial profile works."""
    p = load_profile()
    return {
        key: str(p.get(key) or default).strip()
        for key, default in _DEFAULTS.items()
    }
