"""The board as a LINK rather than an attachment.

Mirror of `job-sift/tests/test_board_url.py` — the two projects deliver the
same way on purpose, and the parity is what stops one of them drifting into a
second, subtly different delivery shape.

The board is now served over HTTPS, so the URL is always current and attaching
a ~50 KB HTML file to every daily run only accumulates copies of yesterday's
board in the chat. Setting `HK_EVENTS_BOARD_URL` swaps the attachment for a
Markdown link in the summary bubble.

What these pin:

  * ONE bubble, still. The link is a LINE in the summary, not a message of its
    own.
  * THE URL WINS when both it and `HK_EVENTS_BOARD_ATTACH` are set.
  * The attachment is still there as the FALLBACK — a deployment with nowhere
    to serve from is byte-for-byte unchanged.
  * The staleness alarm and the ⚠️ source-health line are untouched and still
    push as bubbles of their own.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from hk_events import config, orchestrator
from hk_events.render import render
from hk_events.telegram_client import TelegramPushError

_DAY = date(2026, 9, 9)
_URL = "https://boards.example.test/events-board.html"
_FAKE_BOARD_PATH = Path("/tmp/does-not-matter/Events Board.html")
_MESSAGES = ["⚠️ ALARM banner", "\U0001f39f summary bubble", "⚠️ source health"]


def _render(**kw) -> list[str]:
    base = dict(
        surfaced=[],
        total_new=0,
        total_processed=0,
        calendar_stats=None,
        today=_DAY,
        upcoming_count=0,
    )
    base.update(kw)
    return render(**base)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def send_text(self, messages: list[str]) -> None:
        self.events.append(("text", list(messages)))

    def send_document(self, board, *, caption=None, parse_mode="Markdown") -> dict:
        self.events.append(("document", board, caption))
        return {"sent": [1]}

    @property
    def texts(self) -> list[str]:
        return [m for kind, *rest in self.events if kind == "text" for m in rest[0]]

    @property
    def kinds(self) -> list[str]:
        return [e[0] for e in self.events]


# ---------------------------------------------------------------------------
# The config accessor
# ---------------------------------------------------------------------------

class TestBoardUrlConfig:
    def test_unset_is_none(self, monkeypatch):
        monkeypatch.delenv(config.BOARD_URL_ENV, raising=False)
        assert config.board_url() is None

    def test_blank_is_none(self, monkeypatch):
        monkeypatch.setenv(config.BOARD_URL_ENV, "   ")
        assert config.board_url() is None

    @pytest.mark.parametrize(
        "raw", ["https://boards.example.test/b.html", "http://example.test:8080/b.html"]
    )
    def test_http_and_https_are_accepted(self, monkeypatch, raw):
        monkeypatch.setenv(config.BOARD_URL_ENV, raw)
        assert config.board_url() == raw

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv(config.BOARD_URL_ENV, f"  {_URL}\n")
        assert config.board_url() == _URL

    @pytest.mark.parametrize(
        "raw",
        [
            "javascript:alert(1)",
            "file:///home/someone/Events%20Board.html",
            "boards.example.test/b.html",  # no scheme
            "data:text/html,<h1>hi</h1>",
        ],
    )
    def test_non_http_schemes_are_refused_and_loud(self, monkeypatch, caplog, raw):
        monkeypatch.setenv(config.BOARD_URL_ENV, raw)
        with caplog.at_level(logging.WARNING):
            assert config.board_url() is None
        assert config.BOARD_URL_ENV in caplog.text

    def test_it_is_read_at_call_time(self, monkeypatch):
        monkeypatch.delenv(config.BOARD_URL_ENV, raising=False)
        assert config.board_url() is None
        monkeypatch.setenv(config.BOARD_URL_ENV, _URL)
        assert config.board_url() == _URL


# ---------------------------------------------------------------------------
# The summary bubble
# ---------------------------------------------------------------------------

class TestSummaryCarriesTheLink:
    def test_the_url_renders_as_a_markdown_link(self):
        messages = _render(board_path=_FAKE_BOARD_PATH, board_url=_URL)
        assert f"🗂 [Board]({_URL})" in messages[0]

    def test_the_bubble_count_is_unchanged(self):
        assert len(_render(board_path=_FAKE_BOARD_PATH, board_url=_URL)) == 1
        assert len(_render(board_path=_FAKE_BOARD_PATH)) == 1

    def test_the_on_disk_path_is_not_printed_when_a_url_is_configured(self):
        messages = _render(board_path=_FAKE_BOARD_PATH, board_url=_URL)
        assert str(_FAKE_BOARD_PATH) not in messages[0]

    def test_a_failed_write_reports_no_path_at_all(self, monkeypatch, tmp_path):
        """The FAILURE branch, which the success case can never reach.

        `board_problem` is free text from `_write_board` and is rendered into
        the URL bubble verbatim, so the failure path is the one where a
        filesystem path would actually arrive. Asserted at the source as well
        as at the renderer: fixing it in `render` alone would leave the next
        reason string free to interpolate a path again.
        """
        board = tmp_path / "secret-dir" / "Events Board.html"
        board.parent.mkdir()
        monkeypatch.setattr(config, "board_path", lambda: board)
        monkeypatch.setattr(config, "events_feed_path", lambda: tmp_path / "events_feed.json")
        monkeypatch.setattr(config, "jobs_feed_path", lambda: tmp_path / "jobs_feed.json")
        monkeypatch.setattr(orchestrator.board_mod, "build_board", lambda *a, **k: 1 / 0)

        result = orchestrator._write_board([], _DAY, dry_run=False)

        assert result.path is None
        assert "could not be written" in result.problem      # the cause survives
        assert str(board) not in result.problem
        assert str(tmp_path) not in result.problem
        assert "secret-dir" not in result.problem
        assert "/" not in result.problem

        bubble = _render(
            board_path=result.path, board_problem=result.problem, board_url=_URL
        )[0]
        assert str(board) not in bubble and "secret-dir" not in bubble
        assert "Not refreshed this run" in bubble

    def test_without_a_url_the_path_line_is_exactly_as_before(self):
        messages = _render(board_path=_FAKE_BOARD_PATH)
        assert f"🗂 Board: `{_FAKE_BOARD_PATH}`" in messages[0]
        assert "[Board]" not in messages[0]

    def test_without_a_url_and_without_a_board_the_reason_is_still_given(self):
        messages = _render(board_path=None, board_problem="dry run")
        assert "🗂 Board: not written this run (dry run)." in messages[0]

    def test_a_link_to_an_unrefreshed_board_says_so(self):
        messages = _render(board_path=None, board_problem="disk full", board_url=_URL)
        assert f"🗂 [Board]({_URL})" in messages[0]
        assert "Not refreshed this run (disk full)" in messages[0]
        assert len(messages) == 1

    def test_a_refreshed_board_carries_no_staleness_note(self):
        messages = _render(board_path=_FAKE_BOARD_PATH, board_url=_URL)
        assert "Not refreshed" not in messages[0]


class TestExemptionsAreUntouched:
    def test_both_exemptions_survive_a_url_run(self):
        messages = _render(
            board_path=_FAKE_BOARD_PATH,
            board_url=_URL,
            staleness_alarm="⚠️ meetup has reported nothing for 3 days",
            source_errors={"meetup": "fetch failed"},
        )
        assert len(messages) == 3
        assert messages[0].startswith("⚠️ meetup has reported nothing")
        assert f"[Board]({_URL})" in messages[1]
        assert messages[2].startswith("⚠️")
        assert "meetup" in messages[2]

    def test_the_alarm_still_leads(self):
        messages = _render(
            board_path=None,
            board_problem="dry run",
            board_url=_URL,
            staleness_alarm="⚠️ ALARM",
        )
        assert messages[0] == "⚠️ ALARM"


# ---------------------------------------------------------------------------
# Routing: which mechanism actually delivers
# ---------------------------------------------------------------------------

class TestUrlWinsOverAttachment:
    def test_no_document_is_sent_when_a_url_is_configured(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setenv(config.BOARD_URL_ENV, _URL)
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "events-board")
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(
            orchestrator, "push_document",
            lambda *a, **k: pytest.fail("attached the file while a URL was configured"),
        )
        orchestrator._deliver(
            _MESSAGES,
            orchestrator._BoardWrite(_FAKE_BOARD_PATH),
            staleness_alarm=None,
            drop_notice=None,
        )
        assert rec.kinds == ["text"]
        assert rec.texts == _MESSAGES

    def test_the_conflict_is_logged_not_silent(self, monkeypatch, caplog):
        rec = _Recorder()
        monkeypatch.setenv(config.BOARD_URL_ENV, _URL)
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "events-board")
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(orchestrator, "push_document", rec.send_document)
        with caplog.at_level(logging.INFO, logger="hk_events"):
            orchestrator._deliver(
                _MESSAGES,
                orchestrator._BoardWrite(_FAKE_BOARD_PATH),
                staleness_alarm=None,
                drop_notice=None,
            )
        assert config.BOARD_URL_ENV in caplog.text
        assert config.BOARD_ATTACH_ENV in caplog.text

    def test_a_rejected_url_falls_back_to_the_attachment(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setenv(config.BOARD_URL_ENV, "file:///etc/passwd")
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "events-board")
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(orchestrator, "push_document", rec.send_document)
        orchestrator._deliver(
            _MESSAGES,
            orchestrator._BoardWrite(_FAKE_BOARD_PATH),
            staleness_alarm=None,
            drop_notice=None,
        )
        assert "document" in rec.kinds


class TestAttachmentIsStillTheFallback:
    def test_attach_alone_still_sends_the_document(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.delenv(config.BOARD_URL_ENV, raising=False)
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "events-board")
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(orchestrator, "push_document", rec.send_document)
        orchestrator._deliver(
            _MESSAGES,
            orchestrator._BoardWrite(_FAKE_BOARD_PATH),
            staleness_alarm="⚠️ ALARM banner",
            drop_notice=None,
        )
        assert rec.events[1][0] == "document"
        assert rec.events[1][2] == "\U0001f39f summary bubble"

    def test_neither_configured_is_a_plain_push(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.delenv(config.BOARD_URL_ENV, raising=False)
        monkeypatch.delenv(config.BOARD_ATTACH_ENV, raising=False)
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(
            orchestrator, "push_document",
            lambda *a, **k: pytest.fail("attached with nothing configured"),
        )
        orchestrator._deliver(
            _MESSAGES,
            orchestrator._BoardWrite(_FAKE_BOARD_PATH),
            staleness_alarm=None,
            drop_notice=None,
        )
        assert rec.texts == _MESSAGES

    def test_the_url_route_never_touches_the_document_endpoint(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setenv(config.BOARD_URL_ENV, _URL)
        monkeypatch.delenv(config.BOARD_ATTACH_ENV, raising=False)
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)

        def _boom(*a, **k):
            raise TelegramPushError("push-document should never have been called")

        monkeypatch.setattr(orchestrator, "push_document", _boom)
        orchestrator._deliver(
            _MESSAGES,
            orchestrator._BoardWrite(_FAKE_BOARD_PATH),
            staleness_alarm=None,
            drop_notice=None,
        )
        assert rec.kinds == ["text"]
