/**
 * Document delivery for /push — the allowlist, the filename, and the guards.
 *
 * Pure logic, deliberately separated from the HTTP handler so it can be tested
 * without standing up a server or a bot.
 *
 * The security shape: a caller names a KEY, never a path. Paths live only in
 * this process's environment (`PUSH_DOCUMENTS`), so the endpoint cannot be
 * turned into an arbitrary-file-read primitive by anything in the request
 * body. There is no traversal to sanitise because there is no caller-supplied
 * path component to traverse with.
 *
 * Unset `PUSH_DOCUMENTS` means the whole feature is OFF and the endpoint
 * refuses. That is the default, so a sibling deployment that never configures
 * it is unaffected by this file existing.
 */

import { basename } from "node:path";
import { readFile, stat } from "node:fs/promises";

/** Telegram's hard ceiling for a document uploaded by a bot. */
export const MAX_DOCUMENT_BYTES = 50 * 1024 * 1024;

/** Telegram's ceiling for a media caption. */
export const MAX_CAPTION_LEN = 1024;

/** Board keys are operator-chosen identifiers, not paths. */
const KEY_RE = /^[a-z0-9][a-z0-9_-]*$/;

/**
 * An error that carries the HTTP status the handler should answer with, so the
 * caller can tell "you asked for a board I do not serve" apart from "the file
 * is gone" apart from "the file is absurdly large". A single 500 for all three
 * is how a delivery failure turns into a quiet day.
 */
export class DocumentError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "DocumentError";
  }
}

/** Raised at startup, not per-request: a malformed allowlist is a config bug. */
export class DocumentConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DocumentConfigError";
  }
}

/**
 * Parse `PUSH_DOCUMENTS` — a comma-separated list of `key=/absolute/path`.
 *
 * Undefined/blank yields an EMPTY registry, which the handler treats as
 * "document delivery not configured" and refuses. That is the default-off
 * requirement: nobody has to edit code, or even know this exists, to be
 * unaffected by it.
 *
 * Malformed entries throw rather than being skipped. A skipped entry is a
 * board that silently never arrives — the exact failure mode this codebase
 * keeps deleting — and the only moment it can be reported to a human is
 * process start.
 *
 * Paths containing a comma are not expressible. That is a documented
 * limitation, not an oversight: the alternative (JSON in a .env) buys one
 * pathological filename at the cost of every hand-edit.
 */
export function parseDocumentRegistry(raw: string | undefined): Map<string, string> {
  const registry = new Map<string, string>();
  const text = (raw ?? "").trim();
  if (!text) return registry;

  for (const rawEntry of text.split(",")) {
    const entry = rawEntry.trim();
    if (!entry) continue;

    const eq = entry.indexOf("=");
    if (eq <= 0) {
      throw new DocumentConfigError(
        `PUSH_DOCUMENTS entry ${JSON.stringify(entry)} is not "key=/absolute/path"`,
      );
    }

    const key = entry.slice(0, eq).trim();
    const path = entry.slice(eq + 1).trim();

    if (!KEY_RE.test(key)) {
      throw new DocumentConfigError(
        `PUSH_DOCUMENTS key ${JSON.stringify(key)} must match ${KEY_RE.source}`,
      );
    }
    if (!path.startsWith("/")) {
      throw new DocumentConfigError(
        `PUSH_DOCUMENTS path for ${JSON.stringify(key)} must be absolute`,
      );
    }
    if (path.includes("\0")) {
      throw new DocumentConfigError(
        `PUSH_DOCUMENTS path for ${JSON.stringify(key)} contains a NUL byte`,
      );
    }
    if (registry.has(key)) {
      throw new DocumentConfigError(`PUSH_DOCUMENTS key ${JSON.stringify(key)} is duplicated`);
    }

    registry.set(key, path);
  }

  return registry;
}

/** The configured path for a key, or a DocumentError the handler can answer with. */
export function resolveBoardPath(registry: Map<string, string>, board: unknown): string {
  if (registry.size === 0) {
    throw new DocumentError(503, "document delivery not configured (PUSH_DOCUMENTS unset)");
  }
  if (typeof board !== "string" || !board.trim()) {
    throw new DocumentError(400, "board (string) required");
  }
  const path = registry.get(board.trim());
  if (path === undefined) {
    // The KEYS are echoed (they are operator-chosen labels and this is a
    // localhost-only, secret-gated endpoint); the PATHS never are.
    throw new DocumentError(
      400,
      `unknown board — configured: ${[...registry.keys()].join(", ")}`,
    );
  }
  return path;
}

/**
 * The name the file arrives under in Telegram.
 *
 * Two things are load-bearing:
 *
 * 1. It must end `.html`. Telegram derives a document's `mime_type` from the
 *    filename (grammY hardcodes `application/octet-stream` on the multipart
 *    part itself — see grammy/out/core/payload.js), and the Android client
 *    hands the attachment to Chrome based on that mime type. A board that
 *    arrives extensionless is a board that opens in a file picker.
 *
 * 2. It must contain no space or quote. grammY writes the header as
 *    `filename=${filename}` — UNQUOTED — so the real board basename
 *    "Job Board.html" would be split at the space by a strict parser and lose
 *    its extension, defeating (1). Slugifying is what makes the extension
 *    survive the wire, not cosmetics.
 *
 * Derived entirely from server-side config; no request field reaches it.
 */
export function documentFilename(path: string, key: string): string {
  const stem = basename(path).replace(/\.html?$/i, "");
  const slug = stem
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "");
  return `${slug || key}.html`;
}

/** Caption guard. Telegram rejects >1024; we would rather not truncate silently. */
export function assertCaptionFits(caption: string): void {
  if (caption.length > MAX_CAPTION_LEN) {
    throw new DocumentError(
      422,
      `caption is ${caption.length} chars, over Telegram's ${MAX_CAPTION_LEN} limit`,
    );
  }
}

/**
 * Read a configured board off disk, with the size guard applied BEFORE the read.
 *
 * `stat` first is the whole point: a runaway board must fail in milliseconds
 * with a number in the message, not by pulling itself into memory and then
 * timing out somewhere inside an upload.
 */
export async function readBoardFile(path: string): Promise<Buffer> {
  let info;
  try {
    info = await stat(path);
  } catch {
    throw new DocumentError(404, "board file not found");
  }
  if (!info.isFile()) {
    throw new DocumentError(404, "board path is not a regular file");
  }
  if (info.size === 0) {
    throw new DocumentError(422, "board file is empty");
  }
  if (info.size > MAX_DOCUMENT_BYTES) {
    throw new DocumentError(
      413,
      `board is ${info.size} bytes, over Telegram's ${MAX_DOCUMENT_BYTES}-byte document limit`,
    );
  }
  return readFile(path);
}
