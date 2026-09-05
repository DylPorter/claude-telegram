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
- **Loud config.** A malformed allowlist entry is reported rather than skipped.
  A skipped entry is a board that silently never arrives. ~~Throws at
  startup.~~ **Corrected in round 2** — throwing at startup killed the bot; it
  now degrades to an empty registry plus a 503 that names the typo.
- ~~**Nothing leaks back.** Error bodies name the configured *keys* — operator
  labels — never the paths.~~ **This was false of the 502 branch** and is
  corrected in round 2. It held for 400/404/413/422 only; the 502 piped the
  upstream error message verbatim into both the body and the journal.

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

Nothing in this path can turn a delivery failure into a quiet day.

~~The document send is deliberately **not** retried.~~ **False as shipped** —
the Python client honoured it, the bot side did not, and retried blind. See
round 2.

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


---

# Round 2 — review findings

Three Importants and four Minors. **Two of the three Importants were things the
section above asserted were already handled**, which is the part worth keeping:
the claims were written from intent rather than from the code, and the one file
that could have contradicted them had no tests. Corrections are marked inline
above rather than quietly rewritten.

## Important 1 — `sendDocument` was retried, and double-sent

`push.ts` caught *any* error from `sendDocument` and reissued it. Measured with
a first attempt failing the way a post-upload network timeout does: **two
uploads, and a 200 back to the caller** — so `push_with_board` saw success and
posted no fallback. Two boards in the chat, which is precisely the bubble-count
regression this design exists to prevent, arriving through the success path.
It was also inert in the caption-less case: `parse_mode` was already
`caption ? … : undefined`, so the "retry without parse_mode" reissued a
byte-identical request.

Fixed with a discriminator rather than the suggested gate, because the gate
still double-sends on a timeout that *has* a caption and a parse mode. The
retry now requires `err instanceof GrammyError` — which means Telegram itself
answered `ok: false`, so no message was created and a retry is provably safe —
**and** a caption **and** a parse mode to actually drop. A transport failure is
never retried at all.

    a transport failure  | attempts = 1 | HTTP 502  (caller posts its fallback)
    no caption           | attempts = 1 | HTTP 502
    GrammyError, no mode | attempts = 1 | HTTP 502  (retry would be identical)
    GrammyError + mode   | attempts = 2 | HTTP 200  parseModeDropped

## Important 2 — a typo in `PUSH_DOCUMENTS` killed the bot permanently

`parseDocumentRegistry` threw, `startPushServer` let it escape, and it runs
before `bot.start()`. The unit has `Restart=on-failure` / `RestartSec=5` with no
`StartLimit*` override, so systemd exhausts its default budget in seconds and
leaves the service `failed`. One bad character in an **optional, default-off**
feature took down chat, `/push` and every scheduled brief, and kept them down.

"Loud, not silent" was the right instinct and this was the other ditch. Added
`loadDocumentRegistry`, which catches the config error and returns
`{ registry: empty, problem }`. The parser still throws — it is pure and stays
that way — but nothing fatal reaches startup. The endpoint then answers 503
*naming the typo*, `push_with_board` puts that in the digest bubble, and the
operator finds out from the thing he already reads while still having a bot.

The config messages were also changed to identify entries by **position**
(`entry #1`) instead of echoing the raw text, since a malformed entry is usually
a mistyped path and this string now reaches the journal *and* a Telegram bubble.

## Important 3 — the 502 leaked the upstream error verbatim

`(err2 as Error).message` went into both the response body and
`console.error`, while the 4xx branch two blocks up was careful never to name a
configured path. A transport error's message wraps the request URL, and a Bot
API URL contains the **bot token** — journal, plaintext. That is the leak class
this repo's history was rewritten over twice.

Replaced with classification only, never quotation: a `GrammyError` reports
`error_code N` (Telegram's own numeric code, actionable, carries nothing else);
anything else reports `could not reach Telegram (<constructor name>)`. Confirmed
against an error carrying a real-shaped token and URL — body contains none of
`api.telegram.org`, the token, the description text, or the file path.

`/push`'s own `failed[].error` was routed through the same helper while I was
there; it had the same verbatim-message shape.

## Minors

- **(4)** `documents.ts`'s "no request field reaches it" was false — the handler
  passed the raw `board` field as the fallback key. A padded key produced
  `"  job-board  .html"`, reintroducing the exact space that slugification
  exists to remove, and a CRLF key put a header break inside grammY's unquoted
  `filename=`. Now the handler passes the trimmed key **and**
  `documentFilename` slugifies its fallback, so the comment is true by
  construction rather than by assertion. The comment itself was rewritten to say
  what actually happens.
- **(5)** The `MAX_DOCUMENT_BYTES` assertion restated a constant. `readBoardFile`
  now takes an injectable `maxBytes`, so the over-limit **branch** is tested for
  behaviour (413, with the byte count in the message), the at-limit case is
  tested, and a separate test pins that a 4 MB file is refused in under 50 ms —
  evidence that `stat` really does run before the read.
- **(6)** `push.ts` had **zero** coverage, which is why findings 1, 2 and 4 all
  lived in it. It could not be tested: it imported `env.js`, which parses
  `process.env` and loads `.env` at module load. Split into
  `push-routes.ts` (the HTTP surface, no env, structurally typed `PushBot` so a
  stub can drive it) and `push.ts` (env wiring only). 29 new tests drive the
  routes over a real 127.0.0.1 socket with an ephemeral port: 401 with a
  missing/wrong secret, auth-before-route-split, the allowlist refusals, the
  slugified filename, all four retry cases counted by attempt, the 502 body's
  contents, and `/push`'s unchanged behaviour.
- **(8)** A non-string `caption` was silently dropped and answered 200 — a lost
  bubble reported as a success, in a change whose thesis is that silence is the
  enemy. Now 400.

## Recorded, no action

The push secret is compared with a plain `!==` rather than a constant-time
compare. Pre-existing, untouched by this branch, on a loopback-only listener
with a 32-hex secret. Noted here so the next reader finds a decision rather than
an oversight.

## Verification (round 2)

| Suite | Baseline | Round 1 | Round 2 |
|---|---|---|---|
| job-sift | 658 | 694 | 694 |
| hk-events | 262 | 298 | 298 |
| signal-brief | 98 | 98 | 98 |
| bot (`npm test`) | — | 27 | 68 |

`npx tsc --noEmit` clean. Confirmed by execution:

    1. failing sendDocument      -> sendDocument attempts = 1, HTTP 502
    2. malformed PUSH_DOCUMENTS  -> no throw, registry size = 0, /push-document
                                    503 naming the typo, /push still 200
    3. 502 body                  -> "sendDocument failed: could not reach
                                    Telegram (Error)" — no host, no token, no
                                    upstream text, no path
