"""Daily thread reconciliation + ask-don't-assume pass.

The morning brief used to **regenerate** the Open Threads section from the last
snapshot, so it drifted from reality: a submission still shown as "due" days
after it went out, a newsletter "to-send" after it was sent, a deferred item
still listed as a "today" action. Spec:
`.claude-memory/feedback_daily_thread_reconciliation.md`.

This module instead **reconciles** the prior thread snapshot against what the operator
actually said/did — recent daily-note live-capture sections and recent vault git
commits — marking threads done/deferred/dropped, never re-surfacing a resolved
thread. Where a thread's status is genuinely ambiguous, it asks the operator a SHORT
question rather than guessing.

Design:
  - `gather_reconciliation_context()` — pulls recent daily-note live-capture text
    + recent vault git commits. Degrades cleanly (returns "" per source on error).
  - `reconcile_threads()` — runs the LLM pass (claude -p, same pattern as
    vault_agent), returns a `ReconcileResult` (threads + questions).
  - Brevity is enforced in *pure code*, not trusted to the LLM:
    `cap_questions()` hard-caps at MAX_QUESTIONS and drops blanks; the
    "Quick check-ins" bubble is omitted entirely when there are no questions.
  - State persists to `.data/cache/threads.json` so tomorrow reconciles against
    today's reconciled snapshot (not a stale one).

If the daily note and git are both unavailable, the LLM still gets the prior
snapshot and can carry threads forward — i.e. it degrades to roughly the old
behaviour rather than crashing the brief.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date as _date, datetime, timedelta
from pathlib import Path

from signal_brief.config import (
    CACHE_DIR,
    CLAUDE_BIN,
    DAILY_NOTES_DIR,
    SIGNAL_BRIEF_EFFORT,
    SIGNAL_BRIEF_MODEL,
    VAULT_ROOT,
)
from signal_brief.daily_note import daily_note_path
from signal_brief.filter import _parse_claude_response  # reuse defensive parser

log = logging.getLogger(__name__)

THREADS_STATE_PATH = CACHE_DIR / "threads.json"

# HARD constraint (see spec): at most ~3 questions/day, prefer 0 over filler.
MAX_QUESTIONS = 3

# How many recent daily notes to scan for live-capture evidence.
RECENT_DAYS = 3

# Live-capture section headers that carry "what actually happened" signal.
LIVE_CAPTURE_HEADERS = (
    "## Log",
    "## Journal",
    "## Ideas",
    "## Connections Noticed",
    "## End of Day",
    "## Morning",
    "## Learned",
)

RECONCILE_TIMEOUT = 600.0  # 10 min — same ballpark as the filter pass.

# Valid reconciled statuses. "open" / "in_progress" stay surfaced; the rest are
# terminal and must NEVER be re-surfaced.
ACTIVE_STATUSES = {"open", "in_progress"}
TERMINAL_STATUSES = {"done", "deferred", "dropped"}


@dataclass
class Thread:
    """One tracked open thread / action item."""

    id: str  # stable slug, e.g. "grant-submission"
    title: str  # short human label
    status: str = "open"  # open | in_progress | done | deferred | dropped
    detail: str = ""  # one-line current state / next action
    last_updated: str = ""  # YYYY-MM-DD

    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass
class ReconcileResult:
    """Output of a reconciliation pass."""

    threads: list[Thread] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    rationale: str = ""
    # True when the LLM pass ran; False when we fell back (no LLM / parse fail).
    llm_ran: bool = False

    def active_threads(self) -> list[Thread]:
        return [t for t in self.threads if t.is_active()]


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #

def load_threads(path: Path | None = None) -> list[Thread]:
    """Load the prior thread snapshot. Returns [] if absent or unreadable."""
    path = path or THREADS_STATE_PATH
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log.info("no usable prior thread state at %s (%s)", path, e)
        return []
    threads = []
    for r in raw.get("threads", []):
        try:
            threads.append(Thread(**{k: r[k] for k in r if k in Thread.__dataclass_fields__}))
        except TypeError:
            continue
    return threads


def save_threads(threads: list[Thread], *, date_str: str, path: Path | None = None) -> Path:
    """Persist the reconciled snapshot. Drops terminal threads from what carries
    forward so resolved threads can never be re-surfaced tomorrow.
    """
    path = path or THREADS_STATE_PATH
    carry = [t for t in threads if t.is_active()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"date": date_str, "threads": [asdict(t) for t in carry]},
        ensure_ascii=False,
        indent=2,
    ))
    log.info("saved %d active threads (dropped %d terminal) to %s",
             len(carry), len(threads) - len(carry), path)
    return path


# --------------------------------------------------------------------------- #
# Context gathering (degrades cleanly per source)
# --------------------------------------------------------------------------- #

def _extract_live_capture(note_text: str) -> str:
    """Pull the live-capture sections (## Log / ## Journal / ## Ideas / etc.)
    out of a daily note, skipping the auto-generated Morning Brief block.

    We deliberately start AFTER the morning-brief end so we don't feed yesterday's
    (possibly wrong) regenerated threads back in as if they were ground truth.
    """
    # Drop everything up to and including the morning-brief region. The brief
    # ends either at an explicit end-marker or at the first live-capture header.
    body = note_text
    for marker in ("<!-- signal-brief:end -->", "## Morning\n"):
        idx = body.find(marker)
        if idx >= 0:
            body = body[idx + len(marker):]
            break

    out: list[str] = []
    lines = body.splitlines()
    capture = False
    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith("## ")
        if is_header:
            capture = any(stripped.startswith(h) for h in LIVE_CAPTURE_HEADERS)
        if capture:
            out.append(line)
    text = "\n".join(out).strip()
    return text


def gather_daily_note_evidence(today: str, *, days: int = RECENT_DAYS) -> str:
    """Concatenate live-capture sections from today + the last `days` daily notes.

    Returns "" on any failure (missing dir, unreadable files) — caller treats
    empty evidence as "no ground truth, lean on the prior snapshot / ask".
    """
    if DAILY_NOTES_DIR is None:
        return ""
    try:
        base = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        base = _date.today()

    chunks: list[str] = []
    for delta in range(days):
        day = (base - timedelta(days=delta)).isoformat()
        path = daily_note_path(day)
        try:
            text = path.read_text()
        except (FileNotFoundError, OSError):
            continue
        captured = _extract_live_capture(text)
        if captured:
            chunks.append(f"### Daily note {day} (live-capture)\n{captured}")
    return "\n\n".join(chunks)


def gather_git_evidence(*, days: int = RECENT_DAYS, max_commits: int = 30) -> str:
    """Recent vault git commit subjects, if the vault is a cheap-to-read git repo.

    Returns "" on any failure (not a repo, git missing, timeout). Never raises.
    """
    if VAULT_ROOT is None:
        return ""
    try:
        proc = subprocess.run(
            ["git", "log", f"--since={days}.days.ago",
             f"-n{max_commits}", "--pretty=format:%ad %s", "--date=short"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
            cwd=str(VAULT_ROOT),
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.info("git evidence unavailable: %s", e)
        return ""
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    return proc.stdout.strip()


def gather_reconciliation_context(today: str) -> dict[str, str]:
    """Bundle all evidence sources. Each value is "" if that source is unavailable."""
    return {
        "daily_notes": gather_daily_note_evidence(today),
        "git": gather_git_evidence(),
    }


# --------------------------------------------------------------------------- #
# Brevity enforcement (pure, fully tested without an LLM)
# --------------------------------------------------------------------------- #

def cap_questions(questions: list[str], *, limit: int = MAX_QUESTIONS) -> list[str]:
    """Enforce the HARD brevity constraint:
      - strip whitespace, drop blanks and duplicates
      - collapse each question to a single line
      - keep only the first `limit` (highest-priority — LLM is told to order them)

    Returning [] is the correct outcome when nothing is genuinely uncertain;
    callers omit the bubble entirely in that case.
    """
    seen: set[str] = set()
    out: list[str] = []
    for q in questions:
        if not q:
            continue
        # collapse to one line.
        q = " ".join(str(q).split())
        if not q:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# LLM reconciliation pass
# --------------------------------------------------------------------------- #

RECONCILE_PROMPT_HEADER = """You are the daily THREAD-RECONCILIATION agent for an Obsidian-style knowledge vault.

You are NOT regenerating an open-threads list from scratch. You are RECONCILING a
prior snapshot of the user's open threads / action items against what the user
ACTUALLY said and did since — so the list reflects reality, not stale state.

The user (the operator) has been burned by threads that drift from reality: a thread shown
as "due" after he already submitted it, "to-send" after he already sent it, a
"today" action after he deferred it. That erodes his trust in the whole brief.

**Your job, in order:**

1. **Reconcile every prior thread** against the evidence below (recent daily-note
   live-capture sections + recent vault git commits):
   - If the evidence shows it was completed → status "done".
   - If he deferred / pushed it out → status "deferred".
   - If he dismissed / killed it → status "dropped".
   - If it's still live with a clear next step → "open" or "in_progress".
   - NEVER re-surface a thread the evidence shows is already resolved.

2. **Ask, don't assume.** For threads whose status is GENUINELY unknown or
   ambiguous from the evidence, do NOT guess and do NOT carry stale state
   forward. Instead write a SHORT question for the user.

**Brevity is a HARD constraint on questions (the user will lose trust if you nag):**
- At most 3 questions. Prefer FEWER. Prefer ZERO if nothing is genuinely uncertain.
- One line each. Main-point / highest-uncertainty only. No low-stakes confirmations.
- Phrase yes/no or one-word-answer where possible.
- Order questions most-important first (only the top 3 will be kept).

**Output format (STRICT — valid JSON only, nothing before/after, no markdown fences):**

{
  "threads": [
    {
      "id": "stable-slug",
      "title": "short label",
      "status": "open|in_progress|done|deferred|dropped",
      "detail": "one line: current state or next action",
      "last_updated": "YYYY-MM-DD"
    }
  ],
  "questions": ["short question 1", "short question 2"],
  "rationale": "1-2 sentences: what you reconciled and why. Audit-trail only."
}

Keep ALL prior threads in the `threads` array with their updated status (so the
audit trail shows what got resolved), but be honest about terminal statuses.
"""


def _build_reconcile_prompt(
    prior: list[Thread], context: dict[str, str], today: str
) -> str:
    prior_json = json.dumps([asdict(t) for t in prior], ensure_ascii=False, indent=2)
    daily = context.get("daily_notes") or "(no daily-note live-capture available)"
    git = context.get("git") or "(no git evidence available)"
    return f"""{RECONCILE_PROMPT_HEADER}

---

## Today
{today}

---

## Prior thread snapshot (reconcile THESE)

```json
{prior_json}
```

---

## Evidence A — recent daily-note live-capture (what he said/did)

{daily}

---

## Evidence B — recent vault git commits

{git}

---

Produce the reconciliation JSON now. Output ONLY the JSON object.
"""


def _parse_reconcile(parsed: dict, today: str) -> ReconcileResult:
    threads: list[Thread] = []
    for t in parsed.get("threads", []) or []:
        status = str(t.get("status", "open")).strip().lower()
        if status not in ACTIVE_STATUSES and status not in TERMINAL_STATUSES:
            status = "open"
        threads.append(Thread(
            id=str(t.get("id", "")).strip() or _slug(t.get("title", "")),
            title=str(t.get("title", "")).strip(),
            status=status,
            detail=str(t.get("detail", "")).strip(),
            last_updated=str(t.get("last_updated", "")).strip() or today,
        ))
    questions = cap_questions([str(q) for q in (parsed.get("questions") or [])])
    return ReconcileResult(
        threads=threads,
        questions=questions,
        rationale=str(parsed.get("rationale", "")).strip(),
        llm_ran=True,
    )


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return s or "thread"


def reconcile_threads(
    prior: list[Thread] | None = None,
    *,
    today: str | None = None,
    context: dict[str, str] | None = None,
) -> ReconcileResult:
    """Run the reconciliation pass. Degrades cleanly on every failure path.

    - prior: prior thread snapshot (loaded from state if None).
    - context: evidence bundle (gathered from vault if None).
    On LLM/parse failure, carries prior ACTIVE threads forward unchanged and asks
    nothing — i.e. falls back to roughly the old behaviour rather than crashing.
    """
    today = today or _date.today().isoformat()
    prior = load_threads() if prior is None else prior
    context = gather_reconciliation_context(today) if context is None else context

    # Nothing to reconcile and no evidence — nothing to do.
    if not prior:
        log.info("no prior threads to reconcile")
        return ReconcileResult(threads=[], questions=[], rationale="No prior threads.", llm_ran=False)

    prompt = _build_reconcile_prompt(prior, context, today)
    log.info("invoking reconcile agent (%s/%s) on %d prior threads",
             SIGNAL_BRIEF_MODEL, SIGNAL_BRIEF_EFFORT, len(prior))

    try:
        proc = subprocess.run(
            [
                CLAUDE_BIN,
                "-p", prompt,
                "--output-format", "text",
                "--permission-mode", "bypassPermissions",
                "--model", SIGNAL_BRIEF_MODEL,
                "--effort", SIGNAL_BRIEF_EFFORT,
            ],
            capture_output=True,
            text=True,
            timeout=RECONCILE_TIMEOUT,
            check=False,
            cwd=str(VAULT_ROOT) if VAULT_ROOT else None,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.error("reconcile subprocess failed: %s — carrying prior threads", e)
        return _fallback_reconcile(prior)

    if proc.returncode != 0:
        log.error("reconcile agent exited %d: %s — carrying prior threads",
                  proc.returncode, proc.stderr[-500:])
        return _fallback_reconcile(prior)

    try:
        parsed = _parse_claude_response(proc.stdout)
    except ValueError as e:
        log.error("could not parse reconcile output: %s — carrying prior threads", e)
        return _fallback_reconcile(prior)

    return _parse_reconcile(parsed, today)


def _fallback_reconcile(prior: list[Thread]) -> ReconcileResult:
    """LLM unavailable: carry active prior threads forward, ask nothing.

    Conservative on purpose — asking nothing is always safe; surfacing stale
    state is the failure mode, but without an LLM we can't tell what's stale, so
    we surface the prior active set unchanged and add NO questions.
    """
    return ReconcileResult(
        threads=[t for t in prior if t.is_active()],
        questions=[],
        rationale="⚠️ reconciliation LLM unavailable — prior active threads carried forward, no questions asked.",
        llm_ran=False,
    )
