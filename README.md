# claude-telegram

Chat with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from your phone via Telegram. Uses your **Max plan** auth — no API key, no per-token costs.

I built this because the most useful AI assistant in my life lives in my terminal on my laptop, and I'm not always at my laptop. Bridging it to Telegram (running locally as a systemd service) gives me a "second brain in my pocket" without paying API rates on top of the Max subscription I already have.

```
Telegram (phone)
    ↓
Linux box runs the bot (systemd service)
    ↓
Bot spawns `claude -p --resume <id>` per message
    ↓
Claude Code with full tools, your memory, your CLAUDE.md
    ↓
Responses streamed back as Telegram messages
```

## Features

- **Real Claude Code, not an API wrapper.** Your `~/.claude/CLAUDE.md`, your memory, your MCP servers, your skills — all of it works exactly like it does on the desktop.
- **Per-cycle streaming UX.** Each Claude "thinking cycle" sends a `💭 thinking…` placeholder that gets edited *once* into its actual text. When you stop seeing `thinking…` messages, you know it's finished. No rolling edits, no overwrite races, no duplicated content.
- **Tunable model + effort** per env var or per message. Default is `sonnet` + `low` effort (snappy capture from the phone); `/deep` upgrades a single turn to `opus + high` for harder work; `/run` lets you point one turn at a different working directory entirely.
- **Voice notes** are saved to disk; for *dictating to* the bot, use your phone's native voice-to-text (faster than rolling your own transcription).
- **Photos** auto-save to your configured attachments directory.
- **Auth-gated to a single Telegram user ID.** The bot ignores everyone else.

## Why not just use the Claude app on mobile?

Different shape of problem. The Claude mobile app is for chatting with Claude. This bot is for instructing Claude *Code* — your actual tools, your actual filesystem, your actual workflows. It can edit files on your laptop, run commands, hit your MCP servers. The Claude mobile app can't do any of that.

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram.
2. `/newbot` → pick a name and handle.
3. Save the bot token.

### 2. Get your Telegram user ID

1. Message [@userinfobot](https://t.me/userinfobot).
2. Note your numeric ID.

### 3. Install

```bash
git clone https://github.com/DylPorter/claude-telegram.git ~/Documents/Programming/claude-telegram
cd ~/Documents/Programming/claude-telegram
npm install
```

### 4. Configure

```bash
cp .env.example .env
$EDITOR .env   # paste your bot token and user ID
```

Make sure Claude Code is already installed and authenticated on this machine (`claude auth login`). The bot inherits your existing Max-plan login.

### 5. Try it (dev)

```bash
npm run dev
```

Send your bot a message from Telegram. You should see a `💭 thinking…` reply within a few seconds, edited to the actual response shortly after.

### 6. Run as a service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/claude-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-telegram.service
systemctl --user status claude-telegram.service
```

Logs:

```bash
journalctl --user -u claude-telegram.service -f
```

## Slash commands

| Command | What it does |
|---|---|
| `/start` | Greeting + command list |
| `/reset` | Start a fresh Claude session (clears resume id) |
| `/status` | Show current session id + working dir |
| `/cd <abs-path>` | Switch the chat's working dir (starts a fresh session there) |
| `/vault` | Switch to your default working dir (the `DEFAULT_CWD` env value) |
| `/deep <prompt>` | Run *this turn only* at `opus + high` effort. Same chat session continues. |
| `/run <abs-path> [low\|med\|high\|xhigh\|max] <prompt>` | One-off: opus + chosen effort, in a different cwd, fresh ephemeral session. Doesn't touch your main chat state. |

Anything that isn't a slash command is processed as a regular message.

## Configuration

All env vars live in `.env`:

| Var | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather | _(required)_ |
| `TELEGRAM_ALLOWED_USER_ID` | The only user allowed to talk to the bot | _(required)_ |
| `CLAUDE_BIN` | Path to the `claude` binary | `claude` |
| `DEFAULT_CWD` | Working dir for new chats and `/vault` | _(required)_ |
| `STATE_DIR` | Where per-chat session state is stored | _(required)_ |
| `CLAUDE_MODEL` | `sonnet`, `haiku`, `opus`, or a full model id | `sonnet` |
| `CLAUDE_EFFORT` | Thinking budget: `low`, `medium`, `high`, `xhigh`, `max` | `low` |

To bump the default model permanently, edit `.env` and `systemctl --user restart claude-telegram`.

## Design notes

**Placeholder-per-cycle output.** Per user turn, the bot sends one `💭 thinking…` message. When Claude emits text, that message gets edited *exactly once* to the text. If Claude then uses a tool and responds again, a *new* `💭 thinking…` message is sent for the next cycle. Each Telegram message is therefore edited at most once. This is deliberately different from "stream tokens into one rolling message" — that pattern hits Telegram's edit-throttle, fights with chunk boundaries, and has a nasty failure mode where edits overwrite earlier content. The per-cycle pattern also gives you a free "I'm done" signal: when the last message in the chat is real content (not `thinking…`), the turn is finished.

**Tool activity is silent.** The old version of this bot rendered `🔧 Tool …` / `✓ result` chatter for every tool call. Nice for debugging, ugly for daily use. Now: tools run, you see the output cycle that follows. If you want the trace, `journalctl` it.

**Cold-spawn architecture.** Today, every message spawns `claude -p --resume <session-id>` as a fresh subprocess. The CLI cold start is ~3–5s before any token is generated. That's the main source of perceived latency. A future version may migrate to `@anthropic-ai/claude-agent-sdk` so one bot process keeps a Claude session warm in-memory — that should eliminate the bulk of cold-start lag.

## Limitations

- Linux-focused (systemd unit included). Should work fine on macOS via launchd; you'd have to adapt the service file.
- One authorized user per bot instance. Not multi-tenant.
- Telegram message size cap of 4 KB per message — long outputs get split into multiple messages, but tables and code blocks may break across the seam.
- `/run` tokenizer doesn't handle paths with spaces. Don't put spaces in your project paths and you'll be fine.
- No streaming of partial messages within a cycle. Cold-spawn + Telegram edit limits make this not worth doing today.

## Security

- Only the user matching `TELEGRAM_ALLOWED_USER_ID` is allowed to interact. Everyone else is silently ignored.
- Claude runs with `--permission-mode bypassPermissions`. The bot effectively has the same filesystem + shell access as the user running the systemd service. **Don't run this on a machine where that would be unacceptable.**
- The bot makes outbound connections to Telegram only. No inbound ports.
- `.env` contains a long-lived bot token — keep it out of version control (already in `.gitignore`).

## Architecture

```
src/
├── index.ts              Bot entrypoint, command routing, auth gate
├── lib/
│   ├── env.ts            Zod-validated env config
│   ├── claude.ts         Spawns `claude -p` and parses stream-json events
│   ├── session.ts        Per-chat session-id + cwd state, JSON-file backed
│   └── vault.ts          Saves photo/voice attachments
└── handlers/
    ├── commands.ts       /start, /reset, /status, /cd, /vault
    ├── text.ts           Placeholder-per-cycle text streaming
    ├── photo.ts          Photo capture
    └── voice.ts          Voice-note capture (transcription is gboard's job)
```

## Stack

- [grammY](https://grammy.dev/) for the Telegram bot framework
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI as the brain (via `--output-format stream-json`)
- TypeScript + [tsx](https://github.com/privatenumber/tsx) for the runtime
- systemd for "always on"

## License

MIT.
