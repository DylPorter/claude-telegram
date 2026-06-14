"""Tests for thread rendering: Telegram bubbles + daily-note audit markdown.

Key behaviours under test:
  - resolved (terminal) threads NEVER appear in the Telegram open-threads bubble
  - the questions bubble is OMITTED ENTIRELY when there are no questions
  - the daily-note audit keeps everything (with status glyphs) for the record
"""

from __future__ import annotations

from signal_brief.render import (
    render_threads_for_daily_note,
    render_threads_for_telegram,
)
from signal_brief.threads import ReconcileResult, Thread


def _result(threads, questions=None):
    return ReconcileResult(threads=threads, questions=questions or [], rationale="r", llm_ran=True)


def test_telegram_only_surfaces_active_threads():
    res = _result([
        Thread(id="a", title="Eletrolar", status="in_progress", detail="follow up Greg"),
        Thread(id="b", title="BOCHK", status="done"),
        Thread(id="c", title="Tracy", status="deferred"),
    ])
    msgs = render_threads_for_telegram(res)
    assert len(msgs) == 1  # only the open-threads bubble, no questions bubble
    bubble = msgs[0]
    assert "Eletrolar" in bubble
    assert "follow up Greg" in bubble
    assert "BOCHK" not in bubble       # done -> not surfaced
    assert "Tracy" not in bubble       # deferred -> not surfaced


def test_telegram_questions_bubble_omitted_when_empty():
    res = _result([Thread(id="a", title="A", status="open")], questions=[])
    msgs = render_threads_for_telegram(res)
    assert all("Quick check-ins" not in m for m in msgs)


def test_telegram_questions_bubble_present_when_questions_exist():
    res = _result([Thread(id="a", title="A", status="open")],
                  questions=["Send Tracy the message today?"])
    msgs = render_threads_for_telegram(res)
    assert any("Quick check-ins" in m for m in msgs)
    qbubble = [m for m in msgs if "Quick check-ins" in m][0]
    assert "Send Tracy the message today?" in qbubble


def test_telegram_empty_result_emits_nothing():
    res = _result([Thread(id="a", title="A", status="done")], questions=[])
    assert render_threads_for_telegram(res) == []


def test_daily_note_keeps_resolved_threads_with_glyphs():
    res = _result([
        Thread(id="a", title="Eletrolar", status="open", detail="ping Greg"),
        Thread(id="b", title="BOCHK", status="done"),
    ], questions=["Ship the deck?"])
    md = render_threads_for_daily_note(res)
    assert "## 🧵 Thread Reconciliation" in md
    assert "Eletrolar" in md and "BOCHK" in md  # audit keeps both
    assert "`done`" in md
    assert "Quick check-ins" in md
    assert "Ship the deck?" in md


def test_daily_note_handles_no_threads():
    res = _result([], questions=[])
    md = render_threads_for_daily_note(res)
    assert "No open threads tracked." in md
