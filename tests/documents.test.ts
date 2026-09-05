/**
 * Tests for the /push-document allowlist, filename and guards.
 *
 * Pure logic + a tmpdir. No sockets: nothing here starts a server or reaches
 * the Telegram API.
 */

import assert from "node:assert/strict";
import { mkdtemp, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test, describe } from "node:test";

import {
  DocumentError,
  DocumentConfigError,
  MAX_CAPTION_LEN,
  MAX_DOCUMENT_BYTES,
  assertCaptionFits,
  documentFilename,
  parseDocumentRegistry,
  readBoardFile,
  resolveBoardPath,
} from "../src/lib/documents.js";

/** `assert.throws` returns undefined, so capture the error ourselves. */
function caught(fn: () => unknown): DocumentError {
  try {
    fn();
  } catch (err) {
    assert.ok(err instanceof DocumentError, `expected a DocumentError, got ${err}`);
    return err;
  }
  assert.fail("expected a DocumentError, nothing was thrown");
}

async function caughtAsync(p: Promise<unknown>): Promise<DocumentError> {
  try {
    await p;
  } catch (err) {
    assert.ok(err instanceof DocumentError, `expected a DocumentError, got ${err}`);
    return err;
  }
  assert.fail("expected a DocumentError, nothing was thrown");
}

describe("parseDocumentRegistry", () => {
  test("unset means an empty registry — the feature is off by default", () => {
    assert.equal(parseDocumentRegistry(undefined).size, 0);
    assert.equal(parseDocumentRegistry("").size, 0);
    assert.equal(parseDocumentRegistry("   ").size, 0);
  });

  test("parses key=path pairs", () => {
    const reg = parseDocumentRegistry("job-board=/srv/boards/a.html, events_board=/srv/b.html");
    assert.deepEqual([...reg.entries()], [
      ["job-board", "/srv/boards/a.html"],
      ["events_board", "/srv/b.html"],
    ]);
  });

  test("keeps a path containing '=' intact (splits on the FIRST '=')", () => {
    const reg = parseDocumentRegistry("k=/srv/a=b/board.html");
    assert.equal(reg.get("k"), "/srv/a=b/board.html");
  });

  test("rejects a relative path", () => {
    assert.throws(() => parseDocumentRegistry("k=boards/a.html"), DocumentConfigError);
  });

  test("rejects a traversal-shaped relative path", () => {
    assert.throws(() => parseDocumentRegistry("k=../../etc/passwd"), DocumentConfigError);
  });

  test("rejects a malformed entry rather than skipping it", () => {
    assert.throws(() => parseDocumentRegistry("/srv/a.html"), DocumentConfigError);
    assert.throws(() => parseDocumentRegistry("=/srv/a.html"), DocumentConfigError);
  });

  test("rejects a key with path characters in it", () => {
    assert.throws(() => parseDocumentRegistry("../k=/srv/a.html"), DocumentConfigError);
    assert.throws(() => parseDocumentRegistry("A B=/srv/a.html"), DocumentConfigError);
  });

  test("rejects a duplicated key", () => {
    assert.throws(() => parseDocumentRegistry("k=/srv/a.html,k=/srv/b.html"), DocumentConfigError);
  });

  test("rejects a NUL byte in a path", () => {
    assert.throws(() => parseDocumentRegistry("k=/srv/a\0.html"), DocumentConfigError);
  });
});

describe("resolveBoardPath", () => {
  const reg = parseDocumentRegistry("job-board=/srv/boards/job.html");

  test("returns the CONFIGURED path for a known key", () => {
    assert.equal(resolveBoardPath(reg, "job-board"), "/srv/boards/job.html");
  });

  test("an empty registry refuses with 503, not a lookup", () => {
    assert.equal(caught(() => resolveBoardPath(new Map(), "job-board")).status, 503);
  });

  test("an unknown key is a 400 — no fallthrough to the filesystem", () => {
    assert.equal(caught(() => resolveBoardPath(reg, "nope")).status, 400);
  });

  test("a caller-supplied PATH is not a key and is refused", () => {
    for (const attempt of [
      "/etc/passwd",
      "../../etc/passwd",
      "/srv/boards/job.html",
      "job-board/../../../etc/passwd",
    ]) {
      assert.equal(caught(() => resolveBoardPath(reg, attempt)).status, 400, `${attempt} should be refused`);
    }
  });

  test("a non-string board is refused", () => {
    for (const attempt of [undefined, null, 42, {}, ["job-board"]]) {
      assert.throws(() => resolveBoardPath(reg, attempt), DocumentError);
    }
  });

  test("the error names the configured KEYS but never the paths", () => {
    const err = caught(() => resolveBoardPath(reg, "nope"));
    assert.match(err.message, /job-board/);
    assert.doesNotMatch(err.message, /\/srv/);
  });
});

describe("documentFilename", () => {
  test("always ends .html so Telegram reports a text/html mime type", () => {
    assert.equal(documentFilename("/srv/boards/board", "k"), "board.html");
    assert.equal(documentFilename("/srv/boards/board.html", "k"), "board.html");
    assert.equal(documentFilename("/srv/boards/board.HTML", "k"), "board.html");
    assert.equal(documentFilename("/srv/boards/board.htm", "k"), "board.html");
  });

  test("a space in the basename is slugified away", () => {
    // grammY writes `filename=${name}` UNQUOTED, so a space would truncate the
    // name on the wire and cost it the .html extension.
    assert.equal(documentFilename("/srv/Areas/Work/Job Board.html", "k"), "job-board.html");
  });

  test("no whitespace or quoting characters survive", () => {
    const name = documentFilename('/srv/a b"c\'d;e.html', "k");
    assert.doesNotMatch(name, /[\s"';\\]/);
    assert.match(name, /\.html$/);
  });

  test("falls back to the key when the basename slugifies to nothing", () => {
    assert.equal(documentFilename("/srv/boards/###.html", "job-board"), "job-board.html");
  });

  test("carries no directory component", () => {
    assert.doesNotMatch(documentFilename("/srv/deep/nested/board.html", "k"), /\//);
  });
});

describe("assertCaptionFits", () => {
  test("accepts a caption at the limit", () => {
    assertCaptionFits("x".repeat(MAX_CAPTION_LEN));
  });

  test("rejects one over it with 422 rather than truncating", () => {
    assert.equal(caught(() => assertCaptionFits("x".repeat(MAX_CAPTION_LEN + 1))).status, 422);
  });
});

describe("readBoardFile", () => {
  test("reads a real file", async () => {
    const dir = await mkdtemp(join(tmpdir(), "push-doc-"));
    const path = join(dir, "board.html");
    await writeFile(path, "<html>board</html>");
    const buf = await readBoardFile(path);
    assert.equal(buf.toString(), "<html>board</html>");
  });

  test("a missing file is 404, not a hang", async () => {
    const dir = await mkdtemp(join(tmpdir(), "push-doc-"));
    assert.equal((await caughtAsync(readBoardFile(join(dir, "gone.html")))).status, 404);
  });

  test("a directory is 404", async () => {
    const dir = await mkdtemp(join(tmpdir(), "push-doc-"));
    const sub = join(dir, "sub");
    await mkdir(sub);
    assert.equal((await caughtAsync(readBoardFile(sub))).status, 404);
  });

  test("an empty file is refused — a zero-byte board is not a board", async () => {
    const dir = await mkdtemp(join(tmpdir(), "push-doc-"));
    const path = join(dir, "board.html");
    await writeFile(path, "");
    assert.equal((await caughtAsync(readBoardFile(path))).status, 422);
  });

  test("the size guard is stated in bytes and matches Telegram's limit", () => {
    assert.equal(MAX_DOCUMENT_BYTES, 50 * 1024 * 1024);
  });
});
