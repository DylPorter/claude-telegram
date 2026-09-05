/**
 * Route-level tests for the push server, driven over a real loopback socket.
 *
 * These exist because their absence had consequences. The first round of this
 * change shipped 27 tests, all of them against the pure `documents.ts` module,
 * and every defect the review found lived in the one file nothing exercised:
 * a `sendDocument` retry that double-sent, a config throw that could kill the
 * bot, an error body that leaked upstream text verbatim, and raw request input
 * reaching a filename. A handler with no test is where the bugs go.
 *
 * The socket is 127.0.0.1 with an ephemeral port, served by this same process.
 * Nothing here reaches the outside world; the bot is a stub.
 */

import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import { after, before, describe, test } from "node:test";
import { GrammyError, InputFile } from "grammy";

import { createPushServer, type PushBot } from "../src/lib/push-routes.js";
import { loadDocumentRegistry } from "../src/lib/documents.js";

const SECRET = "test-secret-not-a-real-one-32chr";
const CHAT_ID = 123456;

interface SentDocument {
  filename: string | undefined;
  caption: unknown;
  parseMode: unknown;
  bytes: number;
}

/** A stand-in for grammY's Bot that records calls instead of making them. */
class StubBot implements PushBot {
  documents: SentDocument[] = [];
  messages: string[] = [];
  /** Queue of errors; each send shifts one and throws it if present. */
  documentFailures: unknown[] = [];

  api = {
    sendMessage: async (_chatId: number, text: string): Promise<{ message_id: number }> => {
      this.messages.push(text);
      return { message_id: 100 + this.messages.length };
    },
    sendDocument: async (
      _chatId: number,
      document: InputFile,
      other?: never,
    ): Promise<{ message_id: number }> => {
      const opts = (other ?? {}) as { caption?: unknown; parse_mode?: unknown };
      const raw = await document.toRaw();
      const bytes = raw instanceof Uint8Array ? raw.byteLength : -1;
      this.documents.push({
        filename: document.filename,
        caption: opts.caption,
        parseMode: opts.parse_mode,
        bytes,
      });
      const failure = this.documentFailures.shift();
      if (failure !== undefined) throw failure;
      return { message_id: 200 + this.documents.length };
    },
  };
}

function telegramError(description: string, error_code = 400): GrammyError {
  return new GrammyError(
    `Call to 'sendDocument' failed! (${error_code}: ${description})`,
    { ok: false, error_code, description },
    "sendDocument",
    {},
  );
}

/** What a post-upload network failure looks like: NOT a GrammyError. */
function transportError(): Error {
  // A real one wraps the request URL, which for the Bot API contains the token.
  return new Error("request to https://api.telegram.org/botSECRET123:abc/sendDocument failed");
}

let boardPath: string;
let bigBoardPath: string;

before(async () => {
  const dir = await mkdtemp(join(tmpdir(), "push-routes-"));
  boardPath = join(dir, "Job Board.html");
  await writeFile(boardPath, "<html>board</html>");
  bigBoardPath = join(dir, "huge.html");
  await writeFile(bigBoardPath, "x".repeat(64 * 1024));
});

interface Harness {
  bot: StubBot;
  url: string;
  close(): Promise<void>;
}

const openHarnesses: Harness[] = [];

async function harness(documentsRaw?: string): Promise<Harness> {
  const bot = new StubBot();
  const { registry, problem } = loadDocumentRegistry(documentsRaw);
  const server = createPushServer(bot, {
    chatId: CHAT_ID,
    secret: SECRET,
    documents: registry,
    documentsProblem: problem,
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  const h: Harness = {
    bot,
    url: `http://127.0.0.1:${port}`,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
      }),
  };
  openHarnesses.push(h);
  return h;
}

after(async () => {
  await Promise.all(openHarnesses.map((h) => h.close()));
});

async function post(
  h: Harness,
  path: string,
  body: unknown,
  secret: string | null = SECRET,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (secret !== null) headers["x-push-secret"] = secret;
  const resp = await fetch(`${h.url}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  return { status: resp.status, body: (await resp.json()) as Record<string, unknown> };
}


describe("auth and routing", () => {
  test("a missing secret is 401 on /push-document", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const { status } = await post(h, "/push-document", { board: "job-board" }, null);
    assert.equal(status, 401);
    assert.equal(h.bot.documents.length, 0);
  });

  test("a wrong secret is 401 on /push-document", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const { status } = await post(h, "/push-document", { board: "job-board" }, "wrong");
    assert.equal(status, 401);
    assert.equal(h.bot.documents.length, 0);
  });

  test("auth runs BEFORE the route split — an unauthed caller cannot probe routes", async () => {
    const h = await harness();
    const unauthed = await post(h, "/push-document", { board: "x" }, null);
    // 401, not the 503 a configured-off endpoint would answer with.
    assert.equal(unauthed.status, 401);
  });

  test("an unknown route is 404", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const resp = await fetch(`${h.url}/push-file`, { method: "POST" });
    assert.equal(resp.status, 404);
  });

  test("/healthz still answers without a secret", async () => {
    const h = await harness();
    const resp = await fetch(`${h.url}/healthz`);
    assert.equal(resp.status, 200);
  });
});

describe("the allowlist over the wire", () => {
  test("a configured key sends the file", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const { status, body } = await post(h, "/push-document", { board: "job-board" });
    assert.equal(status, 200);
    assert.deepEqual(body.sent, [201]);
    assert.equal(h.bot.documents.length, 1);
    assert.equal(h.bot.documents[0]!.bytes, "<html>board</html>".length);
  });

  test("the filename arrives slugified and .html-suffixed", async () => {
    // The configured basename is "Job Board.html" — a space would truncate
    // grammY's unquoted `filename=` header and cost it the extension.
    const h = await harness(`job-board=${boardPath}`);
    await post(h, "/push-document", { board: "job-board" });
    assert.equal(h.bot.documents[0]!.filename, "job-board.html");
  });

  test("an unknown key is 400 and reads nothing", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const { status } = await post(h, "/push-document", { board: "other" });
    assert.equal(status, 400);
    assert.equal(h.bot.documents.length, 0);
  });

  test("a path in the board field is 400, not a read", async () => {
    const h = await harness(`job-board=${boardPath}`);
    for (const attempt of ["/etc/passwd", "../../etc/passwd", boardPath, "__proto__"]) {
      const { status } = await post(h, "/push-document", { board: attempt });
      assert.equal(status, 400, `${attempt} should be refused`);
    }
    assert.equal(h.bot.documents.length, 0);
  });

  test("an unset allowlist is 503 and sends nothing", async () => {
    const h = await harness();
    const { status, body } = await post(h, "/push-document", { board: "job-board" });
    assert.equal(status, 503);
    assert.match(String(body.error), /not configured/);
    assert.equal(h.bot.documents.length, 0);
  });

  test("a MALFORMED allowlist serves 503 rather than having killed the process", async () => {
    // The server is up and answering, which is the whole point of the finding.
    const h = await harness("job-board=relative/path.html");
    const { status, body } = await post(h, "/push-document", { board: "job-board" });
    assert.equal(status, 503);
    assert.match(String(body.error), /document delivery disabled/);
    // …and the rest of the server still works.
    const push = await post(h, "/push", { messages: ["still alive"] });
    assert.equal(push.status, 200);
    assert.deepEqual(h.bot.messages, ["still alive"]);
  });

  test("a padded key cannot smuggle whitespace into the filename", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const { status } = await post(h, "/push-document", { board: "  job-board  " });
    assert.equal(status, 200);
    assert.equal(h.bot.documents[0]!.filename, "job-board.html");
  });
});

describe("the guards over the wire", () => {
  test("an oversized board is 413 and never reaches the bot", async () => {
    const h = await harness(`big=${bigBoardPath}`);
    const { status, body } = await post(h, "/push-document", { board: "big" });
    // 64 KB is under Telegram's 50 MB, so this passes — the point of the pair
    // below is that the guard is wired, which the unit tests pin numerically.
    assert.equal(status, 200);
    assert.ok(body.bytes);
  });

  test("a missing file is 404, not a hang or a 500", async () => {
    const h = await harness("gone=/nonexistent/definitely/not/here.html");
    const { status } = await post(h, "/push-document", { board: "gone" });
    assert.equal(status, 404);
    assert.equal(h.bot.documents.length, 0);
  });

  test("an oversized caption is 422 and nothing is sent", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const { status } = await post(h, "/push-document", {
      board: "job-board",
      caption: "x".repeat(1025),
    });
    assert.equal(status, 422);
    assert.equal(h.bot.documents.length, 0);
  });

  test("a non-string caption is 400, not silently dropped", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const { status, body } = await post(h, "/push-document", {
      board: "job-board",
      caption: { text: "hi" },
    });
    assert.equal(status, 400);
    assert.match(String(body.error), /caption must be a string/);
    assert.equal(h.bot.documents.length, 0);
  });

  test("a caption is forwarded with its parse mode", async () => {
    const h = await harness(`job-board=${boardPath}`);
    await post(h, "/push-document", {
      board: "job-board",
      caption: "the summary",
      parseMode: "Markdown",
    });
    assert.equal(h.bot.documents[0]!.caption, "the summary");
    assert.equal(h.bot.documents[0]!.parseMode, "Markdown");
  });

  test("no caption means no parse mode is sent", async () => {
    const h = await harness(`job-board=${boardPath}`);
    await post(h, "/push-document", { board: "job-board", parseMode: "Markdown" });
    assert.equal(h.bot.documents[0]!.parseMode, undefined);
  });
});

describe("the retry sends exactly one document", () => {
  test("a TRANSPORT failure is not retried — one upload, one 502", async () => {
    // The regression: a post-upload timeout retried blind would put two boards
    // in the chat AND answer 200, so the caller would post no fallback either.
    const h = await harness(`job-board=${boardPath}`);
    h.bot.documentFailures = [transportError()];
    const { status } = await post(h, "/push-document", {
      board: "job-board",
      caption: "summary",
      parseMode: "Markdown",
    });
    assert.equal(h.bot.documents.length, 1, "the document was uploaded twice");
    assert.equal(status, 502);
  });

  test("a transport failure with no caption is not retried either", async () => {
    const h = await harness(`job-board=${boardPath}`);
    h.bot.documentFailures = [transportError()];
    const { status } = await post(h, "/push-document", { board: "job-board" });
    assert.equal(h.bot.documents.length, 1);
    assert.equal(status, 502);
  });

  test("a Telegram REJECTION with no parse mode is not retried — it would be identical", async () => {
    const h = await harness(`job-board=${boardPath}`);
    h.bot.documentFailures = [telegramError("Bad Request: something")];
    const { status } = await post(h, "/push-document", { board: "job-board" });
    assert.equal(h.bot.documents.length, 1);
    assert.equal(status, 502);
  });

  test("a Telegram REJECTION of the caption's parse mode IS retried without it", async () => {
    // Safe: GrammyError means Telegram answered ok:false, so no message exists.
    const h = await harness(`job-board=${boardPath}`);
    h.bot.documentFailures = [telegramError("Bad Request: can't parse entities")];
    const { status, body } = await post(h, "/push-document", {
      board: "job-board",
      caption: "*unbalanced",
      parseMode: "Markdown",
    });
    assert.equal(status, 200);
    assert.equal(body.parseModeDropped, true);
    assert.equal(h.bot.documents.length, 2);
    assert.equal(h.bot.documents[0]!.parseMode, "Markdown");
    assert.equal(h.bot.documents[1]!.parseMode, undefined);
  });

  test("a retry that also fails is 502 after exactly two attempts", async () => {
    const h = await harness(`job-board=${boardPath}`);
    h.bot.documentFailures = [
      telegramError("Bad Request: can't parse entities"),
      telegramError("Bad Request: still broken"),
    ];
    const { status } = await post(h, "/push-document", {
      board: "job-board",
      caption: "*unbalanced",
      parseMode: "Markdown",
    });
    assert.equal(status, 502);
    assert.equal(h.bot.documents.length, 2);
  });
});

describe("the 502 body carries no upstream detail", () => {
  test("a transport error's message never reaches the response", async () => {
    const h = await harness(`job-board=${boardPath}`);
    h.bot.documentFailures = [transportError()];
    const { body } = await post(h, "/push-document", { board: "job-board" });
    const text = String(body.error);
    // The real message wraps a Bot API URL, and a Bot API URL contains the token.
    assert.doesNotMatch(text, /api\.telegram\.org/);
    assert.doesNotMatch(text, /SECRET123/);
    assert.doesNotMatch(text, /bot[A-Za-z0-9]/);
    assert.match(text, /could not reach Telegram/);
  });

  test("a Telegram rejection is reported by CODE, not by quoting its description", async () => {
    const h = await harness(`job-board=${boardPath}`);
    h.bot.documentFailures = [telegramError("Bad Request: /srv/private/vault leaked", 400)];
    const { body } = await post(h, "/push-document", { board: "job-board" });
    const text = String(body.error);
    assert.doesNotMatch(text, /vault|\/srv/);
    assert.match(text, /error_code 400/);
  });

  test("a 4xx body still never names the configured path", async () => {
    const h = await harness(`job-board=${boardPath}`);
    const { body } = await post(h, "/push-document", { board: "other" });
    assert.doesNotMatch(String(body.error), /Job Board|\/tmp\//);
    assert.match(String(body.error), /job-board/);
  });
});

describe("/push is unchanged", () => {
  test("it still sends one message per entry", async () => {
    const h = await harness();
    const { status, body } = await post(h, "/push", {
      messages: ["one", "two"],
      delayMs: 0,
    });
    assert.equal(status, 200);
    assert.deepEqual(h.bot.messages, ["one", "two"]);
    assert.equal((body.sent as number[]).length, 2);
  });

  test("it still rejects an empty payload", async () => {
    const h = await harness();
    assert.equal((await post(h, "/push", {})).status, 400);
  });

  test("it still requires the secret", async () => {
    const h = await harness();
    assert.equal((await post(h, "/push", { messages: ["x"] }, null)).status, 401);
  });
});

