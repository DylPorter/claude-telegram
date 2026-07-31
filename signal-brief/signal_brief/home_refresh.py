"""Weekly Home.md refresh + Done Log sweep (deterministic, surgical).

The weekly review LLM agent emits a structured `home_refresh` object alongside
its prose digest:

    {
      "this_week": [ {"text": "...", "status": "🔴|🟠|🟢|✍️|..."}, ... ],
      "sweep_to_done": [ {"date": "YYYY-MM-DD", "bullet": "..."}, ... ]
    }

This module turns that into two byte-conservative edits:

  * `replace_this_week_block` — rewrites ONLY the `## 🎯 This Week (...)` block in
    Home.md (header date refreshed to the run date, ranked list from the agent),
    leaving every other section (Family commitments, Funding, `Next / in the picture`, Clients,
    etc.) untouched byte-for-byte.
  * `prepend_done_log_entries` — prepends swept items to the Done Log under a
    `## YYYY-MM-DD` header (newest-first), idempotently (never duplicates a bullet
    that's already present, never writes an empty dated header).

Hard rules enforced here (from `feedback_home_dashboard_clean`):
  - Home stays active-only / forward-looking. Completed items NEVER remain in Home.
  - Only the `This Week` block may be rewritten; the `Next / in the picture`
    backlog and everything else is preserved.
  - Priorities/sweeps come from the ACTUAL review output — this module never
    fabricates tasks; it only formats and places what the agent supplied.
"""

from __future__ import annotations

import logging
import re
from datetime import date as _date
from pathlib import Path

log = logging.getLogger(__name__)

# Matches the "This Week" section header regardless of the emoji / exact suffix,
# e.g. "## 🎯 This Week (2026-06-18) — ranked". Anchored to a line start.
THIS_WEEK_HEADER_RE = re.compile(r"^##[^\n]*\bThis Week\b[^\n]*$", re.MULTILINE)

# The next section begins at the next line that starts with "## " (level-2
# heading). The "_✅ Done items live in ..._" italic line has no heading marker,
# so it stays inside the This Week block, as required.
NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)

# Default trailing line for the This Week block — preserved if the agent doesn't
# supply one, so the "Done items live in [[Done Log]]" pointer never gets dropped.
DEFAULT_DONE_POINTER = (
    "_✅ Done items live in [[Done Log]] — Home stays active-only._"
)

DONE_DATE_HEADER_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def render_this_week_block(
    items: list[dict], *, run_date: str, done_pointer: str = DEFAULT_DONE_POINTER
) -> str:
    """Render the full replacement text for the This Week block.

    `items` is the agent's ranked list: each {"text": str, "status": str?}.
    Returns the block text starting with the `## 🎯 This Week (...)` header and
    ending with the done-pointer italic line (no trailing blank line).
    """
    header = f"## 🎯 This Week ({run_date}) — ranked"
    lines = [header, ""]
    n = 0
    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        n += 1
        status = (item.get("status") or "").strip()
        prefix = f"{n}. {status} " if status else f"{n}. "
        lines.append(f"{prefix}{text}".rstrip())
    lines.append("")
    lines.append((done_pointer or DEFAULT_DONE_POINTER).strip())
    return "\n".join(lines)


def replace_this_week_block(home_md: str, new_block: str) -> str:
    """Replace ONLY the This Week block in `home_md` with `new_block`.

    Matches from the `## ... This Week ...` header up to (but not including) the
    next `## ` heading. Everything before the header and from the next heading
    onward is preserved byte-for-byte.

    Raises ValueError if the This Week header can't be found (fail loud rather
    than silently corrupt — the caller should skip the refresh on failure).
    """
    m = THIS_WEEK_HEADER_RE.search(home_md)
    if not m:
        raise ValueError("Could not locate the '## ... This Week ...' header in Home.md")

    block_start = m.start()
    # Find the next level-2 heading AFTER the This Week header line.
    nxt = NEXT_HEADING_RE.search(home_md, m.end())
    block_end = nxt.start() if nxt else len(home_md)

    before = home_md[:block_start]
    after = home_md[block_end:]

    # Normalise so exactly one blank line separates the new block from `after`
    # (when `after` exists). `new_block` carries no trailing newline.
    if after:
        return f"{before}{new_block}\n\n{after.lstrip(chr(10))}"
    # No following section (This Week was the last block): keep a trailing newline.
    return f"{before}{new_block}\n"


def _normalise_bullet(text: str) -> str:
    """Normalise a Done Log bullet to a canonical `- ✅ ...` form for comparison.

    Strips list markers and a leading ✅ so an agent-supplied bullet that already
    starts with "✅" or "- " isn't double-prefixed and dedupes against existing
    entries regardless of cosmetic differences.
    """
    t = text.strip()
    t = re.sub(r"^[-*]\s+", "", t)          # drop list marker
    t = re.sub(r"^✅\s*", "", t)            # drop a leading check
    return t.strip()


def _existing_done_bullets(done_md: str) -> set[str]:
    """Return the set of normalised bullet bodies already in the Done Log."""
    out: set[str] = set()
    for line in done_md.splitlines():
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            out.add(_normalise_bullet(s))
    return out


def _frontmatter_split(md: str) -> tuple[str, str]:
    """Split leading YAML frontmatter (if any) from the body.

    Returns (frontmatter_including_trailing_newlines, body). Preserves the
    frontmatter byte-for-byte.
    """
    if md.startswith("---\n"):
        end = md.find("\n---", 4)
        if end != -1:
            # include the closing '---' line and any blank lines after it
            close = md.find("\n", end + 1)
            close = len(md) if close == -1 else close + 1
            return md[:close], md[close:]
    return "", md


def prepend_done_log_entries(
    done_md: str, entries: list[dict], *, run_date: str
) -> str:
    """Prepend swept items to the Done Log under newest-first dated headers.

    `entries` is the agent's list of {"date": "YYYY-MM-DD"?, "bullet": str}.
    Items default to `run_date` when they carry no explicit date. Idempotent:
      - bullets already present anywhere in the log are skipped,
      - if everything is already present (or `entries` is empty), the log is
        returned unchanged (no empty dated header is written),
      - if a dated header already exists, new bullets are inserted under it
        rather than creating a duplicate header.

    The `## <date>` headers are kept newest-first. Frontmatter + the existing
    body are preserved.
    """
    if not entries:
        return done_md

    existing = _existing_done_bullets(done_md)

    # Group genuinely-new bullets by date.
    by_date: dict[str, list[str]] = {}
    for e in entries:
        bullet = (e.get("bullet") or "").strip()
        if not bullet:
            continue
        body = _normalise_bullet(bullet)
        if not body or body in existing:
            continue
        existing.add(body)  # dedupe within this batch too
        d = (e.get("date") or "").strip() or run_date
        by_date.setdefault(d, []).append(f"- ✅ {body}")

    if not by_date:
        return done_md  # nothing new — don't touch the file

    frontmatter, body = _frontmatter_split(done_md)

    # Find where the first existing dated header starts; we insert above it so
    # new dates land newest-first. Everything before that (title, blurb) is kept.
    first_header = DONE_DATE_HEADER_RE.search(body)
    head = body[: first_header.start()] if first_header else body
    tail = body[first_header.start():] if first_header else ""

    # Build the new dated blocks, newest date first.
    existing_headers = {m.group(1) for m in DONE_DATE_HEADER_RE.finditer(tail)}
    new_blocks: list[str] = []
    for d in sorted(by_date, reverse=True):
        bullets = "\n".join(by_date[d])
        if d in existing_headers:
            # Merge into the existing header for that date instead of duplicating.
            tail = _insert_under_header(tail, d, bullets)
        else:
            new_blocks.append(f"## {d}\n{bullets}\n")

    head_norm = head.rstrip("\n")
    parts = [
        p
        for p in (
            head_norm,
            "\n".join(new_blocks).rstrip("\n"),
            tail.strip("\n"),
        )
        if p
    ]
    rebuilt_body = "\n\n".join(parts) + "\n"
    return f"{frontmatter}{rebuilt_body}"


def _insert_under_header(tail: str, target_date: str, bullets: str) -> str:
    """Insert `bullets` immediately under the `## <target_date>` header in `tail`."""
    pattern = re.compile(rf"^## {re.escape(target_date)}\s*$", re.MULTILINE)
    m = pattern.search(tail)
    if not m:
        return tail
    insert_at = m.end()
    # Skip past the newline after the header.
    nl = tail.find("\n", insert_at)
    nl = insert_at if nl == -1 else nl + 1
    return tail[:nl] + bullets + "\n" + tail[nl:]


def apply_home_refresh(
    home_refresh: dict,
    *,
    run_date: str | None = None,
    home_path: Path | None = None,
    done_log_path: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Apply a `home_refresh` payload to Home.md + the Done Log.

    Returns a summary dict: {"home_updated": bool, "done_swept": int, "diff": str?}.
    On `dry_run`, computes the new content and a unified diff but writes nothing.

    `home_refresh` shape:
        {"this_week": [{"text", "status"}], "sweep_to_done": [{"date", "bullet"}]}
    """
    from signal_brief.config import DONE_LOG_NOTE, HOME_NOTE  # local: avoid import cycle

    run_date = run_date or _date.today().isoformat()
    home_path = home_path or HOME_NOTE
    done_log_path = done_log_path or DONE_LOG_NOTE

    summary: dict = {"home_updated": False, "done_swept": 0, "errors": []}
    if not isinstance(home_refresh, dict):
        summary["errors"].append("home_refresh payload is not an object")
        return summary

    this_week = home_refresh.get("this_week") or []
    sweep = home_refresh.get("sweep_to_done") or []

    # --- Home: rewrite the This Week block (only if the agent gave items) -----
    if this_week and home_path and Path(home_path).exists():
        original = Path(home_path).read_text()
        block = render_this_week_block(this_week, run_date=run_date)
        try:
            new_home = replace_this_week_block(original, block)
        except ValueError as e:
            summary["errors"].append(f"home: {e}")
            new_home = original
        if new_home != original:
            summary["home_updated"] = True
            if dry_run:
                summary["home_diff"] = _unified_diff(original, new_home, str(home_path))
            else:
                Path(home_path).write_text(new_home)
                log.info("refreshed This Week block in %s", home_path)
    elif this_week and (not home_path or not Path(home_path).exists()):
        summary["errors"].append(f"home note not found: {home_path}")

    # --- Done Log: prepend swept items (idempotent) ---------------------------
    if sweep and done_log_path and Path(done_log_path).exists():
        original = Path(done_log_path).read_text()
        new_done = prepend_done_log_entries(original, sweep, run_date=run_date)
        if new_done != original:
            # count how many new bullets landed
            summary["done_swept"] = len(_existing_done_bullets(new_done)) - len(
                _existing_done_bullets(original)
            )
            if dry_run:
                summary["done_diff"] = _unified_diff(
                    original, new_done, str(done_log_path)
                )
            else:
                Path(done_log_path).write_text(new_done)
                log.info("swept %d item(s) into %s", summary["done_swept"], done_log_path)
    elif sweep and (not done_log_path or not Path(done_log_path).exists()):
        summary["errors"].append(f"done log not found: {done_log_path}")

    return summary


def _unified_diff(a: str, b: str, path: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
