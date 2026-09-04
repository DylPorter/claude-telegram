"""Advisory tags: derive what is cheap, coerce what the model returns.

COPIED, NOT IMPORTED — same reasoning as board.py. Keep the copies in step.

THE ONE RULE. Every function here returns `None` when it does not know, and
`None` means UNTAGGED — a row the board still shows, under the "—" option of
whatever facet it is missing. Nothing in this module may return a confident
default, because a wrong-but-confident tag is worse than an absent one: the
absent one is visibly absent, and the confident one silently misfiles a role
into a bucket the reader has filtered away.

That is the whole architectural point of the redesign this file belongs to. The
old classifier answered "is this good enough to show?" and threw away what it
said no to; a title-only test cannot tell a research internship at a lab from a
research associate at an asset manager, so the argument was unwinnable and the
losses were silent. Now capture keeps everything in scope and the reader
filters. No keyword list can wrongly delete a role if nothing is being deleted.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Canonical role_type vocabulary. A value outside this set is not coerced into
# it — it is dropped to None, so the board shows the row untagged rather than
# mislabelled.
ROLE_TYPES = (
    "intern",
    "part-time",
    "full-time",
    "contract",
    "rotational",
    "research-assistant",
)

_BOUNDARY_BEFORE = r"(?<![a-z0-9])"
_BOUNDARY_AFTER = r"(?![a-z0-9])"


@lru_cache(maxsize=512)
def _pattern(term: str) -> re.Pattern[str]:
    """Word-boundary match; a trailing `*` means prefix.

    Same matcher discipline as the classifier's term lists, and for the same
    reason: a bare substring match on short tokens ("ra", "ft") fires on a
    third of any real corpus, and a term class that fires on a third of the
    corpus is not a signal.
    """
    t = term.strip().lower()
    prefix = t.endswith("*")
    if prefix:
        t = t[:-1]
    return re.compile(_BOUNDARY_BEFORE + re.escape(t) + ("" if prefix else _BOUNDARY_AFTER))


# ORDER IS THE PRECEDENCE RULE, and it is not alphabetical. A title routinely
# names two of these ("Summer Internship (6-month contract)", "Part-time
# Research Assistant") and the earlier entry wins, so the more specific shape
# of engagement beats the more generic one. "full-time" is last on purpose:
# it is the least informative answer and the one most often incidental.
_ROLE_TYPE_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("intern", ("intern", "interns", "internship", "internships", "co-op", "coop", "placement student")),
    ("rotational", ("rotational", "rotation", "graduate trainee", "trainee", "graduate programme", "graduate program")),
    ("research-assistant", ("research assistant", "research assistants", "teaching assistant", "student assistant")),
    ("part-time", ("part time", "part-time", "parttime", "casual")),
    ("contract", ("contract", "contractor", "temporary", "temp", "fixed term", "fixed-term", "freelance", "locum")),
    ("full-time", ("full time", "full-time", "permanent")),
)


def derive_role_type(*texts: str | None) -> str | None:
    """The engagement shape named in `texts`, or None if none is.

    Cheap and deterministic, so the LLM is never asked for it: a title either
    contains one of these words or it does not, and an LLM adds cost and
    variance to a lookup. `None` is a real and common answer — a bare "Software
    Engineer" names no engagement shape at all — and it must stay None rather
    than defaulting to "full-time", which would file every unlabelled role under
    a filter the reader is most likely to have excluded.
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob.strip():
        return None
    for role_type, terms in _ROLE_TYPE_TERMS:
        for term in terms:
            if _pattern(term).search(blob):
                return role_type
    return None


def clean_tag(value, *, max_len: int = 40) -> str | None:
    """Normalise a free-text tag (an industry, say) from model output.

    Anything that is not a usable short string — None, a bool, a dict, an empty
    string, a model saying "unknown" — becomes None. Untagged, never guessed.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip(" .,;:")
    if not text:
        return None
    if text.lower() in {"unknown", "n/a", "na", "none", "null", "-", "—", "other", "unclear"}:
        return None
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text.lower()


def clean_bool(value) -> bool | None:
    """Parse a tri-state boolean tag. Unrecognised → None, never False.

    False and None are different claims — "I looked and it is not technical"
    versus "nobody said" — and collapsing them is the same one-value-means-two-
    things bug the rest of this codebase spent six branches removing. A reader
    who filters `technical = no` must not be shown roles nobody classified.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "y", "1", "technical"}:
            return True
        if low in {"false", "no", "n", "0", "non-technical", "nontechnical"}:
            return False
    return None


def clean_role_type(value) -> str | None:
    """Accept a role_type only if it is already in the canonical vocabulary."""
    if not isinstance(value, str):
        return None
    low = value.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "internship": "intern",
        "ra": "research-assistant",
        "researchassistant": "research-assistant",
        "fulltime": "full-time",
        "parttime": "part-time",
    }
    low = aliases.get(low.replace("-", ""), aliases.get(low, low))
    return low if low in ROLE_TYPES else None
