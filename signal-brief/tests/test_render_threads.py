"""Tests for thread rendering.

As of the 2026-09-04 Telegram diet there is no Telegram thread renderer at all
— "🧵 Your Open Threads" and "🔎 Quick check-ins" were cut from the phone by
explicit operator instruction. Reconciliation still runs every morning and the
daily-note audit section is unchanged, which is what these tests pin.

Key behaviours under test:
  - the daily-note audit keeps everything (with status glyphs) for the record
  - resolved (terminal) threads are still distinguishable from active ones
  - no Telegram path exists for threads any more
"""

from __future__ import annotations

from signal_brief import render
from signal_brief.render import render_threads_for_daily_note
from signal_brief.threads import ReconcileResult, Thread


def _result(threads, questions=None):
    return ReconcileResult(threads=threads, questions=questions or [], rationale="r", llm_ran=True)


def test_no_telegram_thread_renderer_exists():
    assert not hasattr(render, "render_threads_for_telegram")


def test_daily_note_keeps_resolved_threads_with_glyphs():
    res = _result([
        Thread(id="a", title="Northwind", status="open", detail="ping the intro"),
        Thread(id="b", title="Contoso", status="done"),
    ], questions=["Ship the deck?"])
    md = render_threads_for_daily_note(res)
    assert "## 🧵 Thread Reconciliation" in md
    assert "Northwind" in md and "Contoso" in md  # audit keeps both
    assert "`done`" in md
    assert "Quick check-ins" in md
    assert "Ship the deck?" in md


def test_daily_note_orders_active_threads_first():
    res = _result([
        Thread(id="b", title="Contoso", status="done"),
        Thread(id="a", title="Northwind", status="in_progress", detail="follow up the intro"),
        Thread(id="c", title="Client B", status="deferred"),
    ])
    md = render_threads_for_daily_note(res)
    assert md.index("Northwind") < md.index("Contoso")
    assert md.index("Northwind") < md.index("Client B")
    assert "follow up the intro" in md


def test_daily_note_omits_checkins_heading_when_no_questions():
    res = _result([Thread(id="a", title="A", status="open")], questions=[])
    md = render_threads_for_daily_note(res)
    assert "Quick check-ins" not in md


def test_daily_note_handles_no_threads():
    res = _result([], questions=[])
    md = render_threads_for_daily_note(res)
    assert "No open threads tracked." in md
