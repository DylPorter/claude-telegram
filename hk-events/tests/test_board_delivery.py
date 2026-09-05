"""Board delivery: the HTML board as a Telegram document attachment.

The board is written to the operator's filesystem, which is not where he reads
things — the phone is. These cover the delivery path that closes that gap, and
the three properties it must not break:

  * ONE notification. The fleet was cut from ~12 bubbles a day to ~5 on
    purpose. Attaching the board must swap the summary bubble for a document
    carrying the same text as its caption, not add a second message.
  * Loud failure. A board that could not be sent must still produce the summary
    bubble, with the reason in it. Seven branches of this repo exist to delete
    failures that look like quiet days.
  * No arbitrary paths on the wire. The caller names a KEY the bot maps to a
    file; it never names a file.
"""

from __future__ import annotations

from datetime import date

import pytest

from hk_events import config, orchestrator, telegram_client
from hk_events.render import render, summary_index
from hk_events.telegram_client import TelegramPushError, push_document, push_with_board


class _Recorder:
    """Captures what would have gone to Telegram, in order."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def send_text(self, messages: list[str]) -> None:
        self.events.append(("text", list(messages)))

    def send_document(self, board, *, caption=None, parse_mode="Markdown") -> dict:
        self.events.append(("document", board, caption))
        return {"sent": [1]}

    def failing_document(self, board, *, caption=None, parse_mode="Markdown") -> dict:
        self.events.append(("document-attempt", board, caption))
        raise TelegramPushError("push-document failed: 502 sendDocument failed: boom")

    @property
    def texts(self) -> list[str]:
        return [m for kind, *rest in self.events if kind == "text" for m in rest[0]]

    @property
    def kinds(self) -> list[str]:
        return [e[0] for e in self.events]


_MESSAGES = ["\u26a0\ufe0f ALARM banner", "\U0001f4cb summary bubble", "\u26a0\ufe0f source health"]


class TestAttachmentIsOffByDefault:
    """Unset config must leave this fleet exactly as it was."""

    def test_no_key_means_a_plain_push(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES,
            summary_index=1,
            board_key=None,
            board_written=True,
            send_text=rec.send_text,
            send_document=rec.send_document,
        )
        assert rec.kinds == ["text"]
        assert rec.texts == _MESSAGES

    def test_board_attach_key_is_none_when_the_env_var_is_unset(self, monkeypatch):
        monkeypatch.delenv(config.BOARD_ATTACH_ENV, raising=False)
        assert config.board_attach_key() is None

    def test_board_attach_key_is_none_when_the_env_var_is_blank(self, monkeypatch):
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "   ")
        assert config.board_attach_key() is None

    def test_board_attach_key_is_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "a-board")
        assert config.board_attach_key() == "a-board"
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "another-board")
        assert config.board_attach_key() == "another-board"


class TestOneNotification:
    """The summary bubble becomes the caption. It is not sent twice, and it is
    not joined by a bubble of its own."""

    def test_the_summary_is_the_caption_and_is_not_also_a_message(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES,
            summary_index=1,
            board_key="a-board",
            board_written=True,
            send_text=rec.send_text,
            send_document=rec.send_document,
        )
        assert ("document", "a-board", "\U0001f4cb summary bubble") in rec.events
        assert "\U0001f4cb summary bubble" not in rec.texts

    def test_the_bubble_count_is_unchanged_by_attaching(self):
        plain, attached = _Recorder(), _Recorder()
        push_with_board(
            _MESSAGES, summary_index=1, board_key=None, board_written=True,
            send_text=plain.send_text, send_document=plain.send_document,
        )
        push_with_board(
            _MESSAGES, summary_index=1, board_key="a-board", board_written=True,
            send_text=attached.send_text, send_document=attached.send_document,
        )
        n_plain = len(plain.texts)
        n_attached = len(attached.texts) + sum(1 for k in attached.kinds if k == "document")
        assert n_attached == n_plain == 3

    def test_the_banners_still_lead_and_health_still_follows(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES, summary_index=1, board_key="a-board", board_written=True,
            send_text=rec.send_text, send_document=rec.send_document,
        )
        # A reader who stops after the first bubble must still have seen the alarm.
        assert rec.events[0] == ("text", ["\u26a0\ufe0f ALARM banner"])
        assert rec.events[1][0] == "document"
        assert rec.events[2] == ("text", ["\u26a0\ufe0f source health"])

    def test_a_lone_summary_becomes_a_lone_document(self):
        rec = _Recorder()
        push_with_board(
            ["only the summary"], summary_index=0, board_key="a-board", board_written=True,
            send_text=rec.send_text, send_document=rec.send_document,
        )
        assert rec.kinds == ["document"]

    def test_an_empty_message_list_sends_nothing(self):
        rec = _Recorder()
        push_with_board(
            [], summary_index=0, board_key="a-board", board_written=True,
            send_text=rec.send_text, send_document=rec.send_document,
        )
        assert rec.events == []


class TestFailureIsLoud:
    """A board that did not arrive must not read as a quiet day."""

    def test_a_failed_document_still_sends_the_summary(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES, summary_index=1, board_key="a-board", board_written=True,
            send_text=rec.send_text, send_document=rec.failing_document,
        )
        assert any("summary bubble" in m for m in rec.texts)

    def test_the_failure_reason_is_visible_in_the_bubble(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES, summary_index=1, board_key="a-board", board_written=True,
            send_text=rec.send_text, send_document=rec.failing_document,
        )
        fallback = next(m for m in rec.texts if "summary bubble" in m)
        assert "Board not attached" in fallback
        assert "sendDocument failed" in fallback

    def test_a_failed_document_does_not_kill_the_rest_of_the_run(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES, summary_index=1, board_key="a-board", board_written=True,
            send_text=rec.send_text, send_document=rec.failing_document,
        )
        assert "\u26a0\ufe0f source health" in rec.texts
        assert "\u26a0\ufe0f ALARM banner" in rec.texts

    def test_a_failed_document_preserves_the_ordering(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES, summary_index=1, board_key="a-board", board_written=True,
            send_text=rec.send_text, send_document=rec.failing_document,
        )
        assert rec.texts[0] == "\u26a0\ufe0f ALARM banner"
        assert "summary bubble" in rec.texts[1]
        assert rec.texts[2] == "\u26a0\ufe0f source health"

    def test_a_run_that_wrote_no_board_falls_back_to_a_plain_push(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES, summary_index=1, board_key="a-board", board_written=False,
            send_text=rec.send_text, send_document=rec.send_document,
        )
        assert rec.kinds == ["text"]
        assert rec.texts == _MESSAGES

    def test_an_out_of_range_summary_index_falls_back_rather_than_raising(self):
        rec = _Recorder()
        push_with_board(
            _MESSAGES, summary_index=9, board_key="a-board", board_written=True,
            send_text=rec.send_text, send_document=rec.send_document,
        )
        assert rec.kinds == ["text"]
        assert rec.texts == _MESSAGES


class TestSummaryIndexTracksRender:
    """`summary_index` names an entry of `render()`'s list. If the two drift,
    attachment captions the wrong bubble — so pin them together."""

    @pytest.mark.parametrize(
        "alarm,notice",
        [(None, None), ("ALARM", None), (None, "NOTICE"), ("ALARM", "NOTICE")],
    )
    def test_the_index_points_at_the_summary_for_every_banner_combination(
        self, alarm, notice
    ):
        messages = render(staleness_alarm=alarm, drop_notice=notice, **_RENDER_KWARGS)
        idx = summary_index(staleness_alarm=alarm, drop_notice=notice)
        assert 0 <= idx < len(messages)
        assert messages[idx].startswith(_SUMMARY_PREFIX)

    @pytest.mark.parametrize(
        "alarm,notice",
        [("ALARM", None), (None, "NOTICE"), ("ALARM", "NOTICE")],
    )
    def test_every_entry_before_the_index_is_a_banner(self, alarm, notice):
        messages = render(staleness_alarm=alarm, drop_notice=notice, **_RENDER_KWARGS)
        idx = summary_index(staleness_alarm=alarm, drop_notice=notice)
        banners = [b for b in (alarm, notice) if b]
        assert messages[:idx] == banners


class TestPushDocumentNeverCarriesAPath:
    """The wire format is a KEY. The bot owns the key -> path mapping."""

    def _fake_post(self, captured):
        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"sent": [1]}

        def _post(url, json=None, headers=None, timeout=None):
            captured.append((url, json, headers))
            return _Resp()

        return _post

    def test_the_payload_carries_the_key_and_nothing_path_shaped(self, monkeypatch):
        captured: list = []
        monkeypatch.setattr(telegram_client, "PUSH_SECRET", "x" * 32)
        monkeypatch.setattr(telegram_client.requests, "post", self._fake_post(captured))
        push_document("a-board", caption="hello")
        url, payload, _headers = captured[0]
        assert payload["board"] == "a-board"
        assert set(payload) <= {"board", "caption", "parseMode"}
        assert "path" not in payload and "file" not in payload
        assert url.endswith("/push-document")

    def test_it_posts_to_localhost_only(self, monkeypatch):
        captured: list = []
        monkeypatch.setattr(telegram_client, "PUSH_SECRET", "x" * 32)
        monkeypatch.setattr(telegram_client.requests, "post", self._fake_post(captured))
        push_document("a-board")
        assert captured[0][0].startswith("http://127.0.0.1:")

    def test_an_oversized_caption_is_refused_before_any_http(self, monkeypatch):
        monkeypatch.setattr(telegram_client, "PUSH_SECRET", "x" * 32)
        monkeypatch.setattr(
            telegram_client.requests,
            "post",
            lambda *a, **k: pytest.fail("an oversized caption reached the network"),
        )
        with pytest.raises(TelegramPushError, match="over Telegram's"):
            push_document("a-board", caption="x" * (telegram_client.MAX_CAPTION_CHARS + 1))

    def test_a_caption_at_the_limit_is_allowed(self, monkeypatch):
        captured: list = []
        monkeypatch.setattr(telegram_client, "PUSH_SECRET", "x" * 32)
        monkeypatch.setattr(telegram_client.requests, "post", self._fake_post(captured))
        push_document("a-board", caption="x" * telegram_client.MAX_CAPTION_CHARS)
        assert captured

    def test_a_missing_secret_raises_rather_than_posting_unauthenticated(
        self, monkeypatch
    ):
        monkeypatch.setattr(telegram_client, "PUSH_SECRET", "")
        monkeypatch.setattr(
            telegram_client.requests,
            "post",
            lambda *a, **k: pytest.fail("posted without a secret"),
        )
        with pytest.raises(TelegramPushError, match="PUSH_SECRET"):
            push_document("a-board")

    def test_the_secret_is_sent_as_a_header_and_never_in_the_body(self, monkeypatch):
        captured: list = []
        monkeypatch.setattr(telegram_client, "PUSH_SECRET", "sentinel-secret-value-32chars!!")
        monkeypatch.setattr(telegram_client.requests, "post", self._fake_post(captured))
        push_document("a-board", caption="hello")
        _url, payload, headers = captured[0]
        assert headers["X-Push-Secret"] == "sentinel-secret-value-32chars!!"
        assert "sentinel" not in str(payload)

    def test_an_error_status_raises_rather_than_returning_quietly(self, monkeypatch):
        class _Resp:
            status_code = 503
            text = "document delivery not configured (PUSH_DOCUMENTS unset)"

        monkeypatch.setattr(telegram_client, "PUSH_SECRET", "x" * 32)
        monkeypatch.setattr(
            telegram_client.requests, "post", lambda *a, **k: _Resp()
        )
        with pytest.raises(TelegramPushError, match="503"):
            push_document("a-board")

    def test_a_transport_error_becomes_a_push_error_the_caller_can_degrade_on(
        self, monkeypatch
    ):
        def _boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(telegram_client, "PUSH_SECRET", "x" * 32)
        monkeypatch.setattr(telegram_client.requests, "post", _boom)
        with pytest.raises(TelegramPushError, match="connection refused"):
            push_document("a-board")


class TestOrchestratorDelivery:
    """`_deliver` is the seam the orchestrator actually calls."""

    def test_it_attaches_when_configured(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "a-board")
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(orchestrator, "push_document", rec.send_document)
        board = orchestrator._BoardWrite(_FAKE_BOARD_PATH)
        orchestrator._deliver(
            _MESSAGES, board, staleness_alarm="\u26a0\ufe0f ALARM banner", drop_notice=None
        )
        assert rec.events[1][0] == "document"
        assert rec.events[1][2] == "\U0001f4cb summary bubble"

    def test_it_does_not_attach_when_unconfigured(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.delenv(config.BOARD_ATTACH_ENV, raising=False)
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(
            orchestrator, "push_document", lambda *a, **k: pytest.fail("attached while off")
        )
        board = orchestrator._BoardWrite(_FAKE_BOARD_PATH)
        orchestrator._deliver(_MESSAGES, board, staleness_alarm=None, drop_notice=None)
        assert rec.kinds == ["text"]

    def test_it_does_not_attach_when_the_board_was_not_written(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "a-board")
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(
            orchestrator, "push_document", lambda *a, **k: pytest.fail("attached with no board")
        )
        board = orchestrator._BoardWrite(None, "dry run")
        orchestrator._deliver(_MESSAGES, board, staleness_alarm=None, drop_notice=None)
        assert rec.texts == _MESSAGES

    def test_it_uses_the_transports_from_the_orchestrator_namespace(self, monkeypatch):
        """The suite patches `orchestrator.push_messages`; delivery must read it
        from there at call time, not capture the module function at import."""
        rec = _Recorder()
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "a-board")
        monkeypatch.setattr(orchestrator, "push_messages", rec.send_text)
        monkeypatch.setattr(orchestrator, "push_document", rec.failing_document)
        board = orchestrator._BoardWrite(_FAKE_BOARD_PATH)
        orchestrator._deliver(_MESSAGES, board, staleness_alarm=None, drop_notice=None)
        assert "document-attempt" in rec.kinds
        assert any("Board not attached" in m for m in rec.texts)


class TestDryRunSendsNothing:
    def test_dry_run_neither_writes_a_board_nor_attaches_one(self, monkeypatch, tmp_path):
        monkeypatch.setenv(config.BOARD_ATTACH_ENV, "a-board")
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setattr(config, "assert_required", lambda: None)
        monkeypatch.setattr(orchestrator, "_fetch_all_sources", lambda: ([], {}, []))
        monkeypatch.setattr(
            orchestrator, "push_messages", lambda msgs: pytest.fail("dry-run pushed")
        )
        monkeypatch.setattr(
            orchestrator, "push_document", lambda *a, **k: pytest.fail("dry-run attached")
        )
        monkeypatch.setattr(
            orchestrator, "write_archive", lambda *a, **k: pytest.fail("dry-run wrote")
        )
        monkeypatch.setattr(orchestrator, "_update_event_register", lambda *a, **k: ([], 0))
        monkeypatch.setenv("HK_EVENTS_PUSH_EMPTY", "1")
        assert orchestrator.run(dry_run=True) == 0

    def test_a_dry_run_board_write_reports_the_dry_run_as_its_reason(self):
        assert orchestrator._write_board([], date.today(), dry_run=True).path is None


_FAKE_BOARD_PATH = "/tmp/does-not-need-to-exist/board.html"
_SUMMARY_PREFIX = "🎟"
_RENDER_KWARGS = dict(
    surfaced=[],
    total_new=0,
    total_processed=0,
    calendar_stats=None,
    today=date(2026, 1, 2),
    upcoming_count=0,
)
