"""Newsletter body selection and excerpt windows.

WSJ ships a ~200-char preheader as text/plain and the whole issue as HTML.
Preferring text/plain unconditionally fed the filter a teaser instead of the
newsletter, so the parser takes whichever body is longer. Multi-story digests
also need a wider excerpt window than the filter's 400-char default, or only the
lead story is ever visible.
"""

from __future__ import annotations

from signal_brief.schema import Item
from signal_brief.sources import newsletters as nl


def test_html_wins_when_text_part_is_a_stub():
    stub = "Plus, three more stories. View in browser"
    html = "<p>" + " ".join(f"Story {n} body." for n in range(200)) + "</p>"
    assert "Story 199" in nl._pick_body({"body_text": stub, "body_html": html})


def test_text_wins_when_it_carries_the_content():
    text = "Real plaintext edition. " * 50
    assert nl._pick_body({"body_text": text, "body_html": "<p>short</p>"}) == text


def test_falls_back_to_snippet_when_both_bodies_empty():
    assert nl._pick_body({"body_text": "", "body_html": "", "snippet": "s"}) == "s"


def _fetch_with(monkeypatch, cfg, body_html):
    monkeypatch.setattr(nl, "_list_messages", lambda q, max_results: ["m1"])
    monkeypatch.setattr(nl, "_read_message", lambda _id: {
        "subject": "Issue", "date": "Wed, 16 Sep 2026 06:11:00 +0800",
        "body_text": "stub", "body_html": body_html,
    })
    return nl._fetch_one_newsletter(cfg)


def test_excerpt_chars_config_widens_the_window(monkeypatch):
    html = "<p>" + "word " * 2000 + "</p>"
    [wide] = _fetch_with(monkeypatch, {"id": "d", "query": "q", "excerpt_chars": 3000}, html)
    [default] = _fetch_with(monkeypatch, {"id": "d", "query": "q"}, html)
    assert len(wide.excerpt) == 3000 and wide.meta["excerpt_chars"] == 3000
    assert len(default.excerpt) == nl.DEFAULT_EXCERPT_CHARS


def test_filter_prompt_honours_per_item_excerpt_window():
    from signal_brief import filter as f
    long = "x" * 5000
    digest = Item(title="d", url="u1", source="wsj", source_kind="newsletter",
                  excerpt=long, meta={"excerpt_chars": 3000})
    plain = Item(title="p", url="u2", source="rss", source_kind="rss", excerpt=long)
    prompt = f._build_prompt([digest, plain], "2026-09-16")
    assert "x" * 3000 in prompt and "x" * 3001 not in prompt
