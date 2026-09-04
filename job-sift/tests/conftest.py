"""Suite-wide guards.

Ported from `hk-events/tests/conftest.py`, which had both of these while this
suite — the one with FIVE sources to hk-events' four, and the one belonging to
the bot that actually went dark for fifty days — had neither. Same two autouse
fixtures, same reasoning, adapted to job-sift's fetchers.

Two guards, both about the same hazard: a test that reaches the real internet is
slow, flaky, and — worse for this repo specifically — can pass for the wrong
reason. The whole point of the raise-vs-empty contract is that an adapter's
return value is EVIDENCE about the outside world; a test that quietly fetched
the outside world is testing the weather.

`no_network` makes that structural rather than a matter of discipline. Nothing
in this suite opens a socket today; the guard is here so that stays true by
construction rather than by luck, and so the next adapter's tests cannot
silently start hitting Greenhouse.

`stub_all_sources` exists because `orchestrator._fetch_all_sources` runs whatever
`_source_tasks()` lists, and tests are written patching only the fetchers they
care about. Every unpatched adapter runs for real, so an assertion like
`errors == {"cedars": ...}` can start failing — or worse, PASSING — for reasons
that have nothing to do with the behaviour under test. Neutralising every source
by default makes each test opt IN to the sources it is actually about, and keeps
the next source addition from breaking the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from job_sift import config, refresh_cookie
from job_sift.errors import SourceNotConfiguredError
from job_sift.sources import ashby, cedars, greenhouse, lever, linkedin

# (module, attribute) for every source fetcher the orchestrator can call.
# Keep in step with `orchestrator._source_tasks`.
_FETCHERS = [
    (cedars, "fetch_cedars_listings"),
    (greenhouse, "fetch_greenhouse_listings"),
    (lever, "fetch_lever_listings"),
    (ashby, "fetch_ashby_listings"),
    (linkedin, "fetch_linkedin_listings"),
]


# The genuine functions, captured before anything patches the modules. A test
# that is ABOUT the real adapters (the total-outage reproduction, say) opts back
# in with `real_sources`.
_REAL = {(module.__name__, attr): getattr(module, attr) for module, attr in _FETCHERS}


# The drift guard for this list — "_FETCHERS still covers every source
# `_source_tasks` runs" — lives in test_cedars_parser.py, because pytest does
# not COLLECT tests out of a conftest.


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


def _not_opted_in(*args, **kwargs):
    # *args/**kwargs because `_source_tasks` calls cedars with `seen_ids=`.
    raise SourceNotConfiguredError(
        "test-stub", "this source was not opted into by the test (see tests/conftest.py)"
    )


@pytest.fixture(autouse=True)
def stub_all_sources(monkeypatch):
    """Every source is neutralised unless the test patches it with something else.

    monkeypatch.setattr is last-write-wins and this fixture runs first, so a test
    that patches `orchestrator.cedars.fetch_cedars_listings` still gets its own
    stub.

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
            "off saved fixtures, and adapter tests must patch the transport"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


# ---------------------------------------------------------------------------
# The real-write sandbox. Ported from hk-events/tests/conftest.py, which grew it
# after `TestDryRunWritesNoState` spent two days overwriting the operator's real
# vault note with fixture rows: it drove a LIVE `orchestrator.run(dry_run=False)`
# and had stubbed every outbound writer EXCEPT `write_archive`.
#
# This suite carries the same shape and a worse blast radius. `orchestrator.run`
# calls `write_open_roles` (line ~361), which rewrites the operator's rolling
# Open Roles REGISTER — a hand-annotated note, not a regenerable digest — and
# exactly one test in this suite stubs it. `vault_note` also used to bind
# `VAULT_ROOT` / `JOB_SIFT_ARCHIVE_DIR` / `OPEN_ROLES_PATH` at import time, so
# patching `config` did not redirect those writes at all; that is fixed, and
# this fixture is the guard that keeps it fixed.
#
# The default is inverted: EVERY configurable output path points into a per-test
# tmp dir, and a test must opt IN to touching anything real. monkeypatch is
# last-write-wins and autouse fixtures run first, so per-test patching still
# works exactly as before.

#: Captured at conftest import, BEFORE anything is patched.
#: DELIBERATELY NARROW — see the same note in hk-events/tests/conftest.py.
#: `STATE_DIR`, `COOKIE_DIR` and `LOG_DIR` are all written by
#: `job-sift-keepalive.timer` EVERY TEN MINUTES (it refreshes
#: `state/cedars_session.json` so the PHPSESSID does not idle out). Guarding
#: them means any suite run that straddles a tick fails falsely — observed
#: ticks at 01:22:38 and 01:33:38 during review. `VAULT_ROOT` is the whole
#: vault and is worse still.
#:
#: What remains is the set the SUITE writes and no timer does: the two vault
#: notes and the board.
_REAL_PATHS = {
    "ARCHIVE_DIR": config.JOB_SIFT_ARCHIVE_DIR,
    "OPEN_ROLES_PATH": config.OPEN_ROLES_PATH,
    "BOARD_PATH": config.BOARD_PATH,
}

#: Backed by a config attribute the fixture redirects — safe to clear.
_PATH_ENV_VARS = (
    "JOB_SIFT_BOARD_PATH",
    "JOB_SIFT_ARCHIVE_DIR",
    "JOB_SIFT_OPEN_ROLES_PATH",
    "JOB_SIFT_VAULT_ROOT",
    "DEFAULT_CWD",
)

#: NOT backed by any attribute: `events_feed_path()` falls through to a
#: hardcoded `BOT_ROOT/hk-events/.data/state/events_feed.json`, i.e. the
#: sibling project's real state. Clearing these steers a read OUT of the
#: sandbox, so they are SET rather than deleted.
_FEED_ENV_VARS = ("JOB_SIFT_JOBS_FEED", "JOB_SIFT_EVENTS_FEED")


def _snapshot(path):
    """(size, mtime_ns) per file, not mere existence — the notes this suite can
    clobber ALREADY EXIST on a real machine, so an existence check catches
    nothing."""
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
    """Point every configurable output path at a throwaway dir."""
    sandbox = tmp_path_factory.mktemp("sandbox")
    vault = sandbox / "vault"
    state = sandbox / "state"
    logs = sandbox / "logs"
    cookies = sandbox / "cookies"
    for d in (vault, state, logs, cookies):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "DATA_DIR", sandbox, raising=False)
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "LOG_DIR", logs)
    monkeypatch.setattr(config, "COOKIE_DIR", cookies)
    monkeypatch.setattr(config, "CEDARS_COOKIES_PATH", cookies / "cedars.json")
    monkeypatch.setattr(config, "VAULT_ROOT", vault)
    monkeypatch.setattr(config, "JOB_SIFT_ARCHIVE_DIR", vault / "Inbox" / "Job Sift")
    monkeypatch.setattr(config, "OPEN_ROLES_PATH", vault / "Areas" / "Work" / "Open Roles.md")
    monkeypatch.setattr(config, "BOARD_PATH", vault / "Areas" / "Work" / "Job Board.html")

    # `sources.cedars` and `refresh_cookie` still bind CEDARS_COOKIES_PATH at
    # import time. They only READ it, so this is a live-credential read rather
    # than a clobber — but redirect the bound names anyway so a test cannot
    # accidentally authenticate against the operator's real session.
    monkeypatch.setattr(cedars, "CEDARS_COOKIES_PATH", cookies / "cedars.json", raising=False)
    monkeypatch.setattr(
        refresh_cookie, "CEDARS_COOKIES_PATH", cookies / "cedars.json", raising=False
    )

    for name in _PATH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in _FEED_ENV_VARS:
        monkeypatch.setenv(name, str(state / f"{name.lower()}.json"))

    return sandbox


@pytest.fixture(scope="session", autouse=True)
def _assert_real_dirs_untouched():
    """After the whole session the REAL directories must be byte-for-byte what
    they were before it."""
    before = {name: _snapshot(p) for name, p in _REAL_PATHS.items()}
    yield
    damaged = [
        f"{name} -> {path}"
        for name, path in _REAL_PATHS.items()
        if _snapshot(path) != before[name]
    ]
    assert not damaged, (
        "the test suite wrote to REAL directories: "
        + ", ".join(damaged)
        + " — every output path must resolve through `config` at call time so "
        "the `sandbox_real_paths` fixture can redirect it"
    )
