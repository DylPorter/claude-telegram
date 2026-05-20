import type { Context } from "grammy";
import { env } from "../lib/env.js";
import { getSession, resetSession, updateSession } from "../lib/session.js";

export async function handleStart(ctx: Context): Promise<void> {
  await ctx.reply(
    "👋 Claude here.\n\n" +
      "Send me anything — ideas, questions, instructions to manage your vault.\n\n" +
      "Commands:\n" +
      "• /reset — start a new conversation\n" +
      "• /status — show current session + working dir\n" +
      "• /cd <path> — change working directory\n" +
      "• /vault — switch to vault dir\n" +
      "• /deep <prompt> — one turn at opus + high effort\n" +
      "• /run <abs-path> [low|med|high] <prompt> — one-off in another dir (opus, fresh session)\n",
  );
}

export async function handleReset(ctx: Context): Promise<void> {
  const chatId = ctx.chat?.id;
  if (!chatId) return;
  await resetSession(chatId);
  await ctx.reply("🔄 Fresh conversation. What's up?");
}

export async function handleStatus(ctx: Context): Promise<void> {
  const chatId = ctx.chat?.id;
  if (!chatId) return;
  const s = await getSession(chatId);
  await ctx.reply(
    `**Current state**\n` +
      `• Working dir: \`${s.cwd}\`\n` +
      `• Session: \`${s.sessionId ?? "(new)"}\`\n` +
      `• Updated: ${s.updatedAt}`,
    { parse_mode: "Markdown" },
  );
}

export async function handleCd(ctx: Context, path: string): Promise<void> {
  const chatId = ctx.chat?.id;
  if (!chatId) return;
  await updateSession(chatId, { cwd: path, sessionId: null });
  await ctx.reply(`📂 Now in \`${path}\` (new session)`, { parse_mode: "Markdown" });
}

export async function handleVault(ctx: Context): Promise<void> {
  await handleCd(ctx, env.DEFAULT_CWD);
}
