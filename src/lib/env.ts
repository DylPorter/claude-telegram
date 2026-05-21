import "dotenv/config";
import { z } from "zod";

const envSchema = z.object({
  TELEGRAM_BOT_TOKEN: z.string().min(1, "Missing TELEGRAM_BOT_TOKEN"),
  TELEGRAM_ALLOWED_USER_ID: z.string().regex(/^\d+$/).transform((s) => Number(s)),
  CLAUDE_BIN: z.string().default("claude"),
  DEFAULT_CWD: z.string(),
  STATE_DIR: z.string(),
  CLAUDE_MODEL: z.string().default("sonnet"),
  CLAUDE_EFFORT: z.enum(["low", "medium", "high", "xhigh", "max"]).default("low"),
  // Outbound /push HTTP server for scheduled briefs and external pushes.
  PUSH_PORT: z.string().regex(/^\d+$/).default("7421").transform((s) => Number(s)),
  PUSH_SECRET: z.string().min(16, "PUSH_SECRET must be at least 16 chars"),
});

export const env = envSchema.parse(process.env);
