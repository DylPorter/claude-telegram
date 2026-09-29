"""Daily X/Twitter post drafts, delivered to Telegram for manual posting.

No X API anywhere, by design: this module only READS free public sources (HN
API, GitHub trending, the AI/dev RSS feeds signal-brief already configures) and
WRITES Telegram messages. The operator posts by hand.

Pipeline (driven by `orchestrators/x_drafts.py`):

  1. collect_candidates()   HN front page + GitHub trending + fresh AI/dev RSS,
                            minus anything used in the last DEDUPE_DAYS
  2. select_topics()        LLM call #1 — pick the 3 best, by candidate id
  3. fetch_context()        pull a readable excerpt of each chosen page, so the
                            drafts react to what the page says, not just its title
  4. write_drafts()         LLM call #2 — one draft per topic (+ optional thread)
  5. lint_drafts()          deterministic checks: length, AI-tell words, em-dashes,
                            hashtags, never-mention names, and any NUMBER that is
                            not in the facts block or the source material
                            (the anti-fabrication check). One repair pass.
  6. render_messages()      plain text: one header bubble, one bubble per draft

Grounding: the model only sees a fixed facts block (`config/x_drafts_facts.md`,
gitignored — it is personal) and a few X-register sections of the vault voice
guide. It runs with `--safe-mode --tools ""` so it cannot read the vault,
auto-memory or CLAUDE.md, which carry client details that must never reach a
public post.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import requests

from signal_brief import config
from signal_brief.filter import _parse_claude_response  # reuse defensive parser
from signal_brief.schema import Item
from signal_brief.sources import rss as rss_source
from signal_brief.sources.github_trending import fetch_github_trending
from signal_brief.sources.hn import fetch_hn_front_page

log = logging.getLogger(__name__)

HISTORY_FILE = config.CACHE_DIR / "x_drafts_history.json"
FACTS_FILE = Path(os.environ.get(
    "X_DRAFTS_FACTS_FILE", str(config.CONFIG_DIR / "x_drafts_facts.md")))
VOICE_FILE_REL = "Resources/Voice/Voice Reference.md"

SELECT_MODEL = os.environ.get("X_DRAFTS_SELECT_MODEL", "sonnet")
WRITE_MODEL = os.environ.get("X_DRAFTS_WRITE_MODEL", "opus")
LLM_TIMEOUT = 600.0

DEDUPE_DAYS = 7
N_TOPICS = 3
TWEET_MAX = 280
THREAD_MAX_TWEETS = 4
RSS_FRESH = timedelta(hours=48)

# RSS feeds from feeds.yaml that count as "the AI/dev scene". HN has its own
# API source here (it carries points/comments), arXiv is too raw for a post,
# and bubble-breaker feeds are deliberately off-topic.
RSS_DOMAINS = {"ai-tech", "platform-engineering"}
RSS_EXCLUDE_PREFIXES = ("hn-", "arxiv-")

# Voice guide sections that describe the X register. Pulled by heading, never
# the whole file: the rest of it quotes client correspondence.
VOICE_HEADINGS = (
    "## DON'T patterns",
    "### Staccato is NOT the fix",
    "### Verified sample — [[PACL]] launch post",
    "### Markers",
    "### Anti-patterns for this register specifically",
    "### Dylan's own edit of a Claude X draft",
    "### X take / advice posts",
)

BANNED_PHRASES = (
    "delve", "leverage", "comprehensive", "game-changer", "game changer",
    "here's the thing", "heres the thing", "robust", "cutting-edge",
    "cutting edge", "synergy", "going forward", "utilize", "unlock",
    "supercharge", "seamless", "in today's", "let that sink in",
    "it's worth noting", "furthermore", "moreover", "revolutionize",
    "paradigm shift", "the future of", "buckle up", "hot take",
    "unpopular opinion",
)
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
_HASHTAG = re.compile(r"(?<![\w&])#[A-Za-z]\w*")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ─── candidates ──────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    cid: str
    item: Item

    @property
    def keys(self) -> set[str]:
        """Every identity this story can be recognised by in the history."""
        keys = {_norm_url(self.item.url)}
        if self.item.meta.get("hn_url"):
            keys.add(_norm_url(self.item.meta["hn_url"]))
        if self.item.meta.get("repo"):
            keys.add("gh:" + self.item.meta["repo"].lower())
        return keys

    def signal(self) -> str:
        """Human-readable 'why it's trending' metric, straight from the source."""
        m = self.item.meta
        if self.item.source_kind == "hn":
            return f"HN front page, {m.get('points', 0)} points, {m.get('comments', 0)} comments"
        if self.item.source_kind == "github":
            return f"GitHub trending, +{m.get('stars_today', 0):,} stars today, {m.get('stars', 0):,} total"
        return f"new on {m.get('feed_name', self.item.source)}"

    def for_prompt(self) -> dict:
        return {
            "id": self.cid,
            "title": self.item.title,
            "source": self.item.source,
            "signal": self.signal(),
            "excerpt": self.item.excerpt[:300],
        }


def _norm_url(url: str) -> str:
    url = url.strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.rstrip("/")


def _fresh_rss() -> list[Item]:
    """Reuse signal-brief's RSS adapter WITHOUT its seen-urls cache: the morning
    brief has already marked today's items seen by 08:30, and this job must not
    mark anything seen on the brief's behalf either."""
    feeds = [
        f for f in rss_source._load_feeds()
        if f.get("domain") in RSS_DOMAINS
        and not f.get("bubble_breaker")
        and not str(f.get("id", "")).startswith(RSS_EXCLUDE_PREFIXES)
    ]
    cutoff = datetime.now(timezone.utc) - RSS_FRESH
    out: list[Item] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for items in pool.map(rss_source._parse_one_feed, feeds):
            out.extend(i for i in items if i.published_at and i.published_at >= cutoff)
    return out


def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def recent_history(history: list[dict], *, today: date, days: int = DEDUPE_DAYS) -> list[dict]:
    cutoff = (today - timedelta(days=days)).isoformat()
    return [h for h in history if h.get("date", "") > cutoff]


def save_history(history: list[dict], new: list[dict], *, today: date) -> None:
    # Keep 30 days: DEDUPE_DAYS is what gates reuse, the rest is a short audit.
    keep = recent_history(history, today=today, days=30) + new
    HISTORY_FILE.write_text(json.dumps(keep, indent=2, ensure_ascii=False))


def collect_candidates(*, today: date, history: list[dict]) -> list[Candidate]:
    sources = [
        ("hn", fetch_hn_front_page),
        ("github", fetch_github_trending),
        ("rss", _fresh_rss),
    ]
    items: list[Item] = []
    for name, fn in sources:
        try:
            got = fn()
            log.info("x-drafts source %s: %d", name, len(got))
            items.extend(got)
        except Exception as e:  # noqa: BLE001 — one dead source is not a dead run
            log.exception("x-drafts source %s crashed: %s", name, e)

    used = {k for h in recent_history(history, today=today) for k in h.get("keys", [])}
    cands: list[Candidate] = []
    seen: set[str] = set()
    for item in items:
        c = Candidate(cid=f"c{len(cands) + 1}", item=item)
        if c.keys & used:
            log.info("x-drafts: skip (used within %dd): %s", DEDUPE_DAYS, item.title[:80])
            continue
        if c.keys & seen:
            continue
        seen |= c.keys
        cands.append(c)
    return cands


# ─── grounding material ──────────────────────────────────────────────────────

def load_facts() -> str:
    if not FACTS_FILE.exists():
        raise SystemExit(
            f"x-drafts facts file missing: {FACTS_FILE}. Copy "
            "config/x_drafts_facts.md.example and fill it with verified facts only."
        )
    return FACTS_FILE.read_text()


def never_mention(facts: str) -> list[str]:
    """Names listed under a `## never mention` heading in the facts file."""
    m = re.search(r"^## never mention\s*\n(.*?)(?=^## |\Z)", facts, re.M | re.S | re.I)
    if not m:
        return []
    return [ln.strip("-* \t") for ln in m.group(1).splitlines() if ln.strip("-* \t")]


def claimable_facts(facts: str) -> str:
    """The facts block minus the never-mention list (the model gets a generic
    rule instead, so it is not handed the very names it must avoid)."""
    return re.sub(r"^## never mention\s*\n.*?(?=^## |\Z)", "", facts, flags=re.M | re.S | re.I).strip()


def extract_sections(md: str, headings: tuple[str, ...] = VOICE_HEADINGS) -> str:
    """Return each heading's block (up to the next heading of the same or higher
    level), wikilinks flattened. Missing headings are skipped with a warning."""
    lines = md.splitlines()
    out: list[str] = []
    for h in headings:
        level = len(h) - len(h.lstrip("#"))
        start = next((i for i, ln in enumerate(lines) if ln.startswith(h)), None)
        if start is None:
            log.warning("voice guide: heading not found: %s", h)
            continue
        block = [lines[start]]
        for ln in lines[start + 1:]:
            m = re.match(r"^(#+)\s", ln)
            if m and len(m.group(1)) <= level:
                break
            block.append(ln)
        out.append("\n".join(block).strip())
    text = "\n\n".join(out)
    return re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)


def load_voice() -> str:
    path = Path(os.environ.get("X_DRAFTS_VOICE_FILE", "")) if os.environ.get(
        "X_DRAFTS_VOICE_FILE") else (config.VAULT_ROOT / VOICE_FILE_REL if config.VAULT_ROOT else None)
    if not path or not path.exists():
        log.warning("voice guide not found (%s) — using built-in rules only", path)
        return ""
    return extract_sections(path.read_text())


# ─── LLM ─────────────────────────────────────────────────────────────────────

def _claude(system: str, user: str, *, model: str) -> dict:
    """One isolated `claude -p` call: no tools, no CLAUDE.md / auto-memory
    (--safe-mode), so the only context is what we pass in. Raises on failure."""
    cmd = [
        config.CLAUDE_BIN,
        "--model", model,
        "--safe-mode",
        "--tools", "",
        "--system-prompt", system,
        "--print", user,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=LLM_TIMEOUT, check=False, cwd=str(config.PROJECT_ROOT))
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
    return _parse_claude_response(proc.stdout)


SELECT_SYSTEM = """You pick topics for a software engineer's daily X (Twitter) posts.

The engineer: AI/full-stack developer and CS student in Hong Kong. The facts
block in the user message is everything that is true about his work, but posts
do NOT have to be about his work. Most of them should be commentary on the day.

Pick the {n} candidates that (a) the AI-engineering / builder crowd on X is most
likely talking about TODAY, judging by the signal (HN points and comments, GitHub
stars today, first-party lab announcements), and (b) a sharp builder would have
a real, non-generic opinion about. Prefer variety: not three agent-framework
repos. Skip pure politics, celebrity and finance gossip.

Best picks are a current event that yields UNIVERSAL advice any builder can use.
Avoid:
- announcements whose only angle is restating the news ("model X is cheaper",
  "tool Y launched"). if the post would just repeat the headline, skip it.
- angles that only work if he has hands-on experience with a specific tool or
  technique that is not in FACTS (recommending a library he never shipped with
  reads as irrelevant without a project behind it).

For each pick, choose the post type that fits it best, and make the {n} types a
MIX (at least two different types; at most one "work"):
- "take": commentary on what this means or what people are getting wrong
- "advice": a concrete, practical lesson for builders prompted by the topic
- "provocative": a deliberately contrarian claim he would defend in replies
- "work": a tie-in to a FACT, only when the fit is genuine and not forced

Do not pick a candidate that is the same story as one in "recently used".

Output ONLY JSON:
{{"picks": [{{"id": "c7", "type": "take|advice|provocative|work", "why_trending": "<one short plain sentence on what the thing is and why builders care, from the title and excerpt only. do NOT restate points/stars/comment counts, they are shown separately>", "angle": "<one line: the specific point the post should make>"}}]}}
"""

WRITE_SYSTEM = """You ghostwrite X (Twitter) posts for one specific engineer. He
copies them into X himself, so every draft must be ready to post as-is.

# Post types

Each topic comes with a "type". Write that type:
- "take": his commentary on the topic, what it means, what people miss.
- "advice": a concrete, practical lesson for builders, prompted by the topic.
  specific enough to act on, not "communication is key".
- "provocative": a deliberately contrarian or spicy claim he would happily
  defend in the replies. it argues against an idea, a trend or a practice,
  never against a named person, and it is not rage-bait, not a dunk, not
  "X is dead". it must be a position a thoughtful builder could hold.
- "work": ties the topic to something in FACTS. only this type talks about
  what he built.
Takes, advice and provocative posts do not need to mention his work at all and
usually should not. Opinions stated as opinions are fine and encouraged.

# Hard rules (every type)

The FACTS block in the user message is the complete list of what is true about
his work.
- Never invent a first-person experience, anecdote, metric, client, employer,
  user, project, result or event. No "last week a client asked me", no
  "what broke first when i...", no "i've seen teams do X" unless FACTS says so.
  An opinion is fine, a fake anecdote is not.
- Any number presented as fact must come from FACTS (for claims about him) or
  from the topic material (for claims about the topic). Do not invent
  statistics to make a take land ("90% of startups..."). If you need a number
  you do not have, make the point without one.
- The only names from his work you may use are SourcingGPT, VETsage, BSD
  Education, HKU and PACL. Never name, hint at or describe any other client,
  employer, investor, colleague or private deal.
- Never describe what the linked page says beyond what is in the topic material
  given to you. React to it, do not summarise invented details.
- Facts are used as stated, not inflated ("84% of commits" stays 84%, "beta"
  stays beta).

# Voice

{voice}

Summary of the X register, which overrides anything above that conflicts:
- lowercase prose, acronyms keep capitals (AI, PR, API, RAG, CI). product names
  can be lowercase too.
- no em-dashes or en-dashes at all. use commas, "and", "since", "because".
- no hashtags, no emoji, no "🧵", no "thread:", no "1/", no numbered tweets.
- banned words: delve, leverage, comprehensive, game-changer, robust,
  cutting-edge, synergy, utilize, unlock, supercharge, seamless, "here's the
  thing", "let that sink in", "the future of", furthermore, moreover.
- take / advice / provocative posts use his take shape: line 1 names the
  current event plainly ("let's talk about nvidia's new proposal on agent
  safety."), line 2 says what they propose or claim, attributed to them ("they
  want..."), line 3 pivots with "but" to the real point, line 4 is an imperative
  fix aimed at builders ("fix X, not Y!"). one full plain sentence per line,
  blank line between lines. ONE idea only, no compound "X or Y, and A plus B".
  exclamation marks are welcome on the claim or the fix, not on every line.
- this is a SHAPE, not a template. across the day's posts: "let's talk about"
  opens at most ONE post; other openers name the event their own way
  ("nvidia wants a watchdog chip next to every agent.", "cloudflare just
  shipped vinext 1.0."). vary the line count (3 or 4) and do not start two
  posts with the same words. if the three read like the same template with
  the nouns swapped, rewrite them.
- work posts keep the narrative cadence: a short concrete opener, then one or
  two longer comma-chained sentences describing what happens.
- no fake-profound fragments and no aphoristic kicker line.
- blunt and concrete, never abstract-elevated. "ai slop" not "the erosion of
  signal". a real opinion, stated plainly, with a reason.
- the hook is the first line: specific, a little surprising, never clickbait
  ("you won't believe", "nobody is talking about", "this changes everything",
  "hot take:", "unpopular opinion:").
- no call to action, no "thoughts?", no "what do you think?".
- no link in the post text (he adds the link himself as a reply if he wants).

# Format

- each "post" MUST be 280 characters or fewer, including spaces and newlines.
  aim for 180-260. blank lines between paragraphs are fine.
- exactly {n} topics, in the order given, one post each, keeping each topic's
  type.
- at most ONE topic may also get a "thread": 2 to 4 tweets, each 280 chars or
  fewer, only if the topic really has more to say than fits one post. the
  thread's first tweet may differ from the single post. otherwise null.

Output ONLY JSON, nothing else:
{{"drafts": [{{"id": "c7", "type": "take", "post": "...", "thread": null, "facts_used": ["short quote of each FACT line used, empty unless type is work"]}}]}}
"""


def select_topics(cands: list[Candidate], *, facts: str, history: list[dict],
                  today: date, n: int = N_TOPICS) -> list[dict]:
    recent = [h.get("title", "") for h in recent_history(history, today=today)]
    user = (
        f"# FACTS\n{facts}\n\n# Recently used (last {DEDUPE_DAYS} days)\n"
        + json.dumps(recent, ensure_ascii=False)
        + f"\n\n# Candidates ({len(cands)})\n"
        + json.dumps([c.for_prompt() for c in cands], ensure_ascii=False)
        + f"\n\nPick exactly {n}. JSON only."
    )
    parsed = _claude(SELECT_SYSTEM.format(n=n), user, model=SELECT_MODEL)
    by_id = {c.cid: c for c in cands}
    picks = [p for p in parsed.get("picks", []) if p.get("id") in by_id]
    # De-dupe ids the model may have repeated, keep order.
    out, seen = [], set()
    for p in picks:
        if p["id"] not in seen:
            seen.add(p["id"])
            out.append(p)
    return out[:n]


def fetch_context(item: Item, *, timeout: float = 12.0, max_chars: int = 2500) -> str:
    """Readable text of the linked page (or the repo README), best effort.
    The draft model only sees this, so it cannot quote details it never read."""
    url = item.url
    if item.meta.get("repo"):
        url = f"https://raw.githubusercontent.com/{item.meta['repo']}/HEAD/README.md"
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) signal-brief/0.1"})
        r.raise_for_status()
        text = r.text
    except Exception as e:  # noqa: BLE001
        log.info("x-drafts: context fetch failed for %s: %s", url, e)
        return ""
    if "html" in r.headers.get("content-type", ""):
        text = re.sub(r"(?is)<(script|style|nav|header|footer|svg)[^>]*>.*?</\1>", " ", text)
        paras = re.findall(r"(?is)<(?:p|h1|h2|h3|li)[^>]*>(.*?)</(?:p|h1|h2|h3|li)>", text)
        text = "\n".join(re.sub(r"<[^>]+>", "", p) for p in paras)
    else:
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>", "", text)  # README images/html
    text = re.sub(r"[ \t]+", " ", unescape(text))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:max_chars]


def _topic_block(c: Candidate, pick: dict, context: str) -> dict:
    return {
        "id": c.cid,
        "title": c.item.title,
        "signal": c.signal(),
        "why_trending": pick.get("why_trending", ""),
        "type": pick.get("type", "take"),
        "suggested_angle": pick.get("angle", ""),
        "excerpt": c.item.excerpt[:500],
        "page_text": context,
    }


def write_drafts(topics: list[dict], *, facts: str, voice: str,
                 feedback: str = "") -> list[dict]:
    user = (
        f"# FACTS\n{facts}\n\n# Topics\n"
        + json.dumps(topics, ensure_ascii=False, indent=1)
    )
    if feedback:
        user += "\n\n# Fix these problems from your previous attempt\n" + feedback
    user += f"\n\nWrite exactly {len(topics)} drafts. JSON only."
    parsed = _claude(WRITE_SYSTEM.format(n=len(topics), voice=voice or "(none)"),
                     user, model=WRITE_MODEL)
    return parsed.get("drafts", [])


# ─── lint ────────────────────────────────────────────────────────────────────

def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text)}


def lint_text(text: str, *, allowed_numbers: set[str], forbidden: list[str]) -> list[str]:
    """Problems with one tweet. Empty list = clean."""
    problems: list[str] = []
    if len(text) > TWEET_MAX:
        problems.append(f"{len(text)} chars, over {TWEET_MAX}")
    low = text.lower()
    for p in BANNED_PHRASES:
        if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", low):
            problems.append(f'banned phrase "{p}"')
    if "—" in text or "–" in text:
        problems.append("contains an em/en-dash")
    if _HASHTAG.search(text):
        problems.append("contains a hashtag")
    if _EMOJI.search(text):
        problems.append("contains emoji")
    if re.search(r"https?://", text):
        problems.append("contains a link")
    for name in forbidden:
        if re.search(rf"(?<!\w){re.escape(name.lower())}(?!\w)", low):
            problems.append(f'mentions "{name}", which is private')
    stray = sorted(n for n in _numbers(text) - allowed_numbers if n)
    if stray:
        problems.append("numbers not in FACTS or topic material: " + ", ".join(stray))
    return problems


def lint_drafts(drafts: list[dict], topics: list[dict], *, facts: str,
                forbidden: list[str]) -> dict[str, list[str]]:
    """{draft id: [problems]} for every draft with at least one problem."""
    by_id = {t["id"]: t for t in topics}
    issues: dict[str, list[str]] = {}
    fact_nums = _numbers(facts)
    for d in drafts:
        t = by_id.get(d.get("id"), {})
        allowed = fact_nums | _numbers(" ".join(str(v) for v in t.values()))
        probs = [f"post: {p}" for p in lint_text(d.get("post", ""), allowed_numbers=allowed,
                                                 forbidden=forbidden)]
        thread = d.get("thread") or []
        if thread:
            if not (2 <= len(thread) <= THREAD_MAX_TWEETS):
                probs.append(f"thread has {len(thread)} tweets, must be 2-{THREAD_MAX_TWEETS}")
            for i, tw in enumerate(thread, 1):
                probs += [f"thread tweet {i}: {p}" for p in lint_text(
                    tw, allowed_numbers=allowed, forbidden=forbidden)]
        if not d.get("post"):
            probs.append("empty post")
        if probs:
            issues[d.get("id", "?")] = probs
    return issues


# Problems that make a draft unsendable even after the repair pass.
_HARD = ("private", "numbers not in FACTS", "over 280", "empty post", "must be 2-")


def is_hard(problem: str) -> bool:
    return any(h in problem for h in _HARD)


# ─── render ──────────────────────────────────────────────────────────────────

@dataclass
class Result:
    date: date
    entries: list[dict] = field(default_factory=list)  # {cand, pick, draft, issues}
    dropped: list[dict] = field(default_factory=list)


POST_TYPES = ("take", "advice", "provocative", "work")


def post_type(entry: dict) -> str:
    """The type the writer used, else the one selection asked for, else take."""
    for t in (entry.get("draft", {}).get("type"), entry.get("pick", {}).get("type")):
        if t in POST_TYPES:
            return t
    return "take"


def render_messages(res: Result) -> list[str]:
    """Plain text only — no Markdown — so a long-press copy in Telegram gives
    exactly the post text. Header first, then one bubble per draft, then the
    thread option (one bubble per tweet)."""
    day = res.date.strftime("%a %d %b").lower()
    head = [f"x drafts, {day}", "drafts follow in this order, one message each:", ""]
    thread_entry = None
    for i, e in enumerate(res.entries, 1):
        c: Candidate = e["cand"]
        head.append(f"{i}. [{post_type(e)}] {c.item.title}")
        head.append(f"why: {e['pick'].get('why_trending', '').strip()}")
        head.append(f"signal: {c.signal()}")
        head.append(c.item.url)
        if c.item.meta.get("hn_url") and c.item.meta["hn_url"] != c.item.url:
            head.append(f"discussion: {c.item.meta['hn_url']}")
        if e["issues"]:
            head.append("check before posting: " + "; ".join(e["issues"]))
        head.append("")
        if e["draft"].get("thread") and thread_entry is None:
            thread_entry = (i, e)
    if thread_entry:
        head.append(f"+ thread option for #{thread_entry[0]} "
                    f"({len(thread_entry[1]['draft']['thread'])} tweets) after the drafts")
    for d in res.dropped:
        head.append(f"dropped a draft on \"{d['cand'].item.title[:60]}\": "
                    + "; ".join(d["issues"])[:200])
    msgs = ["\n".join(head).strip()]
    msgs += [e["draft"]["post"].strip() for e in res.entries]
    if thread_entry:
        n, e = thread_entry
        tweets = e["draft"]["thread"]
        msgs.append(f"thread option for #{n}, {len(tweets)} tweets, one per message below")
        msgs += [t.strip() for t in tweets]
    return msgs


def history_entries(res: Result) -> list[dict]:
    return [{
        "date": res.date.isoformat(),
        "title": e["cand"].item.title,
        "url": e["cand"].item.url,
        "keys": sorted(e["cand"].keys),
        "type": post_type(e),
        "post": e["draft"].get("post", ""),
        "thread": e["draft"].get("thread"),
    } for e in res.entries]


# ─── pipeline ────────────────────────────────────────────────────────────────

def run(*, today: date) -> Result:
    facts_raw = load_facts()
    facts = claimable_facts(facts_raw)
    forbidden = never_mention(facts_raw)
    voice = load_voice()
    history = load_history()

    cands = collect_candidates(today=today, history=history)
    log.info("x-drafts: %d candidates", len(cands))
    if not cands:
        raise RuntimeError("no candidates from any source")

    picks = select_topics(cands, facts=facts, history=history, today=today)
    if not picks:
        raise RuntimeError("topic selection returned no usable picks")
    by_id = {c.cid: c for c in cands}
    chosen = [(by_id[p["id"]], p) for p in picks]

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        contexts = list(pool.map(lambda cp: fetch_context(cp[0].item), chosen))
    topics = [_topic_block(c, p, ctx) for (c, p), ctx in zip(chosen, contexts)]

    drafts = write_drafts(topics, facts=facts, voice=voice)
    issues = lint_drafts(drafts, topics, facts=facts, forbidden=forbidden)
    missing = {t["id"] for t in topics} - {d.get("id") for d in drafts}
    if issues or missing:
        feedback = "\n".join(f"- {i}: {'; '.join(p)}" for i, p in issues.items())
        if missing:
            feedback += "\n- missing drafts for: " + ", ".join(sorted(missing))
        log.info("x-drafts: repair pass for:\n%s", feedback)
        drafts = write_drafts(topics, facts=facts, voice=voice, feedback=feedback
                              + "\nKeep every draft that had no problems exactly as it was.")
        issues = lint_drafts(drafts, topics, facts=facts, forbidden=forbidden)

    res = Result(date=today)
    dmap = {d.get("id"): d for d in drafts}
    for c, p in chosen:
        d = dmap.get(c.cid)
        probs = issues.get(c.cid, []) if d else ["no draft returned"]
        entry = {"cand": c, "pick": p, "draft": d or {}, "issues": probs}
        if not d or any(is_hard(x) for x in probs if not x.startswith("thread")):
            log.warning("x-drafts: dropping %s: %s", c.cid, probs)
            res.dropped.append(entry)
            continue
        # A thread with a hard problem is removed; the single post stays.
        if any(is_hard(x) for x in probs if x.startswith("thread")):
            log.warning("x-drafts: removing thread on %s: %s", c.cid, probs)
            d["thread"] = None
            entry["issues"] = [x for x in probs if not x.startswith("thread")]
        if entry["issues"]:
            log.info("x-drafts: soft issues on %s: %s", c.cid, entry["issues"])
        res.entries.append(entry)
    return res
