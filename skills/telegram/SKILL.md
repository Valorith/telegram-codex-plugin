---
name: telegram
description: Send Telegram messages from Codex, guide first-time Telegram bot linking, and arm task-completion notifications through optional Codex hooks.
---

# Telegram

Use this skill when the user asks to set up Telegram notifications, send a Telegram message, test Telegram, link a Telegram account, or be notified on Telegram when Codex finishes a task.

## Core principles

- Never ask the user to paste a Telegram bot token into chat unless they explicitly choose that route after being warned. Prefer the interactive setup command or the `TELEGRAM_BOT_TOKEN` environment variable.
- Treat the bot token like a password. Do not echo it, log it, commit it, or include it in final answers.
- If the user asks for a one-time message and setup is incomplete, guide setup first.
- If the user asks to be notified when the current task is done, arm a completion notification before doing long-running work.
- Keep Telegram messages concise and useful: status, result, relevant workspace, and the final summary. Avoid sending secrets, large logs, or raw stack traces unless the user explicitly asks.

## Plugin files

The plugin's portable command is:

```bash
python3 plugins/telegram/scripts/telegram.py
```

If the plugin is installed somewhere else, locate `scripts/telegram.py` under the installed `telegram` plugin root and use that absolute path.

The command stores local user configuration under:

```text
~/.codex/telegram/config.json
```

Set `TELEGRAM_CODEX_CONFIG_DIR` to override that location.

## First-time setup

When the user asks to set up Telegram:

1. Explain that Telegram requires a bot. The user creates one with `@BotFather`, starts a chat with it, and the plugin links to that chat.
2. Run the setup command if appropriate:

```bash
python3 plugins/telegram/scripts/telegram.py setup
```

3. If an interactive terminal is not practical, give the user the exact command to run locally. They can also set `TELEGRAM_BOT_TOKEN` and run:

```bash
TELEGRAM_BOT_TOKEN='token-from-botfather' python3 plugins/telegram/scripts/telegram.py setup
```

The setup command validates the token with Telegram, shows a one-time code, waits for the user to send that code to the bot, saves the chat id locally, sends a test message, and offers to install Codex completion hooks.

## Sending messages

For direct sends, prefer the MCP tool `telegram_send_message` when it is available. Otherwise run:

```bash
python3 plugins/telegram/scripts/telegram.py send "Message text" --title "Codex"
```

Use this command for a test:

```bash
python3 plugins/telegram/scripts/telegram.py test
```

Use this command to inspect setup:

```bash
python3 plugins/telegram/scripts/telegram.py status
```

## Completion notifications

If hooks are installed, a prompt that mentions Telegram plus a notify/message/ping/alert intent automatically arms a notification. Examples:

- "Send me a Telegram when this is done."
- "Ping me on Telegram after the build finishes."
- "Let me know on Telegram when you finish the QA pass."

For explicit arming, prefer the MCP tool `telegram_notify_next` when available. Otherwise run:

```bash
python3 plugins/telegram/scripts/telegram.py notify-next --title "Codex task complete" --prompt "User requested a Telegram completion alert"
```

If hooks are not installed, install them with:

```bash
python3 plugins/telegram/scripts/telegram.py install-hooks
```

For duration-based notifications on every Codex task, configure the threshold in minutes:

```bash
python3 plugins/telegram/scripts/telegram.py auto-notify 5
```

Or install hooks and set the threshold in one command:

```bash
python3 plugins/telegram/scripts/telegram.py install-hooks --auto-notify-minutes 5
```

Use this command to disable duration-based notifications:

```bash
python3 plugins/telegram/scripts/telegram.py auto-notify off
```

Hooks are user-local. The installer writes absolute script paths into `~/.codex/hooks.json` and enables `codex_hooks = true` in `~/.codex/config.toml`.
After installing hooks, tell the user to restart Codex so the running app session loads the new hook configuration.

## Plugin updates

If the user asks to update or refresh the Telegram plugin, prefer the MCP tool `telegram_update_plugin` when it is available. For a preview, use `telegram_check_plugin_update`.

Otherwise run:

```bash
python3 plugins/telegram/scripts/telegram.py update-plugin
```

To preview changed files without copying:

```bash
python3 plugins/telegram/scripts/telegram.py update-plugin --dry-run
```

The updater uses the plugin manifest's `repository` value by default. To point future updates at a fork or another repository, run:

```bash
python3 plugins/telegram/scripts/telegram.py update-plugin --repo https://github.com/example/telegram-codex-plugin --ref main --save-source
```

The saved update source is local user configuration under `~/.codex/telegram/config.json`; it does not affect the Telegram bot token or chat id. After a successful update, tell the user to restart Codex so the running session reloads changed plugin skills and MCP tools.

## Troubleshooting

- `Telegram is not linked yet`: run `setup`.
- `Timed out waiting for the setup code`: run `setup` again and send the new one-time code to the bot.
- No automatic completion message: run `status`, then `install-hooks`, then restart Codex.
- No duration-based completion message: run `status` and confirm the auto-notify threshold is not off; short tasks under the threshold are intentionally ignored.
- Wrong chat: run `clear`, then `setup` again from the Telegram account or group that should receive messages.

## Privacy

The plugin sends only the message text Codex or the user requests. The bot token and chat id are stored locally. Telegram delivery uses the official Telegram Bot API.
