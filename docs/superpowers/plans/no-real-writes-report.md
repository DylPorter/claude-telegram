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

`job-sift`'s `write_open_roles` was the worst of these. It rewrites a
*hand-annotated* register rather than a regenerable digest, and exactly one test in
a 658-test suite stubbed it. (Correction to an earlier draft, which said
`orchestrator.run` calls it "unconditionally": it does not — there is a `dry_run`
early return at `orchestrator.py:356-361`. Any non-dry-run path reaches it, which is
what matters here, but the original wording overstated it.)

## Real-write paths reachable from a test

- vault daily archive (`vault_note.write_archive`) — **this is the one that fired**
- vault Open Roles register (`job-sift vault_note.write_open_roles`)
- vault HTML board (`config.board_path()`)
- `.data/state/` seen-sets, relevance log, open-events register, source health
- `.data/cache/calendar_synced.json` (calendar idempotency map)
- `.data/cookies/cedars.json` (live session credential)
- events/jobs feed JSON shared between the two projects

## The guard

An autouse `sandbox_real_paths` fixture in each suite points **every**
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

---

# Round 2 — review findings addressed

## Critical: `signal-brief` had no `conftest.py` at all

The earlier claim that the fixture was in "each of the three suites" was wrong: it
was in two. `signal-brief` had no conftest anywhere, and the suite was writing
`.data/logs/<today>-{morning,evening,weekly}.log` on every run — stamped with the
second the suite ran, not the 07:02 / 22:03 / Sun 20:14 the cron fires. Invisible
because `.gitignore` covers `.data/logs/*.log`.

Fixed with a **root** `signal-brief/conftest.py`, not `tests/conftest.py`. `config.py`
calls `load_dotenv()` and freezes `VAULT_ROOT` (line 24) and `DATA_DIR`/`CACHE_DIR`/
`LOG_DIR` (26-28) at import, so the sandbox environment has to be in `os.environ`
before the first `import signal_brief` — which means module scope of the first
conftest pytest loads. `load_dotenv` does not override an already-set variable, so
`SIGNAL_BRIEF_VAULT_ROOT` wins over the shared `.env` without editing it.

Every module binds paths with `from signal_brief.config import <X>`, and three freeze
them again (`exposure.EXPOSURE_FILE:21`, `sources/rss.SEEN_CACHE:25`,
`threads.THREADS_STATE_PATH:54`). The fixture patches the config module *and* all
16 bound names, including `LOG_DIR` on each of the three orchestrators.

**Agent spawn:** `threads.py` and `vault_agent.py` run `claude --permission-mode
bypassPermissions` with `cwd=VAULT_ROOT`. A `no_agent_spawn` autouse fixture now
inspects argv and refuses any such spawn. It guards argv rather than blocking
`subprocess.run` wholesale, because pytest itself uses subprocess legitimately.

## Important 1: both backstops would have cried wolf

`_REAL_PATHS` was too broad in both repos. `job-sift`'s `STATE_DIR`, `COOKIE_DIR`
and `LOG_DIR` are written by `job-sift-keepalive.timer` **every ten minutes** —
ticks observed at 01:33:39 and 01:44:40 during this round alone. hk-events'
`VAULT_ROOT` was the whole vault: ~1800 files under continuous write by four
signal-brief timers, gbrain sync and every git command.

Narrowed to what the suite can actually write and no timer touches on a short
cadence: hk-events `{ARCHIVE_DIR, BOARD_PATH, CACHE_DIR}`, job-sift
`{ARCHIVE_DIR, OPEN_ROLES_PATH, BOARD_PATH}`, signal-brief `{LOG_DIR, CACHE_DIR}`.
After an incident like this one a backstop that cries wolf is worse than none,
because the next real alarm gets waved off.

## Important 2: feed reads escaped the sandbox — same backwards shape

`HK_EVENTS_JOBS_FEED` / `JOB_SIFT_EVENTS_FEED` were being **deleted**, and those two
resolvers have no config attribute behind them: they fall through to a hardcoded
`BOT_ROOT/<sibling>/.data/state/*.json`. Clearing them steered a read straight out
of the sandbox — precisely the mistake the sibling-repo commit message criticises.
They are now **set** to sandbox paths. Env vars that *do* have an attribute behind
them are still cleared, which is correct, and the two groups are now named
separately (`_PATH_ENV_VARS` vs `_FEED_ENV_VARS`) so the distinction is explicit.

## Minors

- `_REAL_DATA_DIR` resolved to `~/.local/share` for a non-editable install. The
  backstop now stands down rather than watching a shared user directory.
- `test_the_write_targets_are_redirected_not_merely_cleared` compared against the
  substring `"sandbox"`, coupling it to `mktemp`'s prefix. It now compares against
  the directory `clean_env` actually returned.
- Corrected the "unconditionally" overstatement about `write_open_roles` above.

## Round-2 verification

All in a throwaway copy with `DEFAULT_CWD` and every path env var pointed at temp,
with the three `.data` dirs replicated so the backstops had real content to compare.

- signal-brief: today's logs deleted from the replica, suite re-run → **not
  recreated**, and the `.data` tree hashes identical before and after.
- signal-brief sandbox neutered → three logs reappear and
  `_assert_real_dirs_untouched` fires naming `LOG_DIR`.
- Agent-spawn guard: a test issuing a real `claude --permission-mode
  bypassPermissions` is refused.
- hk-events reintroduction still fails
  `test_a_live_run_archives_into_the_redirected_vault_only` by name, fake vault empty.

Live snapshots: `hk-events/.data`, `signal-brief/.data` and the vault archive dir
byte-identical. The only delta was `job-sift/.data/state/cedars_session.json`
(01:33:39 → 01:44:40), matched to `job-sift-keepalive.service` in the journal — the
timer, not the suite, and exactly the false positive that motivated narrowing
`_REAL_PATHS`.

**Note:** `signal-brief/.data/logs/2026-09-05-{morning,evening,weekly}.log` (all
stamped 01:32:36) are residue from the reviewer's run of the previous commit. They
are gitignored and harmless; delete at will.
