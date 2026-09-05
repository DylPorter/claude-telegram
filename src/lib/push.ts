import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { InputFile, type Bot } from "grammy";
import { env } from "./env.js";
import {
  DocumentError,
  documentFilename,
  parseDocumentRegistry,
  readBoardFile,
  resolveBoardPath,
  assertCaptionFits,
} from "./documents.js";

interface PushPayload {
  messages: string[];
  parseMode?: "Markdown" | "MarkdownV2" | "HTML";
  disablePreview?: boolean;
  delayMs?: number;
}

interface PushDocumentPayload {
  /** A KEY from PUSH_DOCUMENTS. Never a path — see lib/documents.ts. */
  board: string;
  caption?: string;
  parseMode?: "Markdown" | "MarkdownV2" | "HTML";
}

const MAX_TELEGRAM_LEN = 4000;

function splitForTelegram(text: string, size = MAX_TELEGRAM_LEN): string[] {
  if (text.length <= size) return [text];
  const parts: string[] = [];
  let remaining = text;
  while (remaining.length > size) {
    let split = remaining.lastIndexOf("\n", size);
    if (split < size / 2) split = size;
    parts.push(remaining.slice(0, split));
    remaining = remaining.slice(split).trimStart();
  }
  if (remaining) parts.push(remaining);
  return parts;
}

async function readJsonBody(req: IncomingMessage, maxBytes = 256 * 1024): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let received = 0;
    req.on("data", (c: Buffer) => {
      received += c.length;
      if (received > maxBytes) {
        req.destroy();
        reject(new Error("payload too large"));
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      try {
        const buf = Buffer.concat(chunks).toString("utf8");
        resolve(buf ? JSON.parse(buf) : {});
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function writeJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

/**
 * Start an HTTP server on localhost that lets external processes (signal-brief
 * orchestrators, scheduled scripts) push messages to the bot's allowed user.
 *
 * Security: bound to 127.0.0.1 only + shared-secret header. No external exposure.
 *
 * Endpoints:
 *   GET  /healthz                 — liveness probe
 *   POST /push                    — { messages: string[], parseMode?, disablePreview?, delayMs? }
 *   POST /push-document           — { board: <PUSH_DOCUMENTS key>, caption?, parseMode? }
 *
 * `/push-document` sends ONE configured file as a Telegram document. The caller
 * names a key, not a path, so the endpoint cannot read arbitrary files; the
 * allowlist lives in this process's env and is empty (endpoint disabled) unless
 * the operator sets PUSH_DOCUMENTS. Same 127.0.0.1 bind and same shared secret
 * as /push — no new exposure.
 *
 * Each entry in `messages` becomes a separate Telegram message (one chat bubble).
 * Long entries are auto-split at the 4000-char hard limit. Sender controls
 * logical chunking; this endpoint never merges.
 */
/**
 * POST /push-document — send one allowlisted file as a Telegram document.
 *
 * The caption carries the run's summary, which is what keeps this to ONE
 * notification: the caller sends its summary bubble AS the caption instead of
 * as a separate message, rather than sending both.
 *
 * Auth and the localhost bind are already enforced by the caller.
 */
async function handlePushDocument(
  bot: Bot,
  chatId: number,
  documents: Map<string, string>,
  req: IncomingMessage,
  res: ServerResponse,
): Promise<void> {
  const body = (await readJsonBody(req)) as Partial<PushDocumentPayload>;

  let path: string;
  let filename: string;
  let caption: string | undefined;
  let file: Buffer;
  try {
    path = resolveBoardPath(documents, body?.board);
    filename = documentFilename(path, String(body?.board ?? "board"));
    caption = typeof body?.caption === "string" ? body.caption : undefined;
    if (caption !== undefined) assertCaptionFits(caption);
    file = await readBoardFile(path);
  } catch (err) {
    if (err instanceof DocumentError) {
      // Logged WITHOUT the path: the configured location is the operator's
      // vault, and this line goes to the journal.
      console.error(`[push] document rejected (${err.status}): ${err.message}`);
      writeJson(res, err.status, { error: err.message });
      return;
    }
    throw err;
  }

  try {
    const msg = await bot.api.sendDocument(chatId, new InputFile(file, filename), {
      caption,
      parse_mode: caption ? body?.parseMode : undefined,
    });
    writeJson(res, 200, {
      sent: [msg.message_id],
      board: body?.board,
      filename,
      bytes: file.byteLength,
    });
  } catch (err) {
    // Retry once without parse_mode, matching /push: a caption that Telegram
    // will not parse must not cost the operator the whole attachment.
    try {
      const msg = await bot.api.sendDocument(chatId, new InputFile(file, filename), { caption });
      writeJson(res, 200, {
        sent: [msg.message_id],
        board: body?.board,
        filename,
        bytes: file.byteLength,
        parseModeDropped: true,
      });
    } catch (err2) {
      const message = (err2 as Error).message;
      console.error(`[push] sendDocument failed for ${filename}: ${message}`);
      // 502, not 500: the caller distinguishes "Telegram refused it" from "your
      // request was wrong", and falls back to a plain text bubble either way.
      writeJson(res, 502, { error: `sendDocument failed: ${message}` });
    }
  }
}

export function startPushServer(bot: Bot): void {
  const chatId = env.TELEGRAM_ALLOWED_USER_ID;
  const port = env.PUSH_PORT;
  const expectedSecret = env.PUSH_SECRET;

  // Parsed once, at start: a malformed allowlist should stop the process with a
  // message, not fail one board a day at 07:00 where nobody is reading logs.
  const documents = parseDocumentRegistry(env.PUSH_DOCUMENTS);

  const server = createServer(async (req, res) => {
    try {
      if (req.method === "GET" && req.url === "/healthz") {
        writeJson(res, 200, { ok: true, ts: new Date().toISOString() });
        return;
      }

      if (req.method !== "POST" || (req.url !== "/push" && req.url !== "/push-document")) {
        writeJson(res, 404, { error: "not found" });
        return;
      }

      const auth = req.headers["x-push-secret"];
      if (auth !== expectedSecret) {
        writeJson(res, 401, { error: "unauthorized" });
        return;
      }

      if (req.url === "/push-document") {
        await handlePushDocument(bot, chatId, documents, req, res);
        return;
      }

      const body = (await readJsonBody(req)) as Partial<PushPayload>;
      if (!body || !Array.isArray(body.messages) || body.messages.length === 0) {
        writeJson(res, 400, { error: "messages[] required" });
        return;
      }

      const parseMode = body.parseMode;
      const disablePreview = body.disablePreview !== false;
      const delayMs = Math.max(0, Math.min(5000, body.delayMs ?? 350));

      const sent: number[] = [];
      const failed: { index: number; error: string }[] = [];

      for (let i = 0; i < body.messages.length; i++) {
        const text = String(body.messages[i] ?? "").trim();
        if (!text) continue;
        for (const part of splitForTelegram(text)) {
          try {
            const msg = await bot.api.sendMessage(chatId, part, {
              parse_mode: parseMode,
              link_preview_options: { is_disabled: disablePreview },
            });
            sent.push(msg.message_id);
          } catch (err) {
            // Retry once without parse_mode in case Markdown failed.
            try {
              const msg = await bot.api.sendMessage(chatId, part, {
                link_preview_options: { is_disabled: disablePreview },
              });
              sent.push(msg.message_id);
            } catch (err2) {
              failed.push({ index: i, error: (err2 as Error).message });
            }
          }
          if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
        }
      }

      writeJson(res, 200, { sent, failed });
    } catch (err) {
      console.error("[push] error", err);
      writeJson(res, 500, { error: (err as Error).message });
    }
  });

  server.listen(port, "127.0.0.1", () => {
    console.log(`[push] listening on 127.0.0.1:${port}`);
  });
}
