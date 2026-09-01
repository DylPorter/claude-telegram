"""The two `source_health.py` copies must not drift apart.

`job-sift/job_sift/source_health.py` and `hk-events/hk_events/source_health.py`
are deliberate siblings — identical logic, differing only in their module
docstring and which package's `config` they import. Nothing enforces that, and
the field that matters most is a bare integer. If the two `ALARM_THRESHOLD`s
drift, the bots silently disagree about what "dead" means: an operator who has
learnt "the alarm fires on the third bad run" from one digest reasons wrongly
about the other, which is the same class of quiet wrongness the alarm exists to
kill.

This is a DRIFT ALARM, not a refactor. Merging the two into a shared package is
a repo-structure decision — the monorepo is a candidate to be split into
per-bot repos — and it is not this test's to make. So:

  * the sibling is located RELATIVE to this module, never by absolute path, so
    the test survives a git worktree and a moved checkout;
  * it is read off disk and parsed with `ast`, never imported — importing the
    other bot's package from this interpreter would fail on a dependency it does
    not have, and a test that fails for that reason teaches the operator to
    ignore it;
  * if the sibling is genuinely gone — the split happened — this SKIPS. A guard
    that fails because the thing it compares against no longer exists is noise.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from job_sift import source_health

_OWN = Path(source_health.__file__).resolve()


def _repo_root() -> Path:
    """The checkout root, found by walking up — `.git` is a directory in a
    clone and a FILE in a worktree, so `.exists()` covers both. Falls back to
    the known `<repo>/<bot>/<package>/source_health.py` depth."""
    for parent in _OWN.parents:
        if (parent / ".git").exists():
            return parent
    return _OWN.parents[2]


def _sibling_copies() -> list[Path]:
    """Every OTHER `<bot>/<package>/source_health.py` in this checkout."""
    found = {
        p.resolve()
        for p in _repo_root().glob("*/*/source_health.py")
        if p.resolve() != _OWN
    }
    return sorted(found)


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _alarm_threshold(path: Path):
    for node in _module(path).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ALARM_THRESHOLD" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path} defines no module-level ALARM_THRESHOLD")


def _public_signatures(path: Path) -> dict[str, str]:
    """name -> rendered signature, for every public top-level function.

    `ast.unparse` renders defaults and annotations verbatim, so a changed
    default (`threshold: int = ALARM_THRESHOLD` becoming a literal), a dropped
    keyword-only marker, or a changed return type all show up as a diff.
    """
    out: dict[str, str] = {}
    for node in _module(path).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            returns = ast.unparse(node.returns) if node.returns else "<unannotated>"
            out[node.name] = f"({ast.unparse(node.args)}) -> {returns}"
    return out


@pytest.fixture
def sibling() -> Path:
    copies = _sibling_copies()
    if not copies:
        pytest.skip(
            f"no sibling source_health.py under {_repo_root()} — the bots may have "
            "been split into separate repos, so there is nothing to compare against"
        )
    assert len(copies) == 1, f"expected exactly one sibling copy, found {copies}"
    return copies[0]


def test_the_guard_reads_the_live_threshold():
    """Premise. If the ast reader ever stopped finding the real constant, the
    comparison below would pass vacuously — which is the failure mode of a
    drift alarm nobody notices is dead."""
    assert _alarm_threshold(_OWN) == source_health.ALARM_THRESHOLD


def test_alarm_threshold_agrees_across_the_copies(sibling):
    """Both bots must mean the same thing by "dead"."""
    assert _alarm_threshold(sibling) == source_health.ALARM_THRESHOLD, (
        f"ALARM_THRESHOLD drifted: {_OWN} says {source_health.ALARM_THRESHOLD}, "
        f"{sibling} says {_alarm_threshold(sibling)}. The two bots would disagree "
        "about what a dead source is. Change both, or neither."
    )


def test_public_signatures_agree_across_the_copies(sibling):
    """The rest of the contract: same public functions, same shapes.

    Not a byte-comparison — the docstrings and the `config` import differ on
    purpose. This catches a fix applied to one copy and forgotten in the other.
    """
    assert _public_signatures(sibling) == _public_signatures(_OWN), (
        f"the public API of {_OWN} and {sibling} has drifted — a change was "
        "applied to one copy and not the other"
    )
