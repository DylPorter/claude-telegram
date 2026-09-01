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

import pytest

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
