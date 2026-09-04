"""Suite-wide guards.

Two of them, both autouse, both about the same hazard: a test that reaches the
real internet is slow, flaky, and — worse for this repo specifically — can pass
for the wrong reason. The whole point of the raise-vs-empty contract is that an
adapter's return value is EVIDENCE about the outside world; a test that quietly
fetched the outside world is testing the weather.

`no_network` makes that structural rather than a matter of discipline.

`stub_all_sources` exists because `orchestrator._fetch_all_sources` runs whatever
`_source_tasks()` lists, and tests were written patching only the fetchers they
cared about. That was fine while the list held two sources and silently wrong the
moment it held four: the unpatched adapters ran for real, so an assertion like
`errors == {"meetup": ...}` started failing for reasons that had nothing to do
with the behaviour under test. Neutralising every source by default makes each
test opt IN to the sources it is actually about, and keeps the next source
addition from breaking the suite again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hk_events import config
from hk_events.errors import SourceNotConfiguredError
from hk_events.sources import aitinkerers, cyberport, luma, luma_discover, meetup, startmeuphk

# (module, attribute) for every source fetcher the orchestrator can call.
_FETCHERS = [
    (meetup, "fetch_meetup_events"),
    (luma, "fetch_luma_events"),
    (luma_discover, "fetch_luma_discover_events"),
    (aitinkerers, "fetch_aitinkerers_events"),
    (cyberport, "fetch_cyberport_events"),
    (startmeuphk, "fetch_startmeuphk_events"),
]


# The genuine functions, captured before anything patches the modules. A test
# that is ABOUT the real adapters (the total-outage reproduction, say) opts back
# in with `restore_real_sources`.
_REAL = {(module.__name__, attr): getattr(module, attr) for module, attr in _FETCHERS}


@pytest.fixture
def real_sources(monkeypatch):
    """Opt back in to the genuine adapters, undoing `stub_all_sources`.

    A fixture rather than an importable helper so it works regardless of how the
    suite is invoked — `tests/` carries no `__init__.py`, so `from tests.conftest
    import ...` depends on which directory pytest was started from.

    `no_network` still applies, so a test using this must patch the transport
    (that is the point: the total-outage reproduction patches `httpx.Client.get`
    to raise, and needs the real adapters to be the ones catching it).
    """

    def _restore() -> None:
        for module, attr in _FETCHERS:
            monkeypatch.setattr(module, attr, _REAL[(module.__name__, attr)])

    return _restore


def _not_opted_in():
    raise SourceNotConfiguredError(
        "test-stub", "this source was not opted into by the test (see tests/conftest.py)"
    )


@pytest.fixture(autouse=True)
def stub_all_sources(monkeypatch):
    """Every source is neutralised unless the test patches it with something else.

    monkeypatch.setattr is last-write-wins and this fixture runs first, so a test
    that patches `orchestrator.luma.fetch_luma_events` still gets its own stub.

    THE DEFAULT RAISES `SourceNotConfiguredError`, and the choice matters. The
    obvious stub is `lambda: []` — but `[]` is precisely the value this branch
    exists to distinguish from a raise, and `no_network` cannot catch it because a
    stub opens no socket. With `[]`, an un-opted-in source lands in `succeeded`,
    so a vacuous assertion like `succeeded == [...]` or `errors == {}` PASSES for
    a source the test never thought about. `SourceNotConfiguredError` puts it in
    NEITHER `succeeded` nor `errors` — the orchestrator's documented "nobody asked
    me anything" outcome — so a test that meant to assert something about that
    source fails loudly instead.
    """
    for module, attr in _FETCHERS:
        monkeypatch.setattr(module, attr, _not_opted_in, raising=True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly on any real socket connection from a test."""
    import socket

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "a test tried to open a network connection — parser tests must run "
            "off the saved fixtures in tests/fixtures/"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


# ---------------------------------------------------------------------------
# The real-write sandbox.
#
# WHY THIS EXISTS, in one sentence: on 2026-09-04 and again on 2026-09-05 a full
# suite run overwrote the operator's real vault note at
# `<VAULT_ROOT>/Inbox/HK Events/<today>.md` with fixture rows ("luma event 1",
# "luma event 2"), because `TestDryRunWritesNoState._run` drives a LIVE
# `orchestrator.run(dry_run=False)` and stubbed every outbound writer EXCEPT
# `write_archive`.
#
# The lesson is not "that test forgot a stub". It is that the suite's default
# was to write to real locations and each test had to opt OUT, one writer at a
# time, from memory. Every new writer re-opened the hole. So the default is
# inverted here: EVERY configurable output path points into a per-test tmp dir
# unless a test deliberately points it somewhere else.
#
# This composes with per-test patching rather than fighting it: monkeypatch is
# last-write-wins and autouse fixtures run first, so a test that does its own
# `monkeypatch.setattr(config, "STATE_DIR", tmp_path)` still gets its own path.
#
# Note the two shapes that have to be covered together. `config.STATE_DIR` and
# friends are module GLOBALS read inside call-time helpers, so patching the
# attribute redirects them. But `config.board_path()` and
# `config.events_feed_path()` also consult ENV VARS first, so an exported
# `HK_EVENTS_BOARD_PATH` in the developer's shell would win over the patched
# attribute. Both surfaces are closed below.

#: The genuine paths, captured at conftest import BEFORE anything is patched.
#: `_assert_real_dirs_untouched` compares against these at the end of the
#: session, so a leak is caught even if it escaped every per-test assertion.
_REAL_PATHS = {
    "STATE_DIR": config.STATE_DIR,
    "CACHE_DIR": config.CACHE_DIR,
    "LOG_DIR": config.LOG_DIR,
    "ARCHIVE_DIR": config.HK_EVENTS_ARCHIVE_DIR,
    "BOARD_PATH": config.BOARD_PATH,
    "VAULT_ROOT": config.VAULT_ROOT,
}

#: Env vars that can steer a write target. Cleared so a patched attribute is
#: not silently overridden by the developer's shell.
_PATH_ENV_VARS = (
    "HK_EVENTS_BOARD_PATH",
    "HK_EVENTS_EVENTS_FEED",
    "HK_EVENTS_JOBS_FEED",
    "HK_EVENTS_ARCHIVE_DIR",
    "HK_EVENTS_VAULT_ROOT",
    "DEFAULT_CWD",
)


def _snapshot(path):
    """(name, size, mtime_ns) for every file under `path`, or None if absent.

    Content-sensitive rather than existence-sensitive on purpose: the vault
    archive note for today ALREADY EXISTS on a real machine, so "the file is
    not there" would have caught nothing on either day this actually fired.
    """
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    if p.is_file():
        st = p.stat()
        return {p.name: (st.st_size, st.st_mtime_ns)}
    return {
        str(f.relative_to(p)): (f.stat().st_size, f.stat().st_mtime_ns)
        for f in p.rglob("*")
        if f.is_file()
    }


@pytest.fixture(autouse=True)
def sandbox_real_paths(monkeypatch, tmp_path_factory):
    """Point every configurable output path at a throwaway dir.

    A test must opt IN to touching anything real, by patching the attribute back
    to a real location itself. Nothing in this suite does, and nothing should.
    """
    sandbox = tmp_path_factory.mktemp("sandbox")
    vault = sandbox / "vault"
    state = sandbox / "state"
    cache = sandbox / "cache"
    logs = sandbox / "logs"
    for d in (vault, state, cache, logs):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "DATA_DIR", sandbox, raising=False)
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    monkeypatch.setattr(config, "LOG_DIR", logs)
    monkeypatch.setattr(config, "VAULT_ROOT", vault)
    monkeypatch.setattr(config, "HK_EVENTS_ARCHIVE_DIR", vault / "Inbox" / "HK Events")
    monkeypatch.setattr(config, "BOARD_PATH", vault / "Areas" / "Work" / "Events Board.html")

    for name in _PATH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    return sandbox


@pytest.fixture(scope="session", autouse=True)
def _assert_real_dirs_untouched():
    """The backstop: after the whole session, the REAL directories must be
    byte-for-byte what they were before it.

    This is the assertion that would have caught the vault overwrite on the
    first run rather than the second. It is session-scoped so it sees leaks from
    any test, including ones that patch their own paths and get them wrong.
    """
    before = {name: _snapshot(p) for name, p in _REAL_PATHS.items()}
    yield
    damaged = []
    for name, path in _REAL_PATHS.items():
        if _snapshot(path) != before[name]:
            damaged.append(f"{name} -> {path}")
    assert not damaged, (
        "the test suite wrote to REAL directories: "
        + ", ".join(damaged)
        + " — every output path must resolve through `config` at call time so "
        "the `sandbox_real_paths` fixture can redirect it"
    )
