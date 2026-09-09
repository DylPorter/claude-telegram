"""Thread reconciliation is gated OFF by default.

The pass held exactly one thread and could never resolve it: its evidence
window is `threads.RECENT_DAYS` days of daily-note live-capture plus the same
span of vault git commits, so a thread whose last update predates that window
has no evidence either way — no amount of running changes the answer. It cost
one Sonnet call every morning to re-derive it, and stated it more confidently
than the evidence carried.

So it is switched off, not deleted. What these cover:

  * OFF means INERT — no agent invocation, no daily-note section, no snapshot
    write. Merely not RENDERING the section would leave the Sonnet call, which
    is the entire cost being removed.
  * OFF does not destroy state. `threads.json` on disk is left byte-for-byte
    alone, so flipping the flag back on resumes from where it stopped rather
    than from nothing.
  * ON still works. A gate that quietly broke the feature it gates would only
    be discovered by the person turning it back on.
  * The absence is LOGGED, once, naming the variable — a future reader must
    find a reason where the section used to be, not a silent hole.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date as _date

import pytest

from signal_brief import config, threads as threads_mod
from signal_brief.orchestrators import morning
from signal_brief.schema import Digest, DigestSection
from signal_brief.threads import ReconcileResult, Thread


def _digest() -> Digest:
    return Digest(
        date="2026-09-09",
        headline="A headline.",
        sections=[
            DigestSection(title="Today's Signal", body="One."),
            DigestSection(title="Broad Tech/AI", body="Two."),
            DigestSection(title="Happening This Week", body="Three."),
            DigestSection(title="Bubble Breaker", body="Four."),
            DigestSection(title="Quiet rest", body="Five."),
        ],
        rationale="ranked by project hooks",
        suppressed=[],
    )


def _reconcile() -> ReconcileResult:
    return ReconcileResult(
        threads=[Thread(id="a", title="Northwind API test", status="open", detail="stale")],
        questions=["Still live, or drop it?"],
        rationale="no evidence either way",
        llm_ran=True,
    )


class _Run:
    """What a morning run actually did on its thread-shaped edges."""

    def __init__(self) -> None:
        self.reconciled = 0
        self.sections: list[str] = []
        self.snapshots: list[list[Thread]] = []


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Drive `morning.main()` with every outbound edge captured, not made."""
    rec = _Run()

    def _reconcile_threads(today):
        rec.reconciled += 1
        return _reconcile()

    monkeypatch.setattr(morning, "assert_required", lambda: None)
    monkeypatch.setattr(morning, "filter_items", lambda items, today: _digest())
    monkeypatch.setattr(morning, "reconcile_threads", _reconcile_threads)
    monkeypatch.setattr(morning, "push_messages",
                        lambda m: {"sent": list(m), "failed": []})
    monkeypatch.setattr(morning, "record_digest", lambda d: None)
    monkeypatch.setattr(morning, "upsert_signal_section",
                        lambda date_str, md: "fake/note.md")
    monkeypatch.setattr(
        morning, "upsert_threads_section",
        lambda date_str, md: (rec.sections.append(md), "fake/note.md")[1],
    )
    monkeypatch.setattr(
        morning, "save_threads",
        lambda t, date_str=None: rec.snapshots.append(list(t)),
    )

    items_file = tmp_path / "items.json"
    items_file.write_text(json.dumps([]))

    def _go(*extra_argv: str) -> _Run:
        monkeypatch.setattr(sys, "argv", ["morning", "--items-from", str(items_file), *extra_argv])
        assert morning.main() == 0
        return rec

    return _go


# ---------------------------------------------------------------------------
# The flag itself
# ---------------------------------------------------------------------------

class TestThreadsEnabledDefaultsOff:
    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(config.THREADS_ENABLED_ENV, raising=False)
        assert config.threads_enabled() is False

    @pytest.mark.parametrize("raw", ["", "   ", "0", "false", "FALSE", "no", "off"])
    def test_falsey_values_are_off(self, monkeypatch, raw):
        monkeypatch.setenv(config.THREADS_ENABLED_ENV, raw)
        assert config.threads_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_truthy_values_are_on(self, monkeypatch, raw):
        monkeypatch.setenv(config.THREADS_ENABLED_ENV, raw)
        assert config.threads_enabled() is True

    def test_an_unrecognised_value_is_off_and_loud(self, monkeypatch, caplog):
        """Guessing "on" for a typo is how a switched-off Sonnet call returns."""
        monkeypatch.setenv(config.THREADS_ENABLED_ENV, "maybe")
        with caplog.at_level(logging.WARNING):
            assert config.threads_enabled() is False
        assert config.THREADS_ENABLED_ENV in caplog.text

    def test_it_is_read_through_the_module_at_call_time(self, monkeypatch):
        """No import-time binding: setting the var must change the NEXT call.

        The convention this codebase has been bitten by repeatedly — see
        `job_sift/dedupe.py` for the write-to-real-state version of the bug.
        """
        monkeypatch.delenv(config.THREADS_ENABLED_ENV, raising=False)
        assert config.threads_enabled() is False
        monkeypatch.setenv(config.THREADS_ENABLED_ENV, "1")
        assert config.threads_enabled() is True


# ---------------------------------------------------------------------------
# OFF is inert
# ---------------------------------------------------------------------------

class TestOffIsInert:
    def test_no_reconcile_agent_is_invoked(self, run, monkeypatch):
        def _never(*a, **k):
            pytest.fail("reconcile_threads ran with the feature switched off")

        monkeypatch.setattr(morning, "reconcile_threads", _never)
        run()

    def test_no_daily_note_section_is_written(self, run):
        assert run().sections == []

    def test_no_snapshot_is_written(self, run):
        assert run().snapshots == []

    def test_the_signal_section_is_still_written(self, run, monkeypatch):
        """Switching threads off must not switch the brief off with it."""
        written: list[str] = []
        monkeypatch.setattr(
            morning, "upsert_signal_section",
            lambda date_str, md: (written.append(md), "fake/note.md")[1],
        )
        run()
        assert len(written) == 1
        assert "## 🌅 Morning Signal Brief" in written[0]

    def test_the_five_telegram_bubbles_are_unchanged(self, run, monkeypatch):
        sent: list[list[str]] = []
        monkeypatch.setattr(
            morning, "push_messages",
            lambda m: (sent.append(list(m)), {"sent": list(m), "failed": []})[1],
        )
        run()
        assert len(sent) == 1 and len(sent[0]) == 5
        assert "Northwind" not in "\n".join(sent[0])

    def test_the_disabled_state_is_logged_once_and_names_the_variable(self, run):
        """Read out of the run's own log FILE, not caplog.

        `morning._setup_logging` calls `logging.basicConfig(force=True)`, which
        tears out pytest's capture handler — so a caplog assertion here passes
        or fails on handler ordering rather than on what was logged. The file
        is what a human actually reads (`.data/logs/<date>-morning.log`), so it
        is what this asserts on.
        """
        run()
        log_file = morning.LOG_DIR / f"{_date.today().isoformat()}-morning.log"
        lines = [
            line for line in log_file.read_text().splitlines()
            if config.THREADS_ENABLED_ENV in line
        ]
        assert len(lines) == 1, "the absence must be explained exactly once"
        assert "DISABLED" in lines[0]

    def test_dry_run_prints_no_thread_section(self, run, capsys):
        run("--dry-run")
        out = capsys.readouterr().out
        assert "Thread Reconciliation" not in out
        assert "would push 5 Telegram messages" in out


class TestOffLeavesExistingStateAlone:
    """Turning a feature off must not destroy what it had already persisted."""

    def test_threads_json_is_left_byte_for_byte(self, run, monkeypatch):
        # The sandbox redirects THREADS_STATE_PATH; write a snapshot into it as
        # a real deployment would have, then run with the feature off.
        state = threads_mod.THREADS_STATE_PATH
        state.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"date": "2026-07-15", "threads": [{"id": "a", "title": "Northwind API test"}]}
        )
        state.write_text(payload)

        # The genuine writer, not the capture stub: if the run reaches it, the
        # file changes and this test says so.
        monkeypatch.setattr(morning, "save_threads", threads_mod.save_threads)
        run()

        assert state.exists(), "threads.json was deleted"
        assert state.read_text() == payload


# ---------------------------------------------------------------------------
# ON still works — the gate must not have broken what it gates
# ---------------------------------------------------------------------------

class TestOnRestoresTheFeature:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv(config.THREADS_ENABLED_ENV, "1")

    def test_the_agent_is_invoked(self, run):
        assert run().reconciled == 1

    def test_the_daily_note_section_comes_back(self, run):
        sections = run().sections
        assert len(sections) == 1
        assert "## 🧵 Thread Reconciliation" in sections[0]
        assert "Northwind API test" in sections[0]

    def test_the_snapshot_is_saved(self, run):
        snapshots = run().snapshots
        assert len(snapshots) == 1
        assert [t.title for t in snapshots[0]] == ["Northwind API test"]

    def test_dry_run_still_writes_nothing(self, run, capsys):
        rec = run("--dry-run")
        assert rec.sections == [] and rec.snapshots == []
        assert "Thread Reconciliation" in capsys.readouterr().out

    def test_a_crashing_pass_still_does_not_kill_the_brief(self, run, monkeypatch):
        written: list[str] = []
        monkeypatch.setattr(
            morning, "reconcile_threads",
            lambda today: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        monkeypatch.setattr(
            morning, "upsert_signal_section",
            lambda date_str, md: (written.append(md), "fake/note.md")[1],
        )
        rec = run()
        assert rec.sections == [] and rec.snapshots == []
        assert len(written) == 1
