# Board delivery — the HTML boards as Telegram document attachments

`job-sift` and `hk-events` each write a self-contained HTML board to disk. The
boards are good; the filesystem they land on is not where they get read. This
branch adds a delivery path so the board arrives on the phone.

## What was added

**Bot side (TypeScript)**

- `src/lib/documents.ts` — new. The allowlist parser, the filename rule and the
  guards. Pure logic, no bot and no server, so it is testable directly.
- `src/lib/push.ts` — a second endpoint, `POST /push-document`, on the same
  127.0.0.1 listener behind the same `x-push-secret` header as `/push`. No new
  port, no new credential, no new exposure.
- `src/lib/env.ts` — `PUSH_DOCUMENTS`, optional.
- `tests/documents.test.ts` + an `npm test` script (`node --import tsx --test`).
  No new dependency: the test runner is Node's own and `tsx` was already a
  devDependency.

**Caller side (Python, both projects)**

- `telegram_client.push_document()` — POSTs a board KEY.
- `telegram_client.push_with_board()` — the delivery shape (below).
- `config.board_attach_key()` — reads `JOB_SIFT_BOARD_ATTACH` /
  `HK_EVENTS_BOARD_ATTACH` at call time. Unset means off.
- `render.summary_index()` — which entry of `render()`'s list is the summary.
- `orchestrator._deliver()` — replaces the two `push_messages(...)` call sites.

## The security question: how the path is constrained

A path in an HTTP body is an arbitrary-file-read primitive, so there is no path
in the body. The caller sends a **key**:

```json
{ "board": "job-board", "caption": "…" }
```

The key → path mapping lives only in the bot's own environment
(`PUSH_DOCUMENTS=job-board=/abs/path,…`), parsed once at process start into a
`Map`. `resolveBoardPath()` is a lookup in that map and nothing else — there is
no concatenation, no `join`, no fallback branch that could reach the
filesystem with caller-supplied text. An unknown key is a 400 before any I/O
happens, and so is a key that is itself a path (`/etc/passwd`,
`../../etc/passwd`): they are simply not in the map. There is no traversal to
sanitise because there is nothing to traverse with.

Three further constraints:

- **Default off.** `PUSH_DOCUMENTS` unset yields an empty registry, and an
  empty registry makes the endpoint answer 503 for every request. A sibling
  deployment that never configures it is unaffected by this code existing.
- **Loud config.** A malformed allowlist entry throws at startup rather than
  being skipped. A skipped entry is a board that silently never arrives.
- **Nothing leaks back.** Error bodies name the configured *keys* — operator
  labels — never the paths, which point into a personal vault.

The filename is derived server-side too, and is load-bearing twice over. It must
end `.html` because Telegram infers a document's `mime_type` from the filename
(grammY hardcodes `application/octet-stream` on the multipart part itself), and
that mime type is what makes Android hand the attachment to Chrome instead of a
file picker. It must also contain no space, because grammY writes the header as
`filename=${name}` **unquoted** — so the real basename `Job Board.html` would be
truncated at the space on the wire and lose the extension that point 1 depends
on. `documentFilename()` slugifies to `job-board.html`.

Size is checked with `stat` **before** the read: a runaway board fails in
milliseconds with a byte count in the message rather than pulling itself into
memory and timing out inside an upload. Captions over Telegram's 1024 are
refused rather than truncated, on both sides of the wire.

## One notification, and what happens when it fails

The fleet was cut from ~12 bubbles a day to ~5 on purpose, so attachment must
not add one back. It does not: with attachment on, the summary bubble is
delivered as the document's **caption** instead of as a message of its own. The
bubble count is identical either way, which `test_the_bubble_count_is_unchanged_by_attaching`
pins.

Ordering is preserved around the swap. The exempt banners (staleness alarm, drop
notice) still lead and the source-health line still follows, because a reader who
stops after the first bubble has to have seen the alarm. `summary_index()` is
computed from the same `_banners()` helper the prepend uses, so the two cannot
drift.

Degradation is the reason the shape is what it is:

| Failure | Result |
|---|---|
| `PUSH_DOCUMENTS` unset / attachment unconfigured | plain push, byte-for-byte as before |
| board not written this run | plain push; the summary already says why |
| bot down, file gone, too large, caption too long | **summary bubble still goes**, with `⚠️ Board not attached: <reason>` appended, and the rest of the run continues |
| Telegram rejects the caption's parse mode | retried once without it, matching `/push` |

Nothing in this path can turn a delivery failure into a quiet day, and the
document send is deliberately **not** retried — it is not idempotent, and a
retry after a read timeout would put the board in the chat twice.

`--dry-run` is untouched: it writes no board, so there is nothing to attach, and
`_deliver` is never reached.

## Verification

| Suite | Before | After |
|---|---|---|
| job-sift | 658 | 694 |
| hk-events | 262 | 298 |
| signal-brief | 98 | 98 |
| bot (`npm test`) | — | 27 |

`npx tsc --noEmit` clean. No network in any test: the TypeScript tests touch a
tmpdir and nothing else, and the Python tests inject fake transports rather than
patching around the suites' `no_network` guard.

The two new `*_BOARD_ATTACH` vars were added to each suite's
`sandbox_real_paths` fixture rather than worked around. They are not paths, but
they steer delivery and are read from the operator's real `.env` at import —
left set, a suite run on his machine would take the attachment branch and call
the real `push_document`, tripping `no_network` in tests that have nothing to do
with attachment.

`.gitignore` had `node_modules/`, which matches a directory but not a symlink to
one — and a scratch worktree ends up with the symlink. Tightened to
`node_modules`, since the thing it would otherwise commit into a public repo is
a link naming an absolute path under a home directory.

## Not done

The hosted-URL delivery path is a separate follow-up; it needs a credential the
operator has to create.

`telegram_client.py` is now the third near-identical copy across signal-brief,
job-sift and hk-events, and this change grew all three. The right fix is a
shared `claude_telegram_push` package. Left alone here on purpose — it is a
refactor with a blast radius across the whole fleet, not something to smuggle
into a delivery feature.
