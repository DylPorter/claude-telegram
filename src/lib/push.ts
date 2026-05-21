import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { Bot } from "grammy";
import { env } from "./env.js";

interface PushPayload {
  messages: string[];
  parseMode?: "Markdown" | "MarkdownV2" | "HTML";
  disablePreview?: boolean;
  delayMs?: number;
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
 *
 * Each entry in `messages` becomes a separate Telegram message (one chat bubble).
 * Long entries are auto-split at the 4000-char hard limit. Sender controls
 * logical chunking; this endpoint never merges.
 */
export function startPushServer(bot: Bot): void {
  const chatId = env.TELEGRAM_ALLOWED_USER_ID;
  const port = env.PUSH_PORT;
  const expectedSecret = env.PUSH_SECRET;

  const server = createServer(async (req, res) => {
    try {
      if (req.method === "GET" && req.url === "/healthz") {
        writeJson(res, 200, { ok: true, ts: new Date().toISOString() });
        return;
      }

      if (req.method !== "POST" || req.url !== "/push") {
        writeJson(res, 404, { error: "not found" });
        return;
      }

      const auth = req.headers["x-push-secret"];
      if (auth !== expectedSecret) {
        writeJson(res, 401, { error: "unauthorized" });
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
