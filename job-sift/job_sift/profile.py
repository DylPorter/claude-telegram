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
            log.warning("failed to parse %s at %s: %s", label, path, exc)
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
