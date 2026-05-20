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
});

export const env = envSchema.parse(process.env);
