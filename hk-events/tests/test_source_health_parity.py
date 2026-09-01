"""The `source_health.py` copies must not drift apart.

`job-sift/job_sift/source_health.py` and `hk-events/hk_events/source_health.py`
are deliberate siblings — identical logic, differing only in their module
docstring and which package's `config` they import. Nothing enforces that, and
the field that matters most is a bare integer. If the two `ALARM_THRESHOLD`s
drift, the bots silently disagree about what "dead" means: an operator who has
learnt "the alarm fires on the third bad run" from one digest reasons wrongly
about the other, which is the same class of quiet wrongness the alarm exists to
kill.

Signatures are not enough. `update_health` builds its name list as
`[*errors, *succeeded]`, and that ORDER is what makes "errors wins over a
contradictory success claim" true — flip it and the signature is unchanged
while the semantics invert. So the comparison reaches into the bodies:
`ast.dump` of each public function with docstrings stripped, which ignores
comments and formatting but catches any change to what the code does.

This is a DRIFT ALARM, not a refactor. Merging the copies into a shared package
is a repo-structure decision — the monorepo is a candidate to be split into
per-bot repos — and it is not this test's to make. So:

  * the siblings are located RELATIVE to this module, never by absolute path,
    so the test survives a git worktree and a moved checkout;
  * they are read off disk and parsed with `ast`, never imported — importing
    another bot's package from this interpreter would fail on a dependency it
    does not have, and a test that fails for that reason teaches the operator to
    ignore it;
  * ANY number of siblings is fine. The comparison is pairwise against this
    copy, so adding a third bot adds a comparison rather than breaking the
    guard;
  * if there are no siblings — the split happened — this SKIPS. A guard that
    fails because the thing it compares against no longer exists is noise.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from hk_events import source_health

_OWN = Path(source_health.__file__).resolve()

# Constants that both copies must agree on. ALARM_THRESHOLD is the load-bearing
# one; the other two decide how much of an error string reaches Telegram and the
# vault, and what the state file is called — a divergence there is a divergence
# in what an operator can compare between the two bots.
_SHARED_CONSTANTS = ("ALARM_THRESHOLD", "_MAX_ERROR_CHARS", "STATE_FILENAME")


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


def _constants(path: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for node in _module(path).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in _SHARED_CONSTANTS:
                out[target.id] = ast.literal_eval(node.value)
    missing = set(_SHARED_CONSTANTS) - set(out)
    assert not missing, f"{path} defines no module-level {sorted(missing)}"
    return out


def _strip_docstrings(node: ast.AST) -> ast.AST:
    """Drop every docstring in the tree.

    Prose is allowed to differ — the two copies explain themselves to different
    readers, and the module docstrings already do. Behaviour is not.
    """
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(sub, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                sub.body = body[1:]
    return node


def _public_functions(path: Path) -> dict[str, str]:
    """name -> `ast.dump` of the whole function, docstrings stripped.

    Catches a changed signature (a dropped keyword-only marker, a changed
    default or return type) AND a changed body, which a signature comparison
    misses entirely.
    """
    out: dict[str, str] = {}
    for node in _module(path).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            out[node.name] = ast.dump(_strip_docstrings(copy.deepcopy(node)))
    return out


def _signature(path: Path, name: str) -> str:
    """A human-readable signature, for failure messages only."""
    for node in _module(path).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            returns = ast.unparse(node.returns) if node.returns else "<unannotated>"
            return f"{name}({ast.unparse(node.args)}) -> {returns}"
    return f"{name}: absent"


@pytest.fixture
def siblings() -> list[Path]:
    copies = _sibling_copies()
    if not copies:
        pytest.skip(
            f"no sibling source_health.py under {_repo_root()} — the bots may have "
            "been split into separate repos, so there is nothing to compare against"
        )
    return copies


def test_the_guard_reads_the_live_constants():
    """Premise. If the ast reader ever stopped finding the real values, the
    comparisons below would pass vacuously — which is the failure mode of a
    drift alarm nobody notices is dead."""
    own = _constants(_OWN)
    assert own["ALARM_THRESHOLD"] == source_health.ALARM_THRESHOLD
    assert own["_MAX_ERROR_CHARS"] == source_health._MAX_ERROR_CHARS
    assert own["STATE_FILENAME"] == source_health.STATE_FILENAME


def test_the_guard_reads_the_live_functions():
    """Premise, second half: the body reader must actually see the public API."""
    assert set(_public_functions(_OWN)) >= {
        "load_health",
        "save_health",
        "update_health",
        "stale_sources",
        "render_alarm",
        "dropped_while_stale",
        "render_drop_notice",
    }


def test_shared_constants_agree_across_the_copies(siblings):
    """Both bots must mean the same thing by "dead"."""
    own = _constants(_OWN)
    for sibling in siblings:
        assert _constants(sibling) == own, (
            f"a shared constant drifted between {_OWN} and {sibling}: "
            f"{own} vs {_constants(sibling)}. The two bots would disagree about "
            "what a dead source is. Change both, or neither."
        )


def test_public_functions_agree_across_the_copies(siblings):
    """Same public functions, same signatures, same bodies.

    Not a byte-comparison — docstrings, comments and formatting are free to
    differ, and the `config` import differs on purpose. This catches a fix
    applied to one copy and forgotten in the other.
    """
    own = _public_functions(_OWN)
    for sibling in siblings:
        theirs = _public_functions(sibling)

        assert set(theirs) == set(own), (
            f"the public API of {_OWN} and {sibling} has drifted: "
            f"only here {sorted(set(own) - set(theirs))}, "
            f"only there {sorted(set(theirs) - set(own))}"
        )
        for name in sorted(own):
            assert theirs[name] == own[name], (
                f"`{name}` differs between {_OWN} and {sibling} — a change was "
                "applied to one copy and not the other. "
                f"here: {_signature(_OWN, name)}; there: {_signature(sibling, name)}. "
                "(Signatures may look identical: the bodies are compared too.)"
            )
