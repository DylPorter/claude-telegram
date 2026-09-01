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

import pytest

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
