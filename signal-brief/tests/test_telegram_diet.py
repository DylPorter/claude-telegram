"""Telegram diet (2026-09-04).

The operator cut the digest fleet down by name:

  keep  — morning intro, Today's Signal, Broad Tech/AI, Bubble Breaker, Quiet rest
  cut   — Inbox / Links added / Git housekeeping / Patterns / Tomorrow (evening),
          Your Open Threads, Quick check-ins, filter rationale, suppressed list

These tests pin the two halves of that: what Telegram is allowed to carry, and
that nothing cut from Telegram was also cut from the daily-note audit trail.
"""

from __future__ import annotations

from pathlib import Path

import signal_brief
from signal_brief import render
from signal_brief.render import (
    BUBBLE_CHAR_CAP,
    bulletize,
    render_alarm_for_telegram,
    render_for_daily_note,
    render_for_telegram,
    render_threads_for_daily_note,
    select_for_telegram,
)
from signal_brief.schema import Digest, DigestSection, Item
from signal_brief.threads import ReconcileResult, Thread

QUIET_REST_PROSE = (
    "Rest of the pile: Dezeen ran three architecture pieces; Marginal Revolution "
    "flagged AI-driven fee cuts at big law firms; a dozen arXiv papers on "
    "low-resource-language benchmarks — narrow, skipped unless you want the pile."
)

TODAYS_SIGNAL_PROSE = (
    "*Claude Code v2.1.259*: `managedMcpServers` for org-wide MCP. "
    "*GPT-6 Astra* shipped as OpenAI's most capable model."
)


def _full_digest() -> Digest:
    """A realistic morning digest: the four keepers plus note-only extras."""
    return Digest(
        date="2026-09-04",
        headline="GPT-6 Astra crosses the Critical cyber line.",
        sections=[
            DigestSection(title="Today's Signal", body=TODAYS_SIGNAL_PROSE),
            DigestSection(title="Broad Tech/AI",
                          body="OpenAI, Claude and Grok all went down together. "
                               "Qwen 3.8 27B now serves at 1500 tok/s on Cerebras."),
            DigestSection(title="Happening This Week",
                          body="KubeCon opens Monday in Amsterdam."),
            DigestSection(title="Bubble Breaker",
                          body="Neuroqueering on the lawn (Aeon) — a look at "
                               "'too muchness' and the limits of tolerance."),
            DigestSection(title="Quiet rest", body=QUIET_REST_PROSE),
        ],
        rationale="ranked X over Y because Z",
        suppressed=[Item(title="Audacity 4.0", url="https://x/1",
                         source="hn", source_kind="rss")],
    )


# --------------------------------------------------------------------------
# The five-bubble budget
# --------------------------------------------------------------------------

def test_morning_pushes_exactly_five_bubbles():
    msgs = render_for_telegram(_full_digest())
    assert len(msgs) == 5


def test_morning_bubbles_are_the_named_five():
    msgs = render_for_telegram(_full_digest())
    assert msgs[0].startswith("🌅 *2026-09-04*")
    for expected, msg in zip(
        ["Today's Signal", "Broad Tech/AI", "Bubble Breaker", "Quiet rest"],
        msgs[1:],
    ):
        assert expected in msg


def test_unlisted_section_never_reaches_telegram():
    msgs = render_for_telegram(_full_digest())
    assert all("Happening This Week" not in m for m in msgs)
    assert all("KubeCon" not in m for m in msgs)


def test_unlisted_section_still_lands_in_the_daily_note():
    md = render_for_daily_note(_full_digest())
    assert "### Happening This Week" in md
    assert "KubeCon" in md


def test_section_counter_counts_only_pushed_sections():
    # Not "(1/5)" — the note-only section must not inflate the denominator.
    msgs = render_for_telegram(_full_digest())
    assert "(1/4)" in msgs[1]
    assert "(4/4)" in msgs[4]


def test_rationale_and_suppressed_are_note_only():
    digest = _full_digest()
    msgs = render_for_telegram(digest)
    assert all("ranked X over Y" not in m for m in msgs)
    assert all("Audacity 4.0" not in m for m in msgs)
    md = render_for_daily_note(digest)
    assert "ranked X over Y" in md
    assert "Audacity 4.0" in md


# --------------------------------------------------------------------------
# Bullets — everything but Today's Signal
# --------------------------------------------------------------------------

def test_todays_signal_body_is_never_reflowed():
    msgs = render_for_telegram(_full_digest())
    signal = [m for m in msgs if "Today's Signal" in m][0]
    assert TODAYS_SIGNAL_PROSE in signal
    assert "•" not in signal


def test_quiet_rest_is_bulleted():
    msgs = render_for_telegram(_full_digest())
    quiet = [m for m in msgs if "Quiet rest" in m][0]
    body = quiet.split("\n\n", 1)[1]
    assert body.startswith("• ")
    assert body.count("• ") >= 3
    assert QUIET_REST_PROSE not in quiet


def test_broad_tech_and_bubble_breaker_are_bulleted():
    msgs = render_for_telegram(_full_digest())
    for title in ("Broad Tech/AI", "Bubble Breaker"):
        bubble = [m for m in msgs if title in m][0]
        assert "• " in bubble


def test_every_bubble_respects_the_char_cap():
    for m in render_for_telegram(_full_digest()):
        assert len(m) <= BUBBLE_CHAR_CAP + 120  # header + emoji overhead


def test_bulletize_is_idempotent_on_an_already_bulleted_body():
    already = "- Dezeen: three architecture pieces\n- MR: AI fee cuts at law firms"
    assert bulletize(already) == already


def test_bulletize_leaves_bold_lead_ins_alone():
    # `*bold*` opens the line but is not a bullet marker — must still bulletize.
    body = "*Polars 2.0* pre-release is out. *Qwen 3.8* serves at 1500 tok/s."
    out = bulletize(body)
    assert out.startswith("• *Polars 2.0*")
    assert out.count("• ") == 2


def test_bulletize_never_splits_inside_a_markdown_link():
    body = ("See [Claude Code v2.1.259](https://github.com/anthropics/cc/rel/v2.1.259) "
            "for the change. Second point here.")
    out = bulletize(body)
    assert "(https://github.com/anthropics/cc/rel/v2.1.259)" in out
    assert out.count("• ") == 2


def test_bulletize_does_not_split_decimals():
    assert bulletize("Qwen 3.8 27B is fast.").count("• ") == 1


def test_bulletize_trims_to_fit_the_cap():
    body = " ".join(f"Clause number {i} runs on for a while here." for i in range(20))
    out = bulletize(body)
    assert len(out) <= BUBBLE_CHAR_CAP
    assert out.startswith("• ")


def test_bulletize_handles_empty_body():
    assert bulletize("") == ""
    assert bulletize("   ") == ""


# --------------------------------------------------------------------------
# Threads — cut from Telegram, kept in the note
# --------------------------------------------------------------------------

def test_thread_telegram_renderer_is_gone():
    assert not hasattr(render, "render_threads_for_telegram")


def test_threads_and_checkins_still_written_to_the_daily_note():
    res = ReconcileResult(
        threads=[Thread(id="a", title="Eletrolar", status="open", detail="ping Greg")],
        questions=["Still live, or drop it?"],
        rationale="no evidence either way",
        llm_ran=True,
    )
    md = render_threads_for_daily_note(res)
    assert "## 🧵 Thread Reconciliation" in md
    assert "Eletrolar" in md
    assert "Quick check-ins" in md
    assert "Still live, or drop it?" in md


ORCHESTRATORS = Path(signal_brief.__file__).parent / "orchestrators"


def _orchestrator_src(name: str) -> str:
    """Read an orchestrator's source without importing it — agent_watch pulls
    feedparser, which the test env deliberately doesn't carry (no network deps
    in tests)."""
    return (ORCHESTRATORS / f"{name}.py").read_text()


def test_morning_orchestrator_does_not_import_a_thread_pusher():
    src = _orchestrator_src("morning")
    assert "render_threads_for_telegram" not in src
    assert "render_threads_for_daily_note" in src  # audit trail survives


# --------------------------------------------------------------------------
# Alarm lane — must survive the diet
# --------------------------------------------------------------------------

def test_alarm_section_bypasses_the_keep_list():
    digest = _full_digest()
    digest.sections.append(
        DigestSection(title="⚠️ Source health", body="twitter: all instances failed")
    )
    msgs = render_for_telegram(digest)
    assert any("Source health" in m for m in msgs)


def test_fallback_digest_still_reaches_telegram():
    digest = Digest(
        date="2026-09-04",
        headline="Fallback digest — LLM filter failed.",
        sections=[
            DigestSection(title="⚠️ Fallback digest (LLM filter failed)",
                          body="_LLM filter unavailable._"),
            DigestSection(title="hn-frontpage", body="*hn-frontpage* — 5 items"),
        ],
    )
    msgs = render_for_telegram(digest)
    assert any("Fallback digest" in m for m in msgs)


def test_unrecognised_sections_do_not_produce_a_silent_brief():
    digest = Digest(
        date="2026-09-04",
        headline="",
        sections=[DigestSection(title=f"Drifted title {i}", body=f"body {i}")
                  for i in range(6)],
    )
    kept = select_for_telegram(digest.sections)
    assert 0 < len(kept) <= 4


def test_evening_alarm_lane_is_silent_on_a_healthy_run():
    digest = Digest(
        date="2026-09-04",
        headline="🌙 Evening — filed 3 inbox items, wired 11 links.",
        sections=[
            DigestSection(title="Inbox processed", body="3 items filed."),
            DigestSection(title="Links added", body="11 wikilinks."),
            DigestSection(title="Tomorrow", body="Chase Tracy."),
        ],
    )
    assert render_alarm_for_telegram(digest) == []


def test_evening_alarm_lane_fires_once_when_the_agent_fails():
    digest = Digest(
        date="2026-09-04",
        headline="⚠️ Vault agent failed",
        sections=[DigestSection(title="Error", body="Vault agent exited 1.")],
    )
    msgs = render_alarm_for_telegram(digest)
    assert len(msgs) == 1
    assert "⚠️" in msgs[0]
    assert len(msgs[0]) <= BUBBLE_CHAR_CAP


def test_evening_still_writes_its_full_summary_to_the_note():
    digest = Digest(
        date="2026-09-04",
        headline="🌙 Evening — filed 3 inbox items.",
        sections=[DigestSection(title="Inbox processed", body="3 items filed."),
                  DigestSection(title="Links added", body="11 wikilinks.")],
        rationale="created 2 notes, added 11 links",
    )
    md = render_for_daily_note(digest).replace(
        "## 🌅 Morning Signal Brief", "## 🌙 Evening Sweep")
    assert "## 🌙 Evening Sweep" in md
    assert "Inbox processed" in md
    assert "Links added" in md
    assert "created 2 notes, added 11 links" in md


def test_evening_orchestrator_pushes_only_the_alarm_lane():
    src = _orchestrator_src("evening")
    assert "render_alarm_for_telegram" in src
    assert "render_for_telegram" not in src.replace("render_alarm_for_telegram", "")
    # the vault work + audit trail must be untouched
    assert "run_vault_agent" in src
    assert "upsert_signal_section_evening" in src
    assert "Process Inbox" in src
    assert "Orphan / under-linked sweep" in src


def test_agent_watch_still_pushes():
    """The trip-wire is NOT part of the diet: it pushed 0 messages in the 14
    days before the cut (every run 'quiet run — nothing tripped'), so silencing
    it saves nothing and costs the one alert it exists to deliver."""
    src = _orchestrator_src("agent_watch")
    assert "push_messages(messages)" in src


def test_zero_item_day_is_flagged_as_a_source_health_alarm():
    """A day with no items means the sources are probably down. That has to be
    visible, not swallowed as a quiet morning."""
    from signal_brief.filter import filter_items
    digest = filter_items([], today="2026-09-04")
    msgs = render_for_telegram(digest)
    assert msgs, "a zero-item day must still notify"
    assert any("⚠️" in m for m in msgs)
    assert render_alarm_for_telegram(digest)


def test_every_kept_section_gets_a_real_glyph():
    """A bullet-point header glyph next to bullet-point content is unreadable —
    each of the four kept sections needs its own emoji."""
    msgs = render_for_telegram(_full_digest())
    for m in msgs[1:]:
        assert not m.startswith("• *"), m.splitlines()[0]


def test_alarm_bodies_are_not_reflowed():
    """`_italic across a sentence boundary._` breaks if you bullet-split it."""
    digest = Digest(
        date="2026-09-04",
        headline="⚠️ Quiet day — no signal collected.",
        sections=[DigestSection(
            title="⚠️ Quiet day — no items collected",
            body="_No signal items collected. Sources may be down — check logs._",
        )],
    )
    bubble = render_for_telegram(digest)[1]
    assert "_No signal items collected. Sources may be down — check logs._" in bubble
    assert "•" not in bubble


def test_weekly_review_is_not_dieted():
    """The weekly review was not in scope. Silently cutting it from ~10 bubbles
    to 4 because its section titles don't match a DAILY keep-list would be a
    change nobody asked for."""
    digest = Digest(
        date="2026-09-06",
        headline="Week frame: shipped the Android port.",
        sections=[DigestSection(title=t, body=f"{t} body.") for t in
                  ["Week frame", "Patterns", "Active threads — kill/commit calls",
                   "🔗 Link health", "Auto-applied", "Stub candidates", "Graph health"]],
    )
    assert len(render_for_telegram(digest, restrict_sections=False)) == 8
    assert len(render_for_telegram(digest)) == 5  # dieted path unchanged


def test_weekly_orchestrator_opts_out():
    from pathlib import Path as _P
    src = (_P(signal_brief.__file__).parent / "orchestrators" / "weekly.py").read_text()
    assert "restrict_sections=False" in src
