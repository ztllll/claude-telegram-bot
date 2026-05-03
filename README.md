# claude-telegram-bot

> Use [Claude Code](https://docs.claude.com/en/docs/claude-code) from your phone via Telegram.

A Python bot that bridges Telegram to your local `claude -p` CLI. Inherits your local Claude Code config (model, third-party API endpoints, MCP servers). Supports streaming responses with live tool-call status lines via Telegram Bot API 9.5's native `sendMessageDraft`.

**Why this exists:** Anthropic's first-party Telegram MCP integration only works with the official API. If your `claude` CLI is configured against a third-party endpoint (`ANTHROPIC_BASE_URL`), you need a self-hosted bridge — this is one.

```
You (Telegram)  ──►  Bot (Python, on your Mac)  ──►  claude -p  ──►  Your API endpoint
                          ↑                              │
                          └─── streams tool calls   ◄────┘
                               and token deltas back
```

---

## Features

- **Multi-turn context** — `session_id` persisted per Telegram user
- **Live status lines** — `[bash] echo hi`, `[read] /path/to/foo.py` appear in real time so you know Claude is still working
- **Token-level streaming** — replies appear progressively (typewriter effect) via Telegram's native `sendMessageDraft`
- **Multimodal** — send photos, documents, audio; bot downloads and passes them to Claude
- **Image return protocol** — Claude can include `[[img:/abs/path]]` markers in its reply; bot calls `sendPhoto` automatically
- **Whitelist** — only authorized Telegram user IDs can talk to the bot
- **Per-user serialization** — same user's messages queue; different users run in parallel
- **launchd-ready** — runs as a LaunchAgent on macOS (auto-start, crash-restart)
- **Auto-cleanup** — old downloads pruned after N days
- **Independent of bot's own session** — kill the terminal, the bot keeps running

## Requirements

- macOS 13+ (Linux works too with systemd instead of launchd; not bundled here)
- Python 3.12+
- [`claude` CLI](https://docs.claude.com/en/docs/claude-code) 2.1+ on `PATH`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- The Telegram numeric `user_id`(s) you want to whitelist

## Quick start (5 minutes)

```bash
# 1. Clone
git clone https://github.com/ztllll/warp-zh.git    # ← swap to this repo's URL
cd claude-telegram-bot

# 2. Install Python deps
pip3 install --user -r requirements.txt

# 3. Configure
cp config.example.json config.json
chmod 600 config.json
$EDITOR config.json   # fill bot_token + allowed_user_ids

# 4. Foreground smoke test
./run.sh
# → send a message to your bot in Telegram, confirm it replies
# → Ctrl+C to stop

# 5. (macOS) Install as a LaunchAgent for permanent operation
cp com.example.claude-telegram-bot.plist \
   ~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist
$EDITOR ~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist
# replace every YOUR_* placeholder
chmod 600 ~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist
launchctl bootstrap gui/$(id -u) \
   ~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist
launchctl list | grep claude-telegram-bot   # PID column non-empty = running
```

### Getting your Telegram user_id

Bot API can't look up users by username. To get your numeric ID:

1. Send any message to your bot in Telegram (e.g. `/start`)
2. Run:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[].message.from'
   ```
3. The `id` field is your user_id. Add it to `allowed_user_ids` in `config.json`.

## Bot commands

| Command | Behavior |
|---|---|
| `/start`, `/help` | Welcome message |
| `/new` | Start a new conversation (clears session_id) |
| `/status` | Show current session_id (first 8 chars) |
| anything else | Forwarded to Claude |

## Image return protocol

When Claude needs to send an image to you (screenshots, generated charts, etc.), it includes a marker in its reply:

```
Here's the diagram you asked for: [[img:/tmp/chart.png]]
```

The bot strips the marker and sends the image via `sendPhoto`. Multiple markers per reply are supported.

To make Claude aware of this convention, document it in your Claude Code memory (`~/.claude/projects/.../memory/`) — see [DEVELOPMENT.md](DEVELOPMENT.md#image-return-protocol).

## Security

⚠️ **The whitelist is the only security layer.** Claude runs with your full home-directory permissions by default. Anyone who controls a whitelisted Telegram account can have Claude read/write any file in your home and execute arbitrary shell commands.

Recommendations:
- Enable Telegram two-step verification on your account
- `chmod 600` `config.json` and the installed plist (both contain secrets)
- Don't sync the project directory to iCloud / Dropbox / git with `config.json` present (`.gitignore` covers this for git)
- Consider restricting tools via `claude_args` (see [DEVELOPMENT.md §Configuration](DEVELOPMENT.md#configuration))
- Consider adding a bot-level second password (see [DEVELOPMENT.md §Hardening](DEVELOPMENT.md#hardening))

## Architecture

The bot calls `claude -p --output-format stream-json --verbose --include-partial-messages` and parses the JSONL event stream:

- `system/init` → log session_id
- `assistant` events with `tool_use` → format as `[bash] cmd`, `[read] path`, ... and append to a status log
- `stream_event/content_block_delta` with `text_delta` → accumulate progressive text
- `result` event → finalize: replace draft with permanent `sendMessage`, persist session_id

The accumulated state (status log + text) is rendered via `sendMessageDraft` (Bot API 9.5+, available to all bots since 2026-03-01) with throttled updates. When the draft exceeds 4000 characters, the bot seals it as a permanent message and starts a new draft, preserving continuity.

See [DEVELOPMENT.md](DEVELOPMENT.md) for the full architecture, all event field shapes, the `StreamRenderer` state machine, error-handling paths, and a complete from-scratch walkthrough.

## Project layout

```
claude-telegram-bot/
├── bot.py                                  # Main program (~510 lines)
├── requirements.txt                        # python-telegram-bot[rate-limiter]==22.7
├── run.sh                                  # Launcher
├── config.example.json                     # Config template
├── com.example.claude-telegram-bot.plist   # launchd template
├── README.md                               # This file
├── DEVELOPMENT.md                          # Full development docs
├── LICENSE                                 # MIT
└── .gitignore
```

Generated at runtime (gitignored):
- `config.json` — actual config with bot_token + whitelist
- `sessions.json` — `{telegram_user_id: claude_session_id}`
- `downloads/` — Telegram attachments (auto-pruned)
- `logs/stdout.log`, `logs/stderr.log` — bot logs

## License

MIT — see [LICENSE](LICENSE).

The bot's purpose is to forward messages to Claude Code, which is governed by [Anthropic's Acceptable Use Policy](https://www.anthropic.com/legal/aup). The bot does not bundle or redistribute Claude Code itself.

## Contributing

Issues and PRs welcome. Common improvement areas:
- Linux / systemd packaging (currently only macOS / launchd)
- Bot-level second password (rate-limit + auth flow)
- Token usage tracking + spend alerts
- Group chat support (currently rejects non-private chats implicitly via the streaming API)
- Additional tool status formatters in `format_tool_status()`
