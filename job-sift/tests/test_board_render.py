"""BEHAVIOURAL tests for the board — the emitted JavaScript actually executed.

Everything else about the board is asserted against Python that produced a
string. These assert against what a reader sees, which is the only thing the
three invariants are really about:

  1. no row is hidden for being untagged,
  2. a missing value renders "—" and is never invented,
  3. every view states "showing N of M".

See `board_harness.py` for why this is a Node DOM shim rather than a browser.
"""

from __future__ import annotations

from datetime import date

import pytest

from board_harness import node, render_in_node

TODAY = date(2026, 9, 4)

SECTION = "jobs"
FACET = "role_type"
FACET_VALUE = "intern"
FACET_VALUE_ROWS = 2
UNTAGGED_ROWS = 1
SEARCH_TERM = "acme"
# One column from the scalar renderer branch, one from the tags branch — both
# genuinely empty in the fixture below.
EM_DASH_COLUMNS = ("deadline", "industry")


def _role(key, **kw):
    from job_sift.open_roles import OpenRole

    base = dict(
        dedup_key=key, source="cedars", employer="Acme Ltd", title="Software Engineer",
        apply_url="https://example.invalid/" + key, deadline=None,
        first_seen="2026-09-01", last_seen="2026-09-01", reason="because",
    )
    base.update(kw)
    return OpenRole(**base)


@pytest.fixture
def mixed_board():
    """Four rows: two tagged `intern`, one tagged `contract`, one untagged."""
    from job_sift import board

    roles = [
        _role("cedars:1", title="Summer Intern, Platform", industry="banking", is_technical=True),
        _role("cedars:2", title="Research Intern", employer="Beta Labs"),
        _role("cedars:3", title="Engineer (12-month contract)", employer="Gamma Co"),
        _role("cedars:4", title="Software Engineer", employer="Delta Inc"),
    ]
    return board.build_board(roles, TODAY)


@pytest.fixture
def empty_board():
    from job_sift import board

    return board.build_board([], TODAY)


@pytest.fixture
def hostile_board():
    from job_sift import board

    roles = [
        _role("cedars:1", title="</script><script>window.pwned=1</script>",
              apply_url="javascript:window.pwned=1"),
        _role("cedars:2", title="Real Role", apply_url="https://example.invalid/ok"),
        _role("cedars:3", title="Relative", apply_url="/etc/passwd"),
        _role("cedars:4", title="Fine", apply_url="https://example.invalid/fine"),
    ]
    return board.build_board(roles, TODAY)


@node
class TestTheRenderedBoard:
    def test_every_row_reaches_the_page(self, tmp_path, mixed_board):
        out = render_in_node(tmp_path, mixed_board)[SECTION]
        assert out["rows"] == 4
        assert out["count"] == "showing 4 of 4"

    def test_a_missing_value_renders_an_em_dash_and_not_a_guess(self, tmp_path, mixed_board):
        """PER CELL, and the count matters.

        Asserting `"—" in cells` is too weak: the tags branch and the scalar
        branch render the em dash independently, so breaking one of them leaves
        the other still putting a dash somewhere on the page and the assertion
        green. Verified by mutating the scalar branch alone — all twelve tests
        stayed green. So this counts the dashes against the number of empty
        values the data actually has.
        """
        out = render_in_node(tmp_path, mixed_board)[SECTION]
        by_col: dict[str, list[str]] = {}
        for cell in out["cells"]:
            by_col.setdefault(cell["col"], []).append(cell["text"])

        # BOTH renderer branches, asserted separately. `EM_DASH_COLUMNS` names
        # one column rendered by the scalar branch and one by the tags branch,
        # each of which has a genuinely empty value in the fixture.
        for column in EM_DASH_COLUMNS:
            assert "—" in by_col.get(column, []), (
                f"column {column!r} has an empty value in the fixture and did not "
                "render an em dash — that renderer branch is broken"
            )

        for cell in out["cells"]:
            assert cell["text"] not in (
                "None", "null", "undefined", "NaN", "false", "[object Object]"
            ), f"{cell['col']} leaked {cell['text']!r} — it must render as an em dash"
            assert cell["text"].strip() != "", (
                f"{cell['col']} rendered empty — indistinguishable from a value "
                "that is genuinely blank"
            )

    def test_the_untagged_option_exists_and_is_not_the_default(self, tmp_path, mixed_board):
        out = render_in_node(tmp_path, mixed_board)[SECTION]
        options = out["options"][FACET]
        assert options[0] == "All", "the default must show everything"
        assert any("untagged" in o for o in options)

    def test_filtering_on_a_tag_never_hides_a_row_that_has_it(self, tmp_path, mixed_board):
        out = render_in_node(
            tmp_path, mixed_board,
            [{"type": "select", "section": SECTION, "key": FACET, "value": FACET_VALUE}],
        )[SECTION]
        assert out["count"] == f"showing {FACET_VALUE_ROWS} of 4"
        assert out["rows"] == FACET_VALUE_ROWS

    def test_the_untagged_option_selects_exactly_the_untagged_rows(self, tmp_path, mixed_board):
        """The other half of "never hidden": an untagged row is not merely
        present in the data, it is REACHABLE — there is a filter setting that
        shows it."""
        out = render_in_node(
            tmp_path, mixed_board,
            [{"type": "select", "section": SECTION, "key": FACET, "value": "—"}],
        )[SECTION]
        assert out["count"] == f"showing {UNTAGGED_ROWS} of 4"
        assert out["rows"] == UNTAGGED_ROWS

    def test_an_over_narrow_filter_reads_as_a_filter_not_as_no_data(
        self, tmp_path, mixed_board
    ):
        """The distinction the whole redesign turns on: "nothing matched" and
        "nothing was there" must not render identically."""
        out = render_in_node(
            tmp_path, mixed_board,
            [{"type": "search", "section": SECTION, "value": "zzzz-no-such-thing"}],
        )[SECTION]
        assert out["count"] == "showing 0 of 4", "the denominator is what says the data is there"
        assert "No rows match these filters" in (out["empty"] or "")

    def test_an_empty_dataset_says_something_different(self, tmp_path, empty_board):
        out = render_in_node(tmp_path, empty_board)[SECTION]
        assert out["count"] == "showing 0 of 0"
        assert "No rows match" not in (out["empty"] or "")

    def test_reset_restores_every_row(self, tmp_path, mixed_board):
        out = render_in_node(
            tmp_path, mixed_board,
            [
                {"type": "select", "section": SECTION, "key": FACET, "value": FACET_VALUE},
                {"type": "search", "section": SECTION, "value": "zzzz"},
                {"type": "reset", "section": SECTION},
            ],
        )[SECTION]
        assert out["count"] == "showing 4 of 4"

    def test_search_matches_across_the_search_keys(self, tmp_path, mixed_board):
        out = render_in_node(
            tmp_path, mixed_board,
            [{"type": "search", "section": SECTION, "value": SEARCH_TERM}],
        )[SECTION]
        assert 0 < out["rows"] < 4

    def test_sorting_does_not_change_how_many_rows_are_shown(self, tmp_path, mixed_board):
        out = render_in_node(
            tmp_path, mixed_board,
            [{"type": "select", "section": SECTION, "key": "dir", "value": "desc"}],
        )[SECTION]
        assert out["count"] == "showing 4 of 4"
        assert out["rows"] == 4

    def test_a_javascript_url_is_never_clickable(self, tmp_path, hostile_board):
        """The one place a value from the data could become executable.

        Two halves, and the second is the one that keeps this consistent with
        everything else here: the unsafe row is NOT hidden. It renders, with
        its text intact, just not as a link. Dropping the row would be the
        board deciding what the reader may see, which is the behaviour this
        whole redesign exists to remove."""
        out = render_in_node(tmp_path, hostile_board)[SECTION]
        for link in out["links"]:
            assert link["href"].lower().startswith(("http://", "https://", "mailto:"))
        assert len(out["links"]) == 2, "javascript: and a relative path are not links"
        assert out["rows"] == 4, "and every row is still on the page"
        assert out["count"] == "showing 4 of 4"

    def test_a_hostile_title_does_not_execute(self, tmp_path, hostile_board):
        out = render_in_node(tmp_path, hostile_board)[SECTION]
        assert any("</script>" in c["text"] for c in out["cells"]), "rendered as text, not markup"
