"""Tests for thread reconciliation, brevity enforcement, and state persistence.

These cover the pure logic (no LLM call): the ≤3-question cap, omit-if-empty,
state round-tripping that drops terminal threads, live-capture extraction, and
the clean-degradation fallback. The LLM call itself is patched out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from signal_brief import threads as T
from signal_brief.threads import (
    MAX_QUESTIONS,
    ReconcileResult,
    Thread,
    cap_questions,
    load_threads,
    reconcile_threads,
    save_threads,
    _extract_live_capture,
    _fallback_reconcile,
    _parse_reconcile,
)


# --------------------------------------------------------------------------- #
# cap_questions — the HARD brevity constraint
# --------------------------------------------------------------------------- #

def test_cap_questions_hard_caps_at_max():
    qs = [f"question {i}?" for i in range(10)]
    out = cap_questions(qs)
    assert len(out) == MAX_QUESTIONS == 3
    # keeps the first (highest-priority) ones
    assert out == ["question 0?", "question 1?", "question 2?"]


def test_cap_questions_drops_blanks_and_whitespace():
    out = cap_questions(["", "   ", "real?", None])  # type: ignore[list-item]
    assert out == ["real?"]


def test_cap_questions_collapses_to_one_line():
    out = cap_questions(["line one\nstill same question?"])
    assert out == ["line one still same question?"]
    assert "\n" not in out[0]


def test_cap_questions_dedupes_case_insensitive():
    out = cap_questions(["Send Tracy?", "send tracy?", "Other?"])
    assert out == ["Send Tracy?", "Other?"]


def test_cap_questions_empty_prefers_zero():
    assert cap_questions([]) == []
    assert cap_questions(["", "  "]) == []


# --------------------------------------------------------------------------- #
# State persistence — terminal threads must never carry forward
# --------------------------------------------------------------------------- #

def test_save_threads_drops_terminal_statuses(tmp_path: Path):
    path = tmp_path / "threads.json"
    ts = [
        Thread(id="a", title="Active", status="open"),
        Thread(id="b", title="WIP", status="in_progress"),
        Thread(id="c", title="Done", status="done"),
        Thread(id="d", title="Deferred", status="deferred"),
        Thread(id="e", title="Dropped", status="dropped"),
    ]
    save_threads(ts, date_str="2026-06-14", path=path)
    loaded = load_threads(path)
    ids = {t.id for t in loaded}
    assert ids == {"a", "b"}  # only active threads survive into tomorrow


def test_save_load_roundtrip(tmp_path: Path):
    path = tmp_path / "threads.json"
    ts = [Thread(id="x", title="X", status="open", detail="next step", last_updated="2026-06-14")]
    save_threads(ts, date_str="2026-06-14", path=path)
    loaded = load_threads(path)
    assert loaded[0].detail == "next step"
    assert loaded[0].last_updated == "2026-06-14"


def test_load_threads_missing_file_returns_empty(tmp_path: Path):
    assert load_threads(tmp_path / "nope.json") == []


def test_load_threads_corrupt_file_returns_empty(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert load_threads(path) == []


def test_load_threads_ignores_unknown_fields(tmp_path: Path):
    path = tmp_path / "threads.json"
    path.write_text(json.dumps({"threads": [
        {"id": "a", "title": "A", "status": "open", "bogus": 1}
    ]}))
    loaded = load_threads(path)
    assert loaded[0].id == "a"


# --------------------------------------------------------------------------- #
# Live-capture extraction
# --------------------------------------------------------------------------- #

def test_extract_live_capture_pulls_log_and_journal_skips_brief():
    note = (
        "# 2026-06-14\n\n"
        "## 🌅 Morning Signal Brief\n"
        "### Your Open Threads\n- stale wrong thread\n"
        "<!-- signal-brief:end -->\n\n"
        "## Morning\n> prompt\n\n"
        "## Log\n- 09:00 — submitted BOCHK deck\n\n"
        "## Journal\nFelt good about shipping.\n\n"
        "## Connections Noticed Today\n- A <-> B\n"
    )
    out = _extract_live_capture(note)
    assert "submitted BOCHK deck" in out
    assert "Felt good about shipping" in out
    # must NOT leak the stale morning-brief thread back in
    assert "stale wrong thread" not in out


def test_extract_live_capture_empty_when_no_sections():
    assert _extract_live_capture("# 2026-06-14\n\njust a title") == ""


# --------------------------------------------------------------------------- #
# Parsing LLM output
# --------------------------------------------------------------------------- #

def test_parse_reconcile_normalizes_bad_status_and_caps_questions():
    parsed = {
        "threads": [
            {"id": "a", "title": "A", "status": "DONE"},
            {"title": "No id", "status": "weird-status"},
        ],
        "questions": ["q1?", "q2?", "q3?", "q4?", "q5?"],
        "rationale": "did stuff",
    }
    res = _parse_reconcile(parsed, "2026-06-14")
    assert res.llm_ran is True
    assert res.threads[0].status == "done"
    assert res.threads[1].status == "open"  # invalid -> open
    assert res.threads[1].id == "no-id"     # slugged from title
    assert len(res.questions) == 3          # capped


def test_active_threads_filters_terminal():
    res = ReconcileResult(threads=[
        Thread(id="a", title="A", status="open"),
        Thread(id="b", title="B", status="done"),
    ])
    assert [t.id for t in res.active_threads()] == ["a"]


# --------------------------------------------------------------------------- #
# Reconcile — degradation paths (LLM patched out)
# --------------------------------------------------------------------------- #

def test_reconcile_no_prior_threads_does_nothing():
    res = reconcile_threads(prior=[], today="2026-06-14", context={})
    assert res.threads == []
    assert res.questions == []
    assert res.llm_ran is False


def test_reconcile_subprocess_failure_carries_prior_active(monkeypatch):
    prior = [
        Thread(id="a", title="A", status="open"),
        Thread(id="b", title="B", status="done"),
    ]

    def boom(*args, **kwargs):
        raise OSError("claude not found")

    monkeypatch.setattr(T.subprocess, "run", boom)
    res = reconcile_threads(prior=prior, today="2026-06-14", context={"daily_notes": "", "git": ""})
    # carries prior ACTIVE threads, asks nothing
    assert [t.id for t in res.threads] == ["a"]
    assert res.questions == []
    assert res.llm_ran is False


def test_reconcile_nonzero_exit_falls_back(monkeypatch):
    prior = [Thread(id="a", title="A", status="open")]

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "kaboom"

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: FakeProc())
    res = reconcile_threads(prior=prior, today="2026-06-14", context={})
    assert res.llm_ran is False
    assert [t.id for t in res.threads] == ["a"]


def test_reconcile_parses_valid_llm_output(monkeypatch):
    prior = [Thread(id="bochk", title="BOCHK", status="open")]
    payload = json.dumps({
        "threads": [{"id": "bochk", "title": "BOCHK", "status": "done", "detail": "submitted"}],
        "questions": [],
        "rationale": "BOCHK submitted per Log.",
    })

    class FakeProc:
        returncode = 0
        stdout = payload
        stderr = ""

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: FakeProc())
    res = reconcile_threads(prior=prior, today="2026-06-14",
                            context={"daily_notes": "submitted BOCHK", "git": ""})
    assert res.llm_ran is True
    assert res.threads[0].status == "done"
    assert res.active_threads() == []  # nothing live -> no open-threads bubble


def test_fallback_reconcile_asks_nothing():
    prior = [Thread(id="a", title="A", status="open"), Thread(id="b", title="B", status="dropped")]
    res = _fallback_reconcile(prior)
    assert res.questions == []
    assert [t.id for t in res.threads] == ["a"]
