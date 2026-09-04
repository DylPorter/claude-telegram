"""Suite-wide guard: no test may write to the real vault or the real .data dir.

WHY THIS FILE IS AT THE PROJECT ROOT AND NOT IN tests/
------------------------------------------------------
`signal_brief/config.py` calls `load_dotenv()` and then resolves every path
CONSTANT at import time (`VAULT_ROOT` from DEFAULT_CWD at line 24, `DATA_DIR` /
`CACHE_DIR` / `LOG_DIR` at 26-28, and it mkdirs them at 31-32). By the time a
fixture body runs, those constants are already frozen. So the sandbox
environment has to be in `os.environ` BEFORE the first `import signal_brief`,
which means module scope of a conftest pytest loads first — the project root.

`load_dotenv` does not override variables already present in the environment,
so setting `SIGNAL_BRIEF_VAULT_ROOT` here wins over the `DEFAULT_CWD` in the
shared `.env` without editing that file.

WHY IT EXISTS AT ALL
--------------------
This project had no conftest.py whatsoever, and the suite was writing to the
operator's real `.data/logs/` on every run — `<today>-morning.log`,
`-evening.log` and `-weekly.log`, all stamped with the second the suite ran
rather than the 07:02 / 22:03 / Sun 20:14 the cron actually fires. It went
unnoticed only because `.gitignore` covers `.data/logs/*.log`.

It is the same defect that had hk-events overwriting a real vault note with
fixture rows two days running: a path bound at import time, a writer nobody
remembered to stub, and a suite whose DEFAULT was to write somewhere real. The
daily-note writer (`daily_note.py:39-56`, reachable from both `morning` and
`evening`) was held back by nothing but hand-maintained per-test stubs in
`test_orchestrator_delivery.py` — opt-out-from-memory, which is exactly the
pattern this guard abolishes.

Every module here binds its paths with `from signal_brief.config import <X>`,
and three modules freeze them a second time into their own constants
(`exposure.EXPOSURE_FILE`, `sources.rss.SEEN_CACHE`,
`threads.THREADS_STATE_PATH`). Patching `config` alone therefore redirects
nothing. The fixture below patches the config module AND every bound name.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

#: A realistic multi-section Home note, seeded into every sandbox vault. Lets
#: `test_replace_against_real_home_preserves_rest_byte_for_byte` actually RUN:
#: it resolves `config.HOME_NOTE` and skips if the file is absent, so pointing
#: the vault at an empty tmp dir silently disabled it on every machine forever.
_HOME_FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "home_sample.md"

# ---------------------------------------------------------------------------
# STEP 1 — sandbox the environment BEFORE signal_brief is imported anywhere.
_SESSION_SANDBOX = Path(tempfile.mkdtemp(prefix="signal-brief-sandbox-"))
# Removed at interpreter exit; without this every run leaked a /tmp dir.
atexit.register(shutil.rmtree, _SESSION_SANDBOX, True)
_SANDBOX_VAULT = _SESSION_SANDBOX / "vault"
for _sub in ("Daily Notes", "Reviews", "Inbox", ".claude-memory", "Areas/Personal"):
    (_SANDBOX_VAULT / _sub).mkdir(parents=True, exist_ok=True)
shutil.copyfile(_HOME_FIXTURE, _SANDBOX_VAULT / "Home.md")

os.environ["SIGNAL_BRIEF_VAULT_ROOT"] = str(_SANDBOX_VAULT)
os.environ["SIGNAL_BRIEF_DAILY_NOTES_DIR"] = str(_SANDBOX_VAULT / "Daily Notes")
os.environ["SIGNAL_BRIEF_REVIEWS_DIR"] = str(_SANDBOX_VAULT / "Reviews")
os.environ["SIGNAL_BRIEF_INBOX_DIR"] = str(_SANDBOX_VAULT / "Inbox")
os.environ["SIGNAL_BRIEF_MEMORY_DIR"] = str(_SANDBOX_VAULT / ".claude-memory")

# STEP 2 — now it is safe to import.
import pytest  # noqa: E402

from signal_brief import config, daily_note, exposure, filter as sb_filter, threads, vault_agent  # noqa: E402
from signal_brief.orchestrators import agent_watch, evening, morning, weekly  # noqa: E402
from signal_brief.sources import rss  # noqa: E402

#: The genuine directories this suite could plausibly write, captured before
#: anything is patched. Deliberately NARROW: `VAULT_ROOT` itself is ~1800 files
#: written continuously by four cron timers, gbrain sync and every git
#: operation, so guarding it would fail for reasons that have nothing to do
#: with the suite. An alarm nobody trusts is worse than no alarm.
_REAL_PATHS = {
    "LOG_DIR": config.LOG_DIR,
    "CACHE_DIR": config.CACHE_DIR,
}


def _snapshot(path):
    """(size, mtime_ns) per file — not existence. These files already exist on
    a real machine, so an existence check detects nothing."""
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return {
        str(f.relative_to(p)): (f.stat().st_size, f.stat().st_mtime_ns)
        for f in p.rglob("*")
        if f.is_file()
    }


@pytest.fixture(autouse=True)
def sandbox_real_paths(monkeypatch, tmp_path_factory):
    """Point every path at a throwaway dir — the config module AND every name
    bound off it at import time."""
    sandbox = tmp_path_factory.mktemp("sb")
    vault = sandbox / "vault"
    cache = sandbox / "cache"
    logs = sandbox / "logs"
    daily = vault / "Daily Notes"
    reviews = vault / "Reviews"
    inbox = vault / "Inbox"
    memory = vault / ".claude-memory"
    for d in (vault, cache, logs, daily, reviews, inbox, memory, vault / "Areas" / "Personal"):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_HOME_FIXTURE, vault / "Home.md")

    # The config module itself.
    for name, value in (
        ("DATA_DIR", sandbox),
        ("CACHE_DIR", cache),
        ("LOG_DIR", logs),
        ("VAULT_ROOT", vault),
        ("DAILY_NOTES_DIR", daily),
        ("REVIEWS_DIR", reviews),
        ("INBOX_DIR", inbox),
        ("MEMORY_DIR", memory),
        ("MEMORY_INDEX", memory / "MEMORY.md"),
        # Without these two the sandbox was TWO-TIER: `config.VAULT_ROOT` was
        # the per-test dir while HOME_NOTE/DONE_LOG_NOTE still pointed into the
        # session-scope one. Both are sandbox paths, so it was never a
        # real-write hazard — but a split like that is a trap for the next test.
        ("HOME_NOTE", vault / "Home.md"),
        ("DONE_LOG_NOTE", vault / "Areas" / "Personal" / "Done Log.md"),
    ):
        monkeypatch.setattr(config, name, value, raising=False)

    # Every name bound at import time in a consuming module. Patching `config`
    # alone does NOT reach these — that is the whole bug.
    for module, attr, value in (
        (daily_note, "DAILY_NOTES_DIR", daily),
        (exposure, "CACHE_DIR", cache),
        (exposure, "EXPOSURE_FILE", cache / "exposure_log.json"),
        (rss, "CACHE_DIR", cache),
        (rss, "SEEN_CACHE", cache / "rss_seen.json"),
        (threads, "CACHE_DIR", cache),
        (threads, "THREADS_STATE_PATH", cache / "threads.json"),
        (threads, "DAILY_NOTES_DIR", daily),
        (threads, "VAULT_ROOT", vault),
        (vault_agent, "VAULT_ROOT", vault),
        # filter.py binds VAULT_ROOT at import too, and spawns the THIRD
        # bypassPermissions subprocess in this package (filter.py:275-280).
        (sb_filter, "VAULT_ROOT", vault),
        (agent_watch, "CACHE_DIR", cache),
        (morning, "LOG_DIR", logs),
        (evening, "LOG_DIR", logs),
        (evening, "VAULT_ROOT", vault),
        (weekly, "LOG_DIR", logs),
        (weekly, "REVIEWS_DIR", reviews),
    ):
        monkeypatch.setattr(module, attr, value, raising=False)

    return sandbox


@pytest.fixture(autouse=True)
def no_agent_spawn(monkeypatch):
    """`threads.py`, `vault_agent.py` AND `filter.py` each spawn

        claude -p --permission-mode bypassPermissions   (cwd=VAULT_ROOT)

    Every test today patches `subprocess`, so none of them does. But "every test
    remembers" is precisely the assurance that failed in hk-events, and the
    failure mode here is a real agent running with permissions bypassed against
    the live vault — far worse than a clobbered markdown file.

    So it is unreachable by construction now. This inspects argv rather than
    blocking `subprocess.run` wholesale, because pytest and its plugins use
    subprocess legitimately and a blanket block would be its own outage.
    """
    real_run = subprocess.run

    def _guarded(cmd, *args, **kwargs):
        argv = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
        joined = " ".join(argv)
        # `argv[0]` on an empty list raised IndexError from inside the guard,
        # masking the stdlib's own error. Defer to subprocess for that case.
        head = os.path.basename(argv[0]) if argv and argv[0] else ""
        if "bypassPermissions" in joined or head == "claude":
            raise AssertionError(
                "a test tried to spawn a real Claude agent "
                f"({joined[:120]!r}, cwd={kwargs.get('cwd')!r}). Patch "
                "`subprocess` in the module under test — never let the suite "
                "run an agent with bypassPermissions against a real vault."
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guarded)


@pytest.fixture(scope="session", autouse=True)
def _assert_real_dirs_untouched():
    """Backstop: the real `.data/logs` and `.data/cache` survive the session
    byte-for-byte. This is the check that would have caught the stray
    `<today>-morning.log` on the first run instead of never."""
    before = {name: _snapshot(p) for name, p in _REAL_PATHS.items()}
    yield
    damaged = [
        f"{name} -> {path}"
        for name, path in _REAL_PATHS.items()
        if _snapshot(path) != before[name]
    ]
    assert not damaged, (
        "the test suite wrote to REAL directories: " + ", ".join(damaged)
    )
