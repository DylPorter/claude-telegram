import type { Context } from "grammy";
import { saveAttachmentToVault } from "../lib/vault.js";
import { handleText } from "./text.js";

export async function handlePhoto(ctx: Context): Promise<void> {
  if (!ctx.message?.photo?.length) return;

  // Telegram sends multiple sizes; take the largest
  const photo = ctx.message.photo[ctx.message.photo.length - 1];
  const file = await ctx.api.getFile(photo.file_id);

  if (!file.file_path) {
    await ctx.reply("⚠️ Couldn't fetch photo.");
    return;
  }

  const url = `https://api.telegram.org/file/bot${ctx.api.token}/${file.file_path}`;
  const res = await fetch(url);
  const buffer = Buffer.from(await res.arrayBuffer());
  const ext = file.file_path.split(".").pop() || "jpg";

  const savedPath = await saveAttachmentToVault({ buffer, extension: ext });
  const caption = ctx.message.caption || "";

  await ctx.reply(`📎 Saved: \`${savedPath}\``, { parse_mode: "Markdown" });

  if (caption) {
    await handleText(
      ctx,
      `I just attached a photo at ${savedPath}. Caption: "${caption}". Process it appropriately for my vault (if it's a whiteboard, extract ideas; if it's a receipt, log it; etc).`,
    );
  }
}
