# No real writes from the test suites

## The bug

`hk-events/tests/test_orchestrator.py::TestDryRunWritesNoState` drives a live
`orchestrator.run(dry_run=False)`. It stubbed every outbound writer — `sync_events`,
`_update_event_register`, `_write_board`, `push_messages`, `save_seen` — except
`write_archive`. `config.VAULT_ROOT` is derived from `DEFAULT_CWD`, which on a
developer machine is the real Obsidian vault, so every full-suite run overwrote
`<VAULT_ROOT>/Inbox/HK Events/<today>.md` with two fixture rows.

It fired at least twice. `Inbox/HK Events/2026-09-04.md` is **still** fixture
content (376 bytes, "luma event 1"/"luma event 2") and is not recoverable from
this repo — flagged for the operator, not modified here.

The deeper cause is not a forgotten stub. The suite's **default was to write to
real locations**, and each test had to opt OUT one writer at a time, from memory.
Every new writer reopened the hole.

## Import-time bindings found

Commit `3a19ac0` fixed three (`dedupe.STATE_DIR`, `calendar_sync.CACHE_DIR`,
`vault_note.HK_EVENTS_ARCHIVE_DIR`). Its approach — resolve through the `config`
*module* on every call, never `from config import X` — is correct and is the one
extended here.

| Module | Bound at import | Status |
|---|---|---|
| `hk-events/hk_events/{dedupe,calendar_sync,vault_note}.py` | `STATE_DIR`, `CACHE_DIR`, `HK_EVENTS_ARCHIVE_DIR` | fixed in `3a19ac0` |
| `job-sift/job_sift/vault_note.py` | `VAULT_ROOT`, `JOB_SIFT_ARCHIVE_DIR`, `OPEN_ROLES_PATH` | **fixed here** — same class, worse blast radius |
| `job-sift/job_sift/sources/cedars.py`, `refresh_cookie.py` | `CEDARS_COOKIES_PATH` | read-only (live credential read); redirected in the fixture |
| `hk-events` `config.BOARD_PATH`, `config.HK_EVENTS_ARCHIVE_DIR` | module globals off `VAULT_ROOT` | patching `VAULT_ROOT` alone does **not** redirect them; fixture patches each |

`job-sift`'s `write_open_roles` was the worst of these: `orchestrator.run` calls it
unconditionally, it rewrites a *hand-annotated* register rather than a regenerable
digest, and exactly one test in a 658-test suite stubbed it.

## Real-write paths reachable from a test

- vault daily archive (`vault_note.write_archive`) — **this is the one that fired**
- vault Open Roles register (`job-sift vault_note.write_open_roles`)
- vault HTML board (`config.board_path()`)
- `.data/state/` seen-sets, relevance log, open-events register, source health
- `.data/cache/calendar_synced.json` (calendar idempotency map)
- `.data/cookies/cedars.json` (live session credential)
- events/jobs feed JSON shared between the two projects

## The guard

An autouse `sandbox_real_paths` fixture in each of the three suites points **every**
configurable output path at a per-test tmp dir. A test must now opt IN to touching
anything real. It closes both surfaces that matter: the `config` attributes *and*
the env vars (`*_BOARD_PATH`, `DEFAULT_CWD`, …) that would otherwise win over a
patched attribute from the developer's shell.

Because monkeypatch is last-write-wins and autouse fixtures run first, existing
per-test patching keeps working unchanged.

A session-scoped `_assert_real_dirs_untouched` is the backstop: it snapshots the
real directories by **size + mtime** before the session and re-checks after.
Size+mtime, not existence — the notes these suites can clobber already exist on a
real machine, so `assert not path.exists()` both misses the clobber and fails
spuriously for every genuine user.

## Sibling repo (`hku-cedars-scraper`)

Its `clean_env` cleared `CEDARS_STATE_PATH`/`CEDARS_BOARD_PATH`. That is not
sufficient — it is backwards. `cmd_board` resolves flag → env → `default_state_path()`,
so **clearing** the env var routes an un-flagged `main(["board"])` straight into the
checkout's own `.data/state/register.json`. No test does that today, but that is
discipline, not structure, and the README tells a student to run the suite.

`clean_env` now **redirects** both write targets into a tmp sandbox, and the same
size+mtime session backstop guards the whole `.data/` tree (cookies included). The
old `assert not default_state_path().exists()` guard was replaced: it would have
failed for any student who had actually run `cedars board`.

## Verification

Done in a throwaway copy of the repo with `DEFAULT_CWD` and every path env var
pointed at a temp dir. The live checkout was never used to reproduce the defect.

- **Stage 1** — defect reintroduced (explicit redirect removed from `_run`), sandbox
  fixture left in place: `TestDryRunWritesNoState::test_a_live_run_archives_into_the_redirected_vault_only`
  **fails by name**, and the fake vault stays empty. The bug is caught *and*
  contained.
- **Stage 2** — defect reintroduced *and* the sandbox fixture neutered: the fake
  vault receives the exact 376-byte fixture note, and `_assert_real_dirs_untouched`
  fails, naming all three damaged paths.

The assertion in the original test was strengthened, not weakened. The live run
still proves every classification is logged; `write_archive` is deliberately left
un-stubbed so the run remains genuinely live, with the archive redirected and
asserted to land in the tmp vault. Two tests were added (dry-run writes no archive;
live run archives only into the redirected vault).
