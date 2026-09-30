/**
 * Telegram side of the live-session bridge: routing, pinning, prompt buttons.
 *
 * Text with no pinned target is classified onto one of the Claude sessions
 * already open in tmux panes (see lib/bridge). A confident match is sent
 * straight there; otherwise the owner picks from buttons. Replying (Telegram
 * "reply") to a bridged message sends the reply to that same session.
 */

import { homedir } from "node:os";
import path from "node:path";
import { InlineKeyboard, type Context } from "grammy";
import { env } from "../lib/env.js";
import { getSession, resetSession, updateSession } from "../lib/session.js";
import {
  answerPrompt,
  deliver,
  interruptPane,
  queueDepth,
} from "../lib/bridge/bridge.js";
import {
  listLiveSessions,
  sessionLabel,
  type DiscoveryConfig,
  type LiveSession,
} from "../lib/bridge/discovery.js";
import { classify } from "../lib/bridge/router.js";
import type { PanePrompt } from "../lib/bridge/tmux.js";
import { chunk, handleText, safeReply } from "./text.js";

const ROUTE_MIN_CONFIDENCE = 0.7;

export const bridgeEnabled = env.TMUX_BRIDGE_ENABLED;

const cfg: DiscoveryConfig = {
  tmux: { bin: env.TMUX_BIN, socket: env.TMUX_SOCKET || undefined },
  claudeDir: env.CLAUDE_CONFIG_DIR || path.join(homedir(), ".claude"),
};

// Bot message id → pane, so a Telegram reply goes back to the same session.
const replyTargets = new Map<number, { paneId: string; label: string }>();
function rememberReply(messageId: number | null, paneId: string, label: string) {
  if (messageId === null) return;
  replyTargets.set(messageId, { paneId, label });
  if (replyTargets.size > 1000) replyTargets.delete(replyTargets.keys().next().value!);
}

// Messages waiting for the owner to pick a session from buttons.
const pendingRoutes = new Map<string, string>();
let routeSeq = 0;

// The last pane each chat sent to — context for the router on short follow-ups.
const lastPane = new Map<number, string>();

// In-flight bridged turns per chat, for /stop.
const active = new Map<number, Map<string, { abort: AbortController; label: string }>>();

function statusIcon(s: LiveSession): string {
  if (!s.foregroundClaude) return "🐚";
  switch (s.status) {
    case "idle":
    case "shell":
      return "🟢";
    case "waiting":
      return "⏸";
    case "busy":
      return "⏳";
    default:
      return "❔";
  }
}

function ago(ms: number | null): string {
  if (!ms) return "";
  const m = Math.round((Date.now() - ms) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h}h ago` : `${Math.round(h / 24)}d ago`;
}

function shortLabel(s: LiveSession, max = 28): string {
  const l = sessionLabel(s);
  return l.length > max ? l.slice(0, max - 1) + "…" : l;
}

// ── sending into a pane ──────────────────────────────────────────────────────

async function sendToPane(ctx: Context, paneId: string, label: string, text: string): Promise<void> {
  const chatId = ctx.chat!.id;
  lastPane.set(chatId, paneId);
  const abort = new AbortController();
  const perChat = active.get(chatId) ?? new Map();
  perChat.set(paneId, { abort, label });
  active.set(chatId, perChat);

  let first = true;
  const say = async (body: string) => {
    for (const part of chunk(first ? `→ ${label}\n\n${body}` : body)) {
      rememberReply(await safeReply(ctx, part, { markdown: true }), paneId, label);
    }
    first = false;
  };

  if (queueDepth(paneId) > 0) {
    await safeReply(ctx, `⏳ Queued behind your previous message to ${label}.`);
  } else {
    await ctx.replyWithChatAction("typing").catch(() => {});
  }
  try {
    await deliver(
      cfg,
      paneId,
      text,
      {
        onText: say,
        onNotice: async (n) => {
          rememberReply(await safeReply(ctx, `${n}\n(${label})`), paneId, label);
        },
        onPrompt: (p) => forwardPrompt(ctx, paneId, label, p),
      },
      { signal: abort.signal },
    );
  } catch (e) {
    await safeReply(ctx, `⚠️ Bridge error: ${(e as Error).message}`);
  } finally {
    const m = active.get(chatId);
    if (m?.get(paneId)?.abort === abort) m.delete(paneId);
  }
}

async function forwardPrompt(ctx: Context, paneId: string, label: string, p: PanePrompt) {
  const kb = new InlineKeyboard();
  for (const o of p.options) {
    const text = `${o.key}. ${o.label}`;
    kb.text(text.length > 40 ? text.slice(0, 39) + "…" : text, `pp:${paneId}:${p.hash}:${o.key}`).row();
  }
  const body = `🔐 ${label} is asking:\n\n${p.question.slice(0, 3000)}`;
  await ctx.reply(body, { reply_markup: kb }).catch(() => ctx.reply(body.slice(0, 4000)));
}

// ── routing ──────────────────────────────────────────────────────────────────

/** Entry point for plain text when the bridge is on. */
export async function routeText(ctx: Context, text: string): Promise<void> {
  const chatId = ctx.chat?.id;
  if (!chatId) return;

  // 1. A Telegram reply to a bridged message goes back to that session.
  const replyTo = ctx.message?.reply_to_message?.message_id;
  const replied = replyTo !== undefined ? replyTargets.get(replyTo) : undefined;
  if (replied) return sendToPane(ctx, replied.paneId, replied.label, text);

  // 2. A pinned target.
  const session = await getSession(chatId);
  if (session.pin?.kind === "bot") return handleText(ctx, text);

  const sessions = await listLiveSessions(cfg);
  const pin = session.pin;
  if (pin?.kind === "pane") {
    const pinned = sessions.find((s) => s.paneId === pin.paneId);
    if (pinned) return sendToPane(ctx, pinned.paneId, sessionLabel(pinned), text);
    await updateSession(chatId, { pin: null });
    await ctx.reply(`📌 Pinned session (${pin.label}) is gone, so I unpinned it. Routing instead.`);
  }

  // 3. Nothing live: the old `claude -p` path.
  if (sessions.length === 0) return handleText(ctx, text);

  // 4. Classify.
  await ctx.replyWithChatAction("typing").catch(() => {});
  const decision = await classify(
    { claudeBin: env.CLAUDE_BIN, model: env.ROUTER_MODEL },
    sessions,
    text,
    sessions.find((s) => s.paneId === lastPane.get(chatId))?.n,
  );
  if (decision && decision.confidence >= ROUTE_MIN_CONFIDENCE) {
    if (decision.target === "new") return handleText(ctx, text);
    const s = sessions[decision.target - 1];
    return sendToPane(ctx, s.paneId, sessionLabel(s), text);
  }

  // 5. Unsure: let the owner pick.
  const ranked: LiveSession[] = [];
  const add = (s?: LiveSession) => s && !ranked.includes(s) && ranked.push(s);
  if (decision && decision.target !== "new") add(sessions[decision.target - 1]);
  add(sessions.find((s) => s.paneId === lastPane.get(chatId)));
  for (const s of [...sessions].sort((a, b) => (b.lastActivity ?? 0) - (a.lastActivity ?? 0))) add(s);

  const id = String(++routeSeq);
  pendingRoutes.set(id, text);
  if (pendingRoutes.size > 50) pendingRoutes.delete(pendingRoutes.keys().next().value!);
  const kb = new InlineKeyboard();
  for (const s of ranked.slice(0, 3)) kb.text(`${s.n}. ${shortLabel(s)}`, `rt:${id}:${s.paneId}`).row();
  kb.text("🆕 New session", `rt:${id}:new`);
  await ctx.reply(
    decision ? "Not sure which session this is for:" : "Router didn't answer. Which session?",
    { reply_markup: kb },
  );
}

// ── commands ─────────────────────────────────────────────────────────────────

export async function handleSessions(ctx: Context): Promise<void> {
  if (!bridgeEnabled) {
    await ctx.reply("The tmux bridge is off (TMUX_BRIDGE_ENABLED=false).");
    return;
  }
  const sessions = await listLiveSessions(cfg);
  if (!sessions.length) {
    await ctx.reply("No live Claude sessions in tmux. Messages go to a fresh `claude -p` session.");
    return;
  }
  const s0 = await getSession(ctx.chat!.id);
  const pinnedPane = s0.pin?.kind === "pane" ? s0.pin.paneId : null;
  const lines = sessions.map((s) => {
    const recent = s.summary.recentPrompts.at(-1) ?? s.summary.firstPrompt ?? "";
    return (
      `${s.n}. ${statusIcon(s)} ${sessionLabel(s)}${s.paneId === pinnedPane ? " 📌" : ""}\n` +
      `    ${s.paneTarget} · ${ago(s.lastActivity)}` +
      (recent ? `\n    “${recent.slice(0, 90)}”` : "")
    );
  });
  const kb = new InlineKeyboard();
  sessions.forEach((s, i) => {
    kb.text(`📌 ${s.n}`, `pin:${s.paneId}`);
    if (i % 4 === 3) kb.row();
  });
  kb.row().text("🆕 Bot session", "pin:bot").text("Unpin", "pin:none");
  const body =
    `Live sessions (${s0.pin ? "pinned" : "routing"}):\n\n${lines.join("\n\n")}\n\n` +
    "🟢 idle · ⏳ busy · ⏸ prompt open · 🐚 not at claude prompt";
  for (const [i, part] of chunk(body).entries()) {
    await ctx.reply(part, i === 0 && chunk(body).length === 1 ? { reply_markup: kb } : {});
  }
  if (chunk(body).length > 1) await ctx.reply("Pin one:", { reply_markup: kb });
}

async function pinPane(ctx: Context, s: LiveSession): Promise<void> {
  const label = sessionLabel(s);
  await updateSession(ctx.chat!.id, { pin: { kind: "pane", paneId: s.paneId, label } });
  await ctx.reply(`📌 Pinned to ${s.n}. ${label}\nEverything goes there until /unpin.`);
}

export async function handleTo(ctx: Context, arg: string): Promise<void> {
  if (!bridgeEnabled) {
    await ctx.reply("The tmux bridge is off (TMUX_BRIDGE_ENABLED=false).");
    return;
  }
  const sessions = await listLiveSessions(cfg);
  const n = Number(arg.trim());
  const s = sessions.find((x) => x.n === n || x.paneId === arg.trim());
  if (!s) {
    await ctx.reply(`No session ${arg || "?"}. /sessions lists them.`);
    return;
  }
  await pinPane(ctx, s);
}

export async function handleUnpin(ctx: Context): Promise<void> {
  await updateSession(ctx.chat!.id, { pin: null });
  await ctx.reply(bridgeEnabled ? "Unpinned. Messages are routed to the best-matching session." : "Unpinned.");
}

/** `/new [text]`: fresh `claude -p` conversation, pinned until /unpin. */
export async function handleNew(ctx: Context, text: string): Promise<void> {
  const chatId = ctx.chat!.id;
  await resetSession(chatId);
  await updateSession(chatId, { pin: { kind: "bot" } });
  if (text) {
    void handleText(ctx, text).catch((e) => console.error("[new]", e));
  } else {
    await ctx.reply("🆕 Fresh bot session (claude -p), pinned. /unpin to go back to routing.");
  }
}

/** /stop for bridged turns: Esc into each busy pane this chat is waiting on. */
export async function stopBridged(chatId: number): Promise<string[]> {
  const m = active.get(chatId);
  if (!m?.size) return [];
  const stopped: string[] = [];
  for (const [paneId, job] of m) {
    job.abort.abort();
    if (await interruptPane(cfg, paneId).catch(() => false)) stopped.push(job.label);
  }
  m.clear();
  return stopped;
}

// ── buttons ──────────────────────────────────────────────────────────────────

export async function handleCallback(ctx: Context): Promise<void> {
  const data = ctx.callbackQuery?.data ?? "";
  const chatId = ctx.chat?.id;
  if (!chatId) return;

  if (data.startsWith("pin:")) {
    const what = data.slice(4);
    await ctx.answerCallbackQuery();
    if (what === "none") return handleUnpin(ctx);
    if (what === "bot") {
      await updateSession(chatId, { pin: { kind: "bot" } });
      await ctx.reply("📌 Pinned to the bot's own claude -p session. /new to start it fresh, /unpin to route.");
      return;
    }
    const s = (await listLiveSessions(cfg)).find((x) => x.paneId === what);
    if (!s) return void (await ctx.reply("That session is gone."));
    return pinPane(ctx, s);
  }

  if (data.startsWith("rt:")) {
    const [, id, target] = data.split(":");
    const text = pendingRoutes.get(id);
    await ctx.answerCallbackQuery();
    if (text === undefined) return void (await ctx.reply("That message expired. Send it again."));
    pendingRoutes.delete(id);
    await ctx.editMessageReplyMarkup().catch(() => {});
    if (target === "new") {
      void handleText(ctx, text).catch((e) => console.error("[route-new]", e));
      return;
    }
    const s = (await listLiveSessions(cfg)).find((x) => x.paneId === target);
    if (!s) return void (await ctx.reply("That session is gone."));
    void sendToPane(ctx, s.paneId, sessionLabel(s), text).catch((e) => console.error("[route]", e));
    return;
  }

  if (data.startsWith("pp:")) {
    const [, paneId, hash, key] = data.split(":");
    const result = await answerPrompt(cfg, paneId, hash, key).catch((e) => (e as Error).message);
    await ctx.answerCallbackQuery({ text: result === "ok" ? `Pressed ${key}` : result });
    if (result === "ok") {
      const orig = ctx.callbackQuery?.message && "text" in ctx.callbackQuery.message ? ctx.callbackQuery.message.text : "";
      await ctx.editMessageText(`${orig}\n\n→ pressed ${key}`).catch(() => {});
    }
    return;
  }

  await ctx.answerCallbackQuery();
}

export function bridgeStatusLine(pinLabel: string | null): string {
  if (!bridgeEnabled) return "• Bridge: off";
  // /status is sent as Markdown; a stray _ or * in a title would break it.
  const safe = pinLabel?.replace(/[_*`[\]]/g, " ");
  return `• Bridge: on · ${safe ? `pinned to ${safe}` : "routing"}`;
}
