"""x-drafts: sources, grounding lint, dedupe, rendering, and the end-to-end run
with the two LLM calls stubbed. No network, no `claude -p`, no real cache."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from signal_brief import x_drafts
from signal_brief.schema import Item
from signal_brief.sources.github_trending import parse_trending
from signal_brief.sources.hn import story_to_item

TODAY = date(2026, 9, 28)

FACTS = """# facts
## work
- lead engineer at SourcingGPT, 720 of 861 commits (84%).
- 15 AWS Lambda functions.

## never mention
- Globex
- Secret Client
"""


# ─── sources ────────────────────────────────────────────────────────────────

TRENDING_HTML = """
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/agent-thing" class="Link">
    <span class="text-normal">acme /</span> agent-thing</a></h2>
  <p class="col-9 color-fg-muted my-1 tmp-pr-4">
    Runs &amp; coordinates agents
  </p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/acme/agent-thing/stargazers" class="Link"><svg></svg>
    12,345</a>
  <span class="d-inline-block float-sm-right"><svg></svg> 1,024 stars today</span>
</article>
<article class="Box-row">
  <h2 class="h3"><a href="/b/c">b / c</a></h2>
</article>
"""


def test_parse_trending_reads_repo_desc_and_star_counts():
    items = parse_trending(TRENDING_HTML)
    assert [i.title for i in items] == ["acme/agent-thing", "b/c"]
    first = items[0]
    assert first.url == "https://github.com/acme/agent-thing"
    assert first.excerpt == "Runs & coordinates agents"
    assert first.meta == {"repo": "acme/agent-thing", "language": "Python",
                          "stars_today": 1024, "stars": 12345}
    # A row missing everything but the name still parses, with zeroes.
    assert items[1].meta["stars_today"] == 0


def test_parse_trending_on_changed_markup_returns_nothing():
    assert parse_trending("<html>no rows here</html>") == []


def test_hn_story_to_item_keeps_fresh_stories_and_drops_the_rest():
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    fresh = {"id": 1, "type": "story", "title": "Show HN: x", "score": 200,
             "descendants": 40, "time": int((now - timedelta(hours=3)).timestamp())}
    item = story_to_item(fresh, now=now)
    # No external url → the discussion is the story.
    assert item.url == "https://news.ycombinator.com/item?id=1"
    assert item.meta["points"] == 200 and item.meta["comments"] == 40

    stale = dict(fresh, time=int((now - timedelta(days=3)).timestamp()))
    job = dict(fresh, type="job")
    dead = dict(fresh, dead=True)
    assert story_to_item(stale, now=now) is None
    assert story_to_item(job, now=now) is None
    assert story_to_item(dead, now=now) is None


# ─── grounding material ─────────────────────────────────────────────────────

def test_never_mention_is_parsed_and_withheld_from_the_model():
    assert x_drafts.never_mention(FACTS) == ["Globex", "Secret Client"]
    claimable = x_drafts.claimable_facts(FACTS)
    assert "Globex" not in claimable and "SourcingGPT" in claimable


def test_extract_sections_takes_only_named_blocks():
    md = """# Voice
## DON'T patterns
- no delve
### sub of dont
- still inside
## Sample passages — client email
Bob said hello
### Markers
- lowercase [[PACL|pacl]] posts
### Professional-direct — client
Carol said hi
"""
    out = x_drafts.extract_sections(md, ("## DON'T patterns", "### Markers"))
    assert "no delve" in out and "still inside" in out
    assert "lowercase pacl posts" in out  # wikilink flattened
    assert "Bob" not in out and "Carol" not in out


# ─── lint ───────────────────────────────────────────────────────────────────

ALLOWED = {"720", "861", "84"}


def test_clean_post_passes():
    text = "wrote 720 of 861 commits on SourcingGPT and the thing i learned is that review is the job"
    assert x_drafts.lint_text(text, allowed_numbers=ALLOWED, forbidden=["Globex"]) == []


@pytest.mark.parametrize("text, expect", [
    ("x" * 281, "over 280"),
    ("we delve into agents", 'banned phrase "delve"'),
    ("agents — again", "em/en-dash"),
    ("ship it #buildinpublic", "hashtag"),
    ("ship it 🚀", "emoji"),
    ("see https://example.com", "link"),
    ("worked on the globex rollout", "private"),
    ("cut latency by 40% last week", "numbers not in FACTS"),
])
def test_lint_flags_each_rule(text, expect):
    problems = x_drafts.lint_text(text, allowed_numbers=ALLOWED, forbidden=["Globex"])
    assert any(expect in p for p in problems), problems


def test_numbers_from_the_topic_material_are_allowed():
    topics = [{"id": "c1", "title": "Model hits 93% on X", "signal": "HN, 400 points"}]
    drafts = [{"id": "c1", "post": "93% is nice, 400 points on HN agrees", "thread": None}]
    assert x_drafts.lint_drafts(drafts, topics, facts=FACTS, forbidden=[]) == {}


def test_hard_vs_soft_problems():
    assert x_drafts.is_hard('post: mentions "Globex", which is private')
    assert x_drafts.is_hard("post: numbers not in FACTS or topic material: 40")
    assert not x_drafts.is_hard("post: contains an em/en-dash")


# ─── dedupe ─────────────────────────────────────────────────────────────────

def _item(title, url, **meta):
    return Item(title=title, url=url, source="hn", source_kind="hn", meta=meta)


def test_collect_candidates_skips_topics_used_in_the_last_7_days(monkeypatch):
    items = [
        _item("Used yesterday", "https://a.com/x", hn_url="https://news.ycombinator.com/item?id=9"),
        _item("Used 10 days ago", "https://b.com/y"),
        _item("Fresh", "https://c.com/z"),
        _item("Fresh dup", "https://c.com/z/"),
    ]
    monkeypatch.setattr(x_drafts, "fetch_hn_front_page", lambda: items)
    monkeypatch.setattr(x_drafts, "fetch_github_trending", lambda: [])
    monkeypatch.setattr(x_drafts, "_fresh_rss", lambda: [])
    history = [
        # Matched through the HN discussion url, not the article url.
        {"date": "2026-09-27", "keys": ["news.ycombinator.com/item?id=9"]},
        {"date": "2026-09-18", "keys": ["b.com/y"]},
    ]
    cands = x_drafts.collect_candidates(today=TODAY, history=history)
    assert [c.item.title for c in cands] == ["Used 10 days ago", "Fresh"]


def test_save_history_prunes_old_entries():
    old = [{"date": "2026-08-01", "keys": ["x"]}, {"date": "2026-09-27", "keys": ["y"]}]
    x_drafts.save_history(old, [{"date": "2026-09-28", "keys": ["z"]}], today=TODAY)
    saved = json.loads(x_drafts.HISTORY_FILE.read_text())
    assert [h["date"] for h in saved] == ["2026-09-27", "2026-09-28"]


# ─── render ─────────────────────────────────────────────────────────────────

def _entry(cid, title, post, thread=None, issues=(), ptype="take"):
    cand = x_drafts.Candidate(cid=cid, item=_item(
        title, f"https://{cid}.com", points=300, comments=50,
        hn_url=f"https://news.ycombinator.com/item?id={cid}"))
    return {"cand": cand, "pick": {"why_trending": f"why {cid}", "type": ptype},
            "draft": {"id": cid, "post": post, "thread": thread}, "issues": list(issues)}


def test_render_is_header_then_one_plain_bubble_per_draft_then_thread():
    res = x_drafts.Result(date=TODAY, entries=[
        _entry("c1", "One", "post one", ptype="advice"),
        _entry("c2", "Two", "post two", thread=["t1", "t2"], ptype="provocative"),
        _entry("c3", "Three", "post three", issues=["post: contains an em/en-dash"], ptype="work"),
    ])
    msgs = x_drafts.render_messages(res)
    header = msgs[0]
    # Each topic is labelled with its post type, in order.
    assert "1. [advice] One" in header
    assert "2. [provocative] Two" in header
    assert "3. [work] Three" in header
    assert "https://c1.com" in header and "300 points" in header
    assert "check before posting: post: contains an em/en-dash" in header
    assert "thread option for #2" in header
    # Draft bubbles are the post text EXACTLY — nothing to trim before pasting.
    assert msgs[1:4] == ["post one", "post two", "post three"]
    assert msgs[5:] == ["t1", "t2"]
    assert not any("*" in m or "`" in m for m in msgs)


# ─── end to end (LLM stubbed) ───────────────────────────────────────────────

@pytest.fixture
def facts_file(tmp_path, monkeypatch):
    f = tmp_path / "facts.md"
    f.write_text(FACTS)
    monkeypatch.setattr(x_drafts, "FACTS_FILE", f)
    return f


def test_run_repairs_then_drops_a_draft_that_still_fabricates(monkeypatch, facts_file):
    items = [_item(f"Story {n}", f"https://s{n}.com") for n in range(1, 5)]
    monkeypatch.setattr(x_drafts, "fetch_hn_front_page", lambda: items)
    monkeypatch.setattr(x_drafts, "fetch_github_trending", lambda: [])
    monkeypatch.setattr(x_drafts, "_fresh_rss", lambda: [])
    monkeypatch.setattr(x_drafts, "fetch_context", lambda item: "page text")

    calls = []

    def fake_claude(system, user, *, model):
        calls.append(user)
        if "Pick exactly" in user:
            return {"picks": [{"id": "c1", "type": "work", "why_trending": "a"},
                              {"id": "c2", "type": "take", "why_trending": "b"},
                              {"id": "c3", "type": "advice", "why_trending": "c"}]}
        return {"drafts": [
            {"id": "c1", "post": "720 commits in and review is still the job"},
            {"id": "c2", "post": "cut costs by 63% last month"},  # invented number
            {"id": "c3", "post": "agents — fine", "thread": ["only one"]},
        ]}

    monkeypatch.setattr(x_drafts, "_claude", fake_claude)
    res = x_drafts.run(today=TODAY)

    assert len(calls) == 3  # select, write, one repair pass
    assert "63" in calls[2] and "Fix these problems" in calls[2]
    # The never-mention names are never shown to the model.
    assert not any("Globex" in c for c in calls)
    assert [e["cand"].cid for e in res.entries] == ["c1", "c3"]
    assert [d["cand"].cid for d in res.dropped] == ["c2"]
    c3 = res.entries[1]
    assert c3["draft"]["thread"] is None  # 1-tweet thread removed, post kept
    assert c3["issues"] == ["post: contains an em/en-dash"]


def test_orchestrator_pushes_plain_text_and_records_history(monkeypatch, facts_file):
    from signal_brief.orchestrators import x_drafts as orch

    res = x_drafts.Result(date=TODAY, entries=[_entry("c1", "One", "post one")])
    monkeypatch.setattr(orch.x_drafts, "run", lambda today: res)
    monkeypatch.setattr(orch, "assert_required", lambda: None)
    pushed = []
    monkeypatch.setattr(orch, "push_messages",
                        lambda msgs, **kw: pushed.append((msgs, kw)) or {"sent": msgs, "failed": []})

    assert orch.main(["--dry-run"]) == 0
    assert pushed == [] and not x_drafts.HISTORY_FILE.exists()

    assert orch.main([]) == 0
    msgs, kw = pushed[0]
    assert kw["parse_mode"] is None and msgs[1] == "post one"
    saved = json.loads(x_drafts.HISTORY_FILE.read_text())
    assert saved[0]["url"] == "https://c1.com"


def test_post_type_prefers_writer_then_selector_then_take():
    e = _entry("c1", "One", "p", ptype="advice")
    assert x_drafts.post_type(e) == "advice"
    e["draft"]["type"] = "provocative"
    assert x_drafts.post_type(e) == "provocative"
    e["draft"]["type"] = "rant"  # not a known type → fall back to the pick
    assert x_drafts.post_type(e) == "advice"
    e["pick"]["type"] = None
    assert x_drafts.post_type(e) == "take"


def test_a_take_may_cite_source_numbers_but_not_invent_stats():
    topics = [{"id": "c1", "type": "provocative",
               "title": "Survey: 62% of devs distrust AI code", "signal": "HN, 500 points"}]
    ok = [{"id": "c1", "post": "62% distrusting AI code is too low, you should distrust all code you didn't read"}]
    bad = [{"id": "c1", "post": "90% of agent startups will be dead in a year"}]
    assert x_drafts.lint_drafts(ok, topics, facts=FACTS, forbidden=[]) == {}
    issues = x_drafts.lint_drafts(bad, topics, facts=FACTS, forbidden=[])
    assert any("numbers not in FACTS" in p for p in issues["c1"])


def test_selected_type_reaches_the_writer():
    cand = x_drafts.Candidate(cid="c1", item=_item("T", "https://t.com"))
    block = x_drafts._topic_block(cand, {"type": "advice", "angle": "a"}, "ctx")
    assert block["type"] == "advice"
