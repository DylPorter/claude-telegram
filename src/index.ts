import { Bot } from "grammy";
import { env } from "./lib/env.js";
import {
  handleCd,
  handleReset,
  handleStart,
  handleStatus,
  handleVault,
} from "./handlers/commands.js";
import { handlePhoto } from "./handlers/photo.js";
import { handleText } from "./handlers/text.js";
import { handleVoice } from "./handlers/voice.js";

const bot = new Bot(env.TELEGRAM_BOT_TOKEN);

// Auth: only the allowed user may interact
bot.use(async (ctx, next) => {
  const userId = ctx.from?.id;
  if (userId !== env.TELEGRAM_ALLOWED_USER_ID) {
    console.warn(`[auth] rejected user ${userId}`);
    return;
  }
  await next();
});

// Error handler
bot.catch((err) => {
  console.error("[bot error]", err);
});

// Commands
bot.command("start", handleStart);
bot.command("reset", handleReset);
bot.command("status", handleStatus);
bot.command("vault", handleVault);
bot.command("cd", async (ctx) => {
  const path = ctx.match?.trim();
  if (!path) {
    await ctx.reply("Usage: /cd <absolute-path>");
    return;
  }
  await handleCd(ctx, path);
});

// One-shot heavy-mode override: this turn runs at opus + high effort.
bot.command("deep", async (ctx) => {
  const prompt = ctx.match?.trim();
  if (!prompt) {
    await ctx.reply("Usage: /deep <prompt>  (one turn at opus + high effort)");
    return;
  }
  await handleText(ctx, prompt, { model: "opus", effort: "high" });
});

// One-shot run in a different directory at opus + chosen effort, fresh session.
// Usage: /run <abs-path> [low|medium|high|xhigh|max] <prompt>
// Effort defaults to "high"; if omitted, the second token is treated as the start of the prompt.
bot.command("run", async (ctx) => {
  const raw = ctx.match?.trim();
  if (!raw) {
    await ctx.reply(
      "Usage: /run <abs-path> [low|medium|high|xhigh|max] <prompt>\n" +
        "Example: /run /home/tdporter/Documents/Programming/sourcinggpt-v1-app medium fix the embedding pipeline error class",
    );
    return;
  }
  const tokens = raw.split(/\s+/);
  const cwd = tokens.shift();
  if (!cwd || !cwd.startsWith("/")) {
    await ctx.reply("⚠️ First arg must be an absolute path (e.g. /home/tdporter/...).");
    return;
  }
  const efforts = ["low", "medium", "high", "xhigh", "max"] as const;
  let effort: (typeof efforts)[number] = "high";
  if (tokens[0] && (efforts as readonly string[]).includes(tokens[0])) {
    effort = tokens.shift() as (typeof efforts)[number];
  }
  const prompt = tokens.join(" ").trim();
  if (!prompt) {
    await ctx.reply("⚠️ Missing prompt after path/effort.");
    return;
  }
  await handleText(ctx, prompt, {
    model: "opus",
    effort,
    cwd,
    ephemeral: true,
  });
});

// Content handlers
bot.on("message:photo", handlePhoto);
bot.on("message:voice", handleVoice);
bot.on("message:text", async (ctx) => {
  // Skip commands (they're handled above)
  if (ctx.message.text.startsWith("/")) return;
  await handleText(ctx, ctx.message.text);
});

// Startup
console.log(`[startup] authorized user: ${env.TELEGRAM_ALLOWED_USER_ID}`);
console.log(`[startup] default cwd: ${env.DEFAULT_CWD}`);
console.log(`[startup] claude bin: ${env.CLAUDE_BIN}`);
await bot.start({
  onStart: (me) => console.log(`[startup] bot @${me.username} is live`),
});
