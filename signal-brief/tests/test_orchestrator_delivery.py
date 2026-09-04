"""End-to-end delivery checks for the morning + evening orchestrators.

Runs `main()` with every outbound edge stubbed — no network, no `claude -p`
subprocess, no writes outside tmp_path. The property under test is the one the
diet must not break: **cutting a Telegram push must not cut the daily-note
write.**
"""

from __future__ import annotations

import json
import sys

import pytest

from signal_brief.schema import Digest, DigestSection, Item
from signal_brief.threads import ReconcileResult, Thread


@pytest.fixture
def pushes(monkeypatch):
    """Capture every Telegram push instead of making one."""
    sent: list[list[str]] = []

    def _fake_push(messages):
        sent.append(list(messages))
        return {"sent": list(messages), "failed": []}

    from signal_brief.orchestrators import evening, morning
    monkeypatch.setattr(morning, "push_messages", _fake_push)
    monkeypatch.setattr(evening, "push_messages", _fake_push)
    monkeypatch.setattr(morning, "assert_required", lambda: None)
    monkeypatch.setattr(evening, "assert_required", lambda: None)
    return sent


@pytest.fixture
def notes(monkeypatch):
    """Capture daily-note section writes instead of touching the vault."""
    written: dict[str, str] = {}

    from signal_brief.orchestrators import evening, morning

    def _sig(date_str, md):
        written["signal"] = md
        return "fake/note.md"

    def _threads(date_str, md):
        written["threads"] = md
        return "fake/note.md"

    def _evening(date_str, md):
        written["evening"] = md
        return "fake/note.md"

    monkeypatch.setattr(morning, "upsert_signal_section", _sig)
    monkeypatch.setattr(morning, "upsert_threads_section", _threads)
    monkeypatch.setattr(morning, "save_threads", lambda *a, **k: None)
    monkeypatch.setattr(morning, "record_digest", lambda d: None)
    monkeypatch.setattr(evening, "upsert_signal_section_evening", _evening)
    return written


def _run(module, argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)
    return module.main()


# --------------------------------------------------------------------------
# Morning
# --------------------------------------------------------------------------

def _morning_digest() -> Digest:
    return Digest(
        date="2026-09-04",
        headline="Astra crosses the Critical line.",
        sections=[
            DigestSection(title="Today's Signal", body="*Claude Code v2.1.259* ships managedMcpServers."),
            DigestSection(title="Broad Tech/AI", body="Three providers went down at once. Polars 2.0 is out."),
            DigestSection(title="Happening This Week", body="KubeCon opens Monday."),
            DigestSection(title="Bubble Breaker", body="Aeon on 'too muchness'. Worth five minutes."),
            DigestSection(title="Quiet rest", body="Dezeen ran three pieces; MR flagged law-firm fee cuts."),
        ],
        rationale="ranked by project hooks",
        suppressed=[Item(title="Audacity 4.0", url="https://x/1", source="hn", source_kind="rss")],
    )


def _reconcile() -> ReconcileResult:
    return ReconcileResult(
        threads=[Thread(id="a", title="Eletrolar API test", status="open", detail="stale since 07-15")],
        questions=["Still live, or drop it?"],
        rationale="no evidence either way",
        llm_ran=True,
    )


@pytest.fixture
def morning_stubs(monkeypatch, tmp_path):
    from signal_brief.orchestrators import morning
    monkeypatch.setattr(morning, "filter_items", lambda items, today: _morning_digest())
    monkeypatch.setattr(morning, "reconcile_threads", lambda today: _reconcile())
    items_file = tmp_path / "items.json"
    items_file.write_text(json.dumps([]))
    return items_file


def test_morning_pushes_five_bubbles_and_no_threads(monkeypatch, pushes, notes, morning_stubs):
    from signal_brief.orchestrators import morning
    rc = _run(morning, ["morning", "--items-from", str(morning_stubs)], monkeypatch)
    assert rc == 0
    assert len(pushes) == 1
    bubbles = pushes[0]
    assert len(bubbles) == 5
    joined = "\n".join(bubbles)
    assert "Open Threads" not in joined
    assert "Quick check-ins" not in joined
    assert "Eletrolar" not in joined
    assert "Happening This Week" not in joined
    assert "ranked by project hooks" not in joined
    assert "Audacity 4.0" not in joined


def test_morning_still_writes_both_daily_note_sections(monkeypatch, pushes, notes, morning_stubs):
    from signal_brief.orchestrators import morning
    _run(morning, ["morning", "--items-from", str(morning_stubs)], monkeypatch)

    signal = notes["signal"]
    assert "## 🌅 Morning Signal Brief" in signal
    assert "### Happening This Week" in signal          # note-only section kept
    assert "### Filter rationale" in signal             # telemetry kept
    assert "### Suppressed (deliberately dropped)" in signal
    assert "Audacity 4.0" in signal

    threads = notes["threads"]
    assert "## 🧵 Thread Reconciliation" in threads     # audit trail kept
    assert "Eletrolar API test" in threads
    assert "Quick check-ins" in threads
    assert "Still live, or drop it?" in threads


def test_morning_dry_run_pushes_nothing(monkeypatch, pushes, notes, morning_stubs, capsys):
    from signal_brief.orchestrators import morning
    rc = _run(morning, ["morning", "--dry-run", "--items-from", str(morning_stubs)], monkeypatch)
    assert rc == 0
    assert pushes == []
    assert notes == {}
    out = capsys.readouterr().out
    assert "would push 5 Telegram messages" in out


# --------------------------------------------------------------------------
# Evening
# --------------------------------------------------------------------------

def _vault_result(headline, sections):
    from signal_brief.vault_agent import VaultAgentResult
    return VaultAgentResult(
        headline=headline,
        sections=[DigestSection(title=t, body=b) for t, b in sections],
        rationale="created 2 notes, added 11 links",
    )


def test_evening_healthy_run_notifies_nothing(monkeypatch, pushes, notes):
    from signal_brief.orchestrators import evening
    monkeypatch.setattr(evening, "run_vault_agent", lambda p: _vault_result(
        "filed 3 inbox items, wired 11 links",
        [("Inbox processed", "3 items filed."),
         ("Links added", "11 wikilinks."),
         ("Git housekeeping", "committed and pushed."),
         ("Patterns / logs", "nothing new."),
         ("Tomorrow", "chase Tracy.")],
    ))
    rc = _run(evening, ["evening"], monkeypatch)
    assert rc == 0
    assert pushes == []                       # <-- the whole point of the cut


def test_evening_still_writes_its_full_note_section(monkeypatch, pushes, notes):
    from signal_brief.orchestrators import evening
    monkeypatch.setattr(evening, "run_vault_agent", lambda p: _vault_result(
        "filed 3 inbox items, wired 11 links",
        [("Inbox processed", "3 items filed."), ("Links added", "11 wikilinks.")],
    ))
    _run(evening, ["evening"], monkeypatch)
    md = notes["evening"]
    assert "## 🌙 Evening Sweep" in md
    assert "🌙 Evening — filed 3 inbox items" in md
    assert "### Inbox processed" in md
    assert "### Links added" in md
    assert "created 2 notes, added 11 links" in md


def test_evening_alarm_still_notifies_once(monkeypatch, pushes, notes):
    from signal_brief.orchestrators import evening
    monkeypatch.setattr(evening, "run_vault_agent", lambda p: _vault_result(
        "⚠️ Vault agent failed",
        [("Error", "Vault agent exited 1.")],
    ))
    rc = _run(evening, ["evening"], monkeypatch)
    assert rc == 0
    assert len(pushes) == 1 and len(pushes[0]) == 1
    assert "⚠️" in pushes[0][0]
    assert "## 🌙 Evening Sweep" in notes["evening"]


def test_evening_dry_run_pushes_nothing_and_writes_nothing(monkeypatch, pushes, notes, capsys):
    from signal_brief.orchestrators import evening
    monkeypatch.setattr(evening, "run_vault_agent", lambda p: _vault_result(
        "⚠️ Vault agent failed", [("Error", "exited 1.")]))
    rc = _run(evening, ["evening", "--dry-run"], monkeypatch)
    assert rc == 0
    assert pushes == []
    assert notes == {}
    assert "would push 1 Telegram messages" in capsys.readouterr().out
