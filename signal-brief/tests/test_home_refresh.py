"""Tests for the weekly Home.md refresh + Done Log sweep (pure logic, no LLM).

The load-bearing guarantee: the This Week block replacement is SURGICAL — every
other section of Home (Family commitments, Funding, `Next / in the picture`, Clients, etc.) is
preserved byte-for-byte. We prove that against the REAL Home.md (read-only copy;
the test never writes to the vault), plus idempotency / no-empty-header rules for
the Done Log sweep.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signal_brief.home_refresh import (
    DEFAULT_DONE_POINTER,
    apply_home_refresh,
    prepend_done_log_entries,
    render_this_week_block,
    replace_this_week_block,
)

# A self-contained Home fixture mirroring the real vault's structure.
SAMPLE_HOME = """---
type: dashboard
tags:
  - dashboard
updated: 2026-06-03
---

# Home

> Human-facing dashboard.

## 🎯 This Week (2026-06-01) — ranked
1. 🔴 **Old urgent thing.** Body with [[A Link]].
2. 🟠 Second item.

_✅ Done items live in [[Done Log]] — Home stays active-only._

## 🚨 Family commitments's uni admissions *(CANNOT forget)*
> The Family commitments block — must be preserved byte-for-byte.
- A bullet with [[Family commitments]].

## 🔭 Next / in the picture
> The backlog — must NOT be deleted.
- **[[Portfolio]]** — on deck.
- Another backlog item.

## 💰 Funding & runway
- Money stuff.
"""

SAMPLE_DONE = """---
type: log
tags:
  - done
created: 2026-06-03
---

# Done Log

> Completed items swept out of [[Home]]. Newest first.

## 2026-06-14
- ✅ Already-logged item with [[X]].

## 2026-06-12
- ✅ Older done item.
"""


# --------------------------------------------------------------------------- #
# render_this_week_block
# --------------------------------------------------------------------------- #

def test_render_block_numbers_and_status():
    block = render_this_week_block(
        [
            {"text": "Ship [[PACL]] video.", "status": "🟠"},
            {"text": "No-status item."},
        ],
        run_date="2026-06-20",
    )
    assert block.startswith("## 🎯 This Week (2026-06-20) — ranked")
    assert "1. 🟠 Ship [[PACL]] video." in block
    assert "2. No-status item." in block
    assert block.rstrip().endswith(DEFAULT_DONE_POINTER)


def test_render_block_skips_empty_text():
    block = render_this_week_block(
        [{"text": "", "status": "🔴"}, {"text": "real", "status": "🟢"}],
        run_date="2026-06-20",
    )
    # the empty one is dropped; numbering restarts so the real one is #1
    assert "1. 🟢 real" in block
    assert "🔴" not in block


# --------------------------------------------------------------------------- #
# replace_this_week_block — THE surgical-preservation guarantee
# --------------------------------------------------------------------------- #

def test_replace_preserves_everything_outside_this_week():
    new_block = render_this_week_block(
        [{"text": "Fresh #1 [[Thing]].", "status": "✍️"}],
        run_date="2026-06-20",
    )
    out = replace_this_week_block(SAMPLE_HOME, new_block)

    # New block landed, with refreshed date.
    assert "## 🎯 This Week (2026-06-20) — ranked" in out
    assert "Fresh #1 [[Thing]]." in out
    # Old This Week content is gone.
    assert "Old urgent thing" not in out
    assert "Second item" not in out

    # Every other section preserved byte-for-byte.
    for sentinel in (
        "## 🚨 Family commitments's uni admissions *(CANNOT forget)*",
        "> The Family commitments block — must be preserved byte-for-byte.",
        "- A bullet with [[Family commitments]].",
        "## 🔭 Next / in the picture",
        "> The backlog — must NOT be deleted.",
        "- **[[Portfolio]]** — on deck.",
        "- Another backlog item.",
        "## 💰 Funding & runway",
        "- Money stuff.",
    ):
        assert sentinel in out, f"lost section content: {sentinel!r}"

    # Frontmatter untouched.
    assert out.startswith("---\ntype: dashboard\n")
    # The Next backlog is intact (explicit anti-deletion guard).
    assert out.count("## 🔭 Next / in the picture") == 1


def test_replace_against_real_home_preserves_rest_byte_for_byte():
    """Run the replacement against the LIVE Home.md and prove only the This Week
    block changed — everything from the next heading onward is identical."""
    # Resolved from config (DEFAULT_CWD / SIGNAL_BRIEF_VAULT_ROOT), not a literal
    # path: this asserts against whatever vault the checkout is configured for,
    # and skips cleanly on a machine that has none.
    from signal_brief.config import HOME_NOTE

    if HOME_NOTE is None:
        pytest.skip("no vault configured — set SIGNAL_BRIEF_VAULT_ROOT or DEFAULT_CWD")
    real_home = Path(HOME_NOTE)
    if not real_home.exists():
        pytest.skip(f"configured Home note not present: {real_home}")
    original = real_home.read_text()

    new_block = render_this_week_block(
        [{"text": "Synthetic test item — should never be written.", "status": "🔴"}],
        run_date="2099-01-01",
    )
    out = replace_this_week_block(original, new_block)

    # Locate the boundary heading in BOTH strings and assert the suffix is equal.
    import re
    nxt = re.compile(r"^## ", re.MULTILINE)
    # next heading AFTER the This Week header
    tw = re.search(r"^##[^\n]*This Week[^\n]*$", original, re.MULTILINE)
    after_orig = nxt.search(original, tw.end())
    after_out = nxt.search(out, out.index(new_block) + len(new_block))
    assert original[after_orig.start():] == out[after_out.start():], (
        "content from the next section onward must be byte-for-byte identical"
    )
    # And the prefix (frontmatter + intro before This Week) is identical too.
    assert original[: tw.start()] == out[: out.index(new_block)]


def test_replace_raises_when_no_this_week_header():
    with pytest.raises(ValueError):
        replace_this_week_block("# Home\n\n## Other\nstuff\n", "## 🎯 This Week (x)\n")


def test_replace_handles_this_week_as_last_section():
    home = "# Home\n\n## 🎯 This Week (2026-06-01) — ranked\n1. old\n"
    block = render_this_week_block([{"text": "new"}], run_date="2026-06-20")
    out = replace_this_week_block(home, block)
    assert "old" not in out
    assert "1. new" in out
    assert out.startswith("# Home\n")


# --------------------------------------------------------------------------- #
# prepend_done_log_entries
# --------------------------------------------------------------------------- #

def test_done_prepend_newest_first_under_new_header():
    out = prepend_done_log_entries(
        SAMPLE_DONE,
        [{"date": "2026-06-20", "bullet": "Shipped the [[Feature]]."}],
        run_date="2026-06-20",
    )
    assert "## 2026-06-20\n- ✅ Shipped the [[Feature]]." in out
    # newest first: 06-20 header appears before the pre-existing 06-14 one
    assert out.index("## 2026-06-20") < out.index("## 2026-06-14")
    # frontmatter + title preserved
    assert out.startswith("---\ntype: log\n")
    assert "# Done Log" in out


def test_done_prepend_is_idempotent_on_duplicate_bullet():
    # The bullet already exists in the log → no change.
    out = prepend_done_log_entries(
        SAMPLE_DONE,
        [{"date": "2026-06-20", "bullet": "✅ Already-logged item with [[X]]."}],
        run_date="2026-06-20",
    )
    assert out == SAMPLE_DONE


def test_done_prepend_no_empty_header_when_nothing_new():
    out = prepend_done_log_entries(SAMPLE_DONE, [], run_date="2026-06-20")
    assert out == SAMPLE_DONE
    # all-duplicate batch also produces no new dated header
    out2 = prepend_done_log_entries(
        SAMPLE_DONE,
        [{"bullet": "Already-logged item with [[X]]."}],
        run_date="2026-06-20",
    )
    assert "## 2026-06-20" not in out2


def test_done_prepend_merges_into_existing_date_header():
    out = prepend_done_log_entries(
        SAMPLE_DONE,
        [{"date": "2026-06-14", "bullet": "A second thing that day."}],
        run_date="2026-06-20",
    )
    # no duplicate header for 06-14
    assert out.count("## 2026-06-14") == 1
    assert "- ✅ A second thing that day." in out
    assert "- ✅ Already-logged item with [[X]]." in out


def test_done_prepend_dedupes_within_batch():
    out = prepend_done_log_entries(
        SAMPLE_DONE,
        [
            {"date": "2026-06-20", "bullet": "Same thing."},
            {"date": "2026-06-20", "bullet": "✅ Same thing."},
        ],
        run_date="2026-06-20",
    )
    assert out.count("- ✅ Same thing.") == 1


# --------------------------------------------------------------------------- #
# apply_home_refresh — orchestration (dry-run + write to temp copies)
# --------------------------------------------------------------------------- #

def test_apply_dry_run_writes_nothing_and_returns_diffs(tmp_path):
    home = tmp_path / "Home.md"
    done = tmp_path / "Done Log.md"
    home.write_text(SAMPLE_HOME)
    done.write_text(SAMPLE_DONE)

    payload = {
        "this_week": [{"text": "New ranked item [[Z]].", "status": "🟠"}],
        "sweep_to_done": [{"date": "2026-06-20", "bullet": "Did the thing."}],
    }
    summary = apply_home_refresh(
        payload, run_date="2026-06-20",
        home_path=home, done_log_path=done, dry_run=True,
    )
    assert summary["home_updated"] is True
    assert summary["done_swept"] == 1
    assert "home_diff" in summary and "done_diff" in summary
    # nothing written
    assert home.read_text() == SAMPLE_HOME
    assert done.read_text() == SAMPLE_DONE


def test_apply_live_writes_both_files(tmp_path):
    home = tmp_path / "Home.md"
    done = tmp_path / "Done Log.md"
    home.write_text(SAMPLE_HOME)
    done.write_text(SAMPLE_DONE)

    payload = {
        "this_week": [{"text": "New ranked item [[Z]].", "status": "🟠"}],
        "sweep_to_done": [{"date": "2026-06-20", "bullet": "Did the thing."}],
    }
    summary = apply_home_refresh(
        payload, run_date="2026-06-20",
        home_path=home, done_log_path=done, dry_run=False,
    )
    assert summary["home_updated"] is True
    assert summary["done_swept"] == 1

    h = home.read_text()
    assert "## 🎯 This Week (2026-06-20) — ranked" in h
    assert "New ranked item [[Z]]." in h
    assert "Old urgent thing" not in h
    # Next backlog preserved
    assert "## 🔭 Next / in the picture" in h
    assert "- **[[Portfolio]]** — on deck." in h

    d = done.read_text()
    assert "## 2026-06-20\n- ✅ Did the thing." in d


def test_apply_empty_payload_is_noop(tmp_path):
    home = tmp_path / "Home.md"
    done = tmp_path / "Done Log.md"
    home.write_text(SAMPLE_HOME)
    done.write_text(SAMPLE_DONE)
    summary = apply_home_refresh(
        {"this_week": [], "sweep_to_done": []},
        run_date="2026-06-20", home_path=home, done_log_path=done, dry_run=False,
    )
    assert summary["home_updated"] is False
    assert summary["done_swept"] == 0
    assert home.read_text() == SAMPLE_HOME
    assert done.read_text() == SAMPLE_DONE
