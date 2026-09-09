# Threads off, board by URL

Two changes on `threads-off-board-url`. Both are switches with a documented
default, not deletions: every code path that existed before still exists and is
still tested, and both features are recoverable by setting one environment
variable.

## Change 1 — thread reconciliation is gated OFF

### Why it is a flag and not a `git rm`

The pass held one stale thread and could not resolve it. Its evidence window is
fixed —
`threads.RECENT_DAYS` days of daily-note live-capture sections plus the same
span of vault git commits — so a thread whose last update predates that window
has no evidence either way, and no amount of running produces one. What it
actually bought was a Sonnet call every morning to re-derive the same stuck
answer and state it with more confidence than the evidence carried.

That is an argument for turning it off, not for destroying it. The
reconciliation logic, its renderer, its snapshot format and their tests are
untouched; the operator may want it back, or may replace it with a
hand-maintained markdown file, and neither is served by a deletion.

### The switch

```
SIGNAL_BRIEF_THREADS_ENABLED=1     # "1" / "true" / "yes" / "on"
                                   # unset, "0", "false", "no", "off" = OFF
```

`signal_brief.config.threads_enabled()`, read through the `config` **module**
at call time — never bound at import. That convention is not decoration: this
codebase has been bitten repeatedly by import-time binding (see
`job-sift/job_sift/dedupe.py:20-32`, whose version of the bug wrote to the
operator's real state instead of a test's `tmp_path`). An unrecognised value is
treated as OFF and logged, because silently guessing "on" for a typo is how a
switched-off Sonnet call comes back.

### What OFF means

`morning.py` guards the call itself, not the render:

- **No agent invocation.** `reconcile_threads()` is not called, so no
  `claude -p` subprocess is spawned. Guarding the render instead would have run
  the pass and thrown the answer away — saving nothing, which is the whole
  point.
- **No `## 🧵 Thread Reconciliation` section.** `reconcile` stays `None`, so
  `upsert_threads_section` is never reached.
- **No snapshot write.** `save_threads` is never reached.
- **The existing `threads.json` is left alone.** Turning a feature off must not
  destroy its state; flipping the flag back on resumes from the last snapshot
  rather than from nothing. Pinned by a test that writes a snapshot, runs with
  the feature off using the *genuine* writer, and asserts the file is
  byte-for-byte unchanged.
- **The absence is logged once at startup**, naming the variable and the
  untouched state file, so a future reader finds a reason where the section used
  to be instead of a silent hole.

Everything else about the morning brief is unchanged: five Telegram bubbles, the
`## 🌅 Morning Signal Brief` section, the filter rationale and the suppressed
list.

## Change 2 — the board reaches Telegram as a URL

The boards are served over HTTPS now. The URL is always current, so attaching a
~50 KB HTML file to every daily run re-sends the same information and
accumulates copies of yesterday's board in the chat.

```
JOB_SIFT_BOARD_URL=https://boards.example.test/job-board.html
HK_EVENTS_BOARD_URL=https://boards.example.test/events-board.html
```

`config.board_url()` in both projects, same call-time convention. Only
`http://` and `https://` are accepted — the value is rendered into a link that
lands on a phone, so a `javascript:` or `file:` value is refused with a warning
rather than rendered.

### Which mechanism wins

| `*_BOARD_URL` | `*_BOARD_ATTACH` | Result |
|---|---|---|
| set | set | **URL wins.** A Markdown link in the summary bubble; nothing attached. The conflict is logged. |
| set | unset | Link. |
| unset | set | **Fallback: attachment**, exactly as before — the summary bubble is the document's caption. |
| unset | unset | Plain push, byte-for-byte unchanged. |

The URL outranking the attachment is deliberate: they deliver the same board,
and honouring both would put it in the chat twice a day. A URL that is *refused*
(bad scheme) does not count as configured, so the attachment fallback still
fires — refusing a value must not disable both routes at once.

The suppression happens in `orchestrator._deliver`, by withholding the board key
from `push_with_board`, rather than inside `push_with_board` itself. That
function's job is "attach this key or don't"; routing is a decision about
configuration, not about transport.

### Invariants held

- **One bubble.** The link is a *line* in the summary bubble, never a message of
  its own. The fleet was cut to ~5 notifications a day on purpose.
- **The staleness alarm and the ⚠️ source-health line are exempt** and still
  push as their own bubbles, ahead of and after the summary respectively. They
  are the only channel separating "nothing was found" from "I could not look".
- **A stale link says so.** The URL keeps serving whatever was written last, so
  a run that did not rewrite the board renders
  `⚠️ Not refreshed this run (<reason>) — the link may be stale.` under the
  link. A link that silently serves yesterday's rows is exactly the class of
  quiet failure this codebase keeps deleting.
- **The on-disk path is not printed when a URL is configured** — it is noise on
  a phone, and a filesystem path is not something to render into a summary that
  may be read over someone's shoulder.

## Tests

| Suite | Before | After |
|---|---|---|
| job-sift | 694 | **720** |
| hk-events | 298 | **324** |
| signal-brief | 98 | **127** |
| bot (`npm test`) | 76 | **76** |

`npx tsc --noEmit` clean. No new runtime dependencies.

**Failing before, passing after** — measured, not estimated. Each measurement
reverts only the *source* of one change and runs the suite against the new
tests:

| Reverted | Result |
|---|---|
| `morning.py` only | **6 failed / 121 passed** — all six the gate tests |
| board sources (`config`/`render`/`orchestrator`, both projects) | job-sift **24 failed / 2 passed**; hk-events **24 failed / 2 passed** |

Reverting more than `morning.py` for the signal-brief measurement does not
work and the number would be fiction: `conftest.py` references
`config.THREADS_ENABLED_ENV` at module scope, so removing `config.py` raises
`AttributeError` at collection and the whole suite errors rather than failing
test-by-test.

The two board tests that pass in **both** directions are
`test_without_a_url_the_path_line_is_exactly_as_before` and
`test_without_a_url_and_without_a_board_the_reason_is_still_given` — pure
`render()` tests that pin *preserved* behaviour, which is exactly what they are
for. (Not the delivery tests: `_deliver` calls `config.board_url()`, which does
not exist pre-change, so those error out with the rest.)

No test writes real state or the real vault, and the autouse guards in each
`tests/conftest.py` were **strengthened**, not weakened:

- `job-sift` and `hk-events`: `*_BOARD_URL` added to `_DELIVERY_ENV_VARS`, so it
  is cleared per test. Without that, a value in the operator's real `.env` would
  silently move every delivery test from the attachment branch to the link
  branch — the tests would still pass, just not on the code they name.
- `signal-brief`: a new `neutral_feature_flags` autouse fixture clears
  `SIGNAL_BRIEF_THREADS_ENABLED`, for the same reason (`config.py` calls
  `load_dotenv()` at import). The one existing test that asserts the thread
  section is written now opts the feature in explicitly.

One test reads the run's own log file rather than `caplog`:
`morning._setup_logging` calls `logging.basicConfig(force=True)`, which tears
out pytest's capture handler, so a `caplog` assertion there would pass or fail
on handler ordering rather than on what was logged.

## Notes for the operator

- **Telegram Markdown link escaping — keep the served URL plain.** The link is
  emitted as `[Board](<url>)` with `parse_mode="Markdown"`, and the scheme check
  validates only the scheme. Four characters in the URL are hazards, and all
  four fail the same quiet way:

  | In the URL | What legacy Markdown does |
  |---|---|
  | `)` | terminates the link early; the tail becomes literal text |
  | `_` | opens italics, so the link target is truncated at it |
  | `*` | opens bold, same truncation |
  | ` ` (space) | passes through verbatim into a broken target |

  `_` and `*` are not hypothetical here — the projects are named `job_sift` and
  `hk_events`, so an underscore in a served filename is the likely case, not the
  exotic one. The failure mode is the bad one: Telegram accepts the message, no
  `400` is raised, and the bot's parse-mode-drop retry never engages, so the
  operator gets a **silently wrong link** rather than an error. Serve the board
  at a plain lowercase-hyphen path (`/job-board.html`) and this cannot arise.
- **`board_url()` does not check reachability.** It is configuration, not a
  probe: nothing in the run confirms the URL actually serves the board. The
  staleness line reports whether *this run rewrote the file*, which is a
  different fact from whether the server is up.
- **`job-sift` has no `.env.example`**, so `JOB_SIFT_BOARD_URL` is documented
  only in its README, while `hk-events` got a commented block in its own
  `.env.example` alongside the README. The two projects are otherwise kept in
  step; this asymmetry predates the change and was not introduced by it.
- **The `PUSH_DOCUMENTS` allowlist entry is no longer needed** on a deployment
  that has moved to a URL, but nothing removes it automatically — the attachment
  route stays configured and simply unused until the key is unset.

---

## Review round 2 — fixes

### The absolute board path reached the URL bubble

Correct, and the invariant this report itself states was the thing being
violated. `_write_board` built its failure reason as
`f"could not be written to {path}"`, and `render._board_lines` renders
`board_problem` verbatim into the **new** URL branch. So a failed write with a
URL configured produced:

```
🗂 [Board](https://boards.example.test/job-board.html)
⚠️ Not refreshed this run (could not be written to /home/…/Areas/Work/Job Board.html) — the link may be stale.
```

**Fixed at the source, not at the renderer.** `_write_board` now returns
`"could not be written to disk — path is in the log"`, in both projects. The
path is not lost: the `log.error` immediately above it already carries the full
path, and that is where someone debugging a failed write is looking. Sanitising
inside `render` instead would have left the next reason string free to
interpolate a path again — the reason string is the leak, so the reason string
is what changed.

The old test asserted the property only on the success path, where no path was
ever going to appear. It now asserts it where a path actually arrives:
`test_a_failed_write_reports_no_path_at_all` forces `build_board` to raise
against a board under a `secret-dir/` in `tmp_path`, then checks the returned
`problem` for the full path, the parent, the marker directory name, and any `/`
at all — and re-checks the rendered bubble end to end. Present in both projects.

**Confirmed by execution**, not only by test. Forcing
`build_board` to raise `OSError(28, "No space left on device")` with
`JOB_SIFT_BOARD_URL` set and the board path pointed at a
`…/home/someone/Vault/Areas/Work/Job Board.html`-shaped location:

```
_BoardWrite.path         -> None
_BoardWrite.problem      -> 'could not be written to disk — path is in the log'

--- the Telegram bubble, verbatim ---
📋 *Job sift — 2026-09-09*
0 new · 0 open
_Processed 0 listings, 0 new._
🗂 [Board](https://boards.example.test/job-board.html)
⚠️ Not refreshed this run (could not be written to disk — path is in the log) — the link may be stale.
--- end ---

PATH FRAGMENTS IN BUBBLE: NONE
any '/' outside the URL:  no
```

The path appeared once, on the `log.error` line, which is the intended place.

### The tautological parity test

Deleted, along with its claim. `hk-events/tests/test_board_url.py` asserted
`config.BOARD_URL_ENV == "HK_EVENTS_BOARD_URL"` and `callable(config.board_url)`
under a docstring promising three parity properties — it never imported
`job_sift`, so it compared nothing. Importing `job_sift` from the hk-events
suite would mean reaching across projects on `sys.path`, which is worse than
having no test. The file's header still describes it as a deliberate mirror of
the job-sift suite, which is true and is a description, not an assertion.

### Corrections to this report

The failing-before figures, the named both-ways tests, and the Markdown
escaping disclosure have all been corrected in place above, from measurements
rather than recollection. The claimed signal-brief figure was not reproducible
and should not have been written down.

## Final counts

| Suite | Baseline | Final |
|---|---|---|
| job-sift | 694 | **720** |
| hk-events | 298 | **324** |
| signal-brief | 98 | **127** |
| bot | 76 | **76** |

## Out of scope, for the record

Real client and counterparty names — the kind this report was corrected for
carrying — are **still present in the repository's older commit objects**, in
test fixtures that were later deleted. Two client organisations and two personal
first names, across several `signal-brief` thread-rendering tests.

No currently tracked file contains them: `git grep` over the working tree is
clean and both commits on this branch are clean. But they stay reachable from
`HEAD` through history, so a clone of this public repo still carries them. That
is the same exposure the history has twice been rewritten for.

Deliberately **not** touched here. Rewriting history is not this branch's job
and must not happen as a side effect of it; it needs its own decision, and the
names are not repeated in this file for the same reason they were struck from
the paragraph above.
