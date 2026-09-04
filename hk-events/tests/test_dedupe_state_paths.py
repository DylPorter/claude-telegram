"""The suite's sandbox has to actually hold, for every module that resolves a
write path off `config`.

`dedupe`, `calendar_sync`, and `vault_note` used to do
`from hk_events.config import STATE_DIR` / `CACHE_DIR` / `HK_EVENTS_ARCHIVE_DIR`
(and `VAULT_ROOT`), binding the path at IMPORT time. A test that points
`config.STATE_DIR` (etc.) at a tmp_path therefore did not redirect them at all —
it only looked like it did. `source_health` had already been written the other
way and says why (see hk_events/source_health.py). job-sift carries the same
guard in tests/test_dedupe_collapse.py::TestStateDirIsRedirectable.

Nothing fired in practice here either: every existing hk-events test that drives
`dedupe`/`calendar_sync`/`vault_note` also patches `config.STATE_DIR` (or stubs
the caller entirely), so the suite wrote zero real state files before this fix.
But the guard was load-bearing-by-luck — DEFAULT_CWD in this checkout's .env
points `VAULT_ROOT` at the real Obsidian vault, so an un-patched `write_archive`
call would have landed a markdown file in `Inbox/HK Events/` on a developer
machine, and an un-patched `dedupe`/`calendar_sync` call would have landed in
this repo's own live `.data/state/` and `.data/cache/`.

These fail the moment any of the three modules re-binds the name.
"""

from __future__ import annotations

from datetime import date

from hk_events import calendar_sync, config, dedupe, vault_note


class TestStateDirIsRedirectable:
    def test_dedupe_resolves_the_state_dir_at_call_time(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        assert dedupe._seen_path("luma") == tmp_path / "seen_luma.json"
        assert dedupe._log_path() == tmp_path / "relevance_log.jsonl"

    def test_calendar_sync_resolves_the_cache_dir_at_call_time(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
        assert calendar_sync._synced_path() == tmp_path / "calendar_synced.json"

    def test_vault_note_resolves_the_archive_dir_at_call_time(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(config, "HK_EVENTS_ARCHIVE_DIR", tmp_path / "archive")
        path = vault_note.write_archive(date(2026, 9, 4), "hello")
        assert path == tmp_path / "archive" / "2026-09-04.md"
        assert path.read_text() == "hello"


class TestNoWriterEscapesThePatch:
    """The property that matters, asserted on the filesystem rather than on a
    path string: with the patch in place, every state writer lands inside
    tmp_path and the REAL state/cache directories gain nothing.

    Named explicitly so a leak points straight at the live path: if this fails,
    something wrote into the checkout's own hk-events/.data/state/ or
    hk-events/.data/cache/ instead of the patched tmp_path.
    """

    def test_no_writer_escapes_the_patch(self, monkeypatch, tmp_path):
        real_state = config.STATE_DIR
        real_cache = config.CACHE_DIR
        before_state = set(real_state.iterdir()) if real_state.exists() else set()
        before_cache = set(real_cache.iterdir()) if real_cache.exists() else set()

        fake_state = tmp_path / "state"
        fake_cache = tmp_path / "cache"
        fake_state.mkdir()
        fake_cache.mkdir()
        monkeypatch.setattr(config, "STATE_DIR", fake_state)
        monkeypatch.setattr(config, "CACHE_DIR", fake_cache)

        dedupe.save_seen("luma", {"1": {"stages": ["new"], "tag": None}})
        calendar_sync._save_synced({"abc123": {"gcal_id": "x"}})

        assert {p.name for p in fake_state.iterdir()} == {"seen_luma.json"}
        assert {p.name for p in fake_cache.iterdir()} == {"calendar_synced.json"}

        after_state = set(real_state.iterdir()) if real_state.exists() else set()
        after_cache = set(real_cache.iterdir()) if real_cache.exists() else set()
        assert after_state == before_state, (
            f"a writer escaped the patch into the REAL state dir {real_state}"
        )
        assert after_cache == before_cache, (
            f"a writer escaped the patch into the REAL cache dir {real_cache}"
        )
