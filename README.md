# Telegram Codex Plugin

Send Codex task updates, test messages, and completion notifications to Telegram through a bot you control.

This plugin is useful when Codex is running longer tasks and you want a lightweight notification on your phone when work finishes. It includes a guided first-time setup, direct Telegram sends, and optional Codex hooks that automatically notify you when prompts like "Send me a Telegram when this task is done" complete.

## Features

- Guided Telegram bot linking with `@BotFather`
- Direct messages from Codex to your linked Telegram chat
- Optional task-completion notifications through Codex hooks
- MCP tools for status, sends, hook installation, and next-task notification arming
- Local-only storage for the bot token and chat id
- Dependency-free Python helper script

## Getting Started

1. Install or place this plugin in your Codex plugin marketplace.
2. Run the setup command from the plugin root:

```bash
python3 scripts/telegram.py setup
```

3. In Telegram, create a bot with `@BotFather` and paste the token into the terminal prompt.
4. Send the one-time setup code to your new bot when prompted.
5. Choose whether to enable Codex hooks for automatic completion notifications.
6. If you enable hooks, restart Codex so the running app session loads the new hook configuration.

After setup, try asking Codex:

```text
Send me a Telegram when this task is done.
```

## Common Commands

```bash
python3 scripts/telegram.py status
python3 scripts/telegram.py test
python3 scripts/telegram.py send "Build finished" --title "Codex"
python3 scripts/telegram.py install-hooks
python3 scripts/telegram.py uninstall-hooks
python3 scripts/telegram.py clear
```

## MCP Tools

When loaded by Codex, the plugin exposes:

- `telegram_status`
- `telegram_send_message`
- `telegram_notify_next`
- `telegram_install_hooks`

## How Notifications Work

The hook installer writes user-local entries to `~/.codex/hooks.json` and enables `codex_hooks = true` in `~/.codex/config.toml`.

- `UserPromptSubmit` detects Telegram notification intent and arms the current task.
- `Stop` sends the final completion message and clears the pending notification.

Hooks are opt-in because Telegram must be linked before automatic notifications are useful.

## Privacy

The plugin sends only the message text Codex or the user requests. The Telegram bot token and chat id are stored locally under `~/.codex/telegram/config.json`; they are not committed to this repository.

Treat the bot token like a password. If it is exposed, revoke or rotate it with `@BotFather`.

## License

MIT. See [LICENSE](LICENSE).

Telegram and the Telegram logo are trademarks of Telegram Messenger Inc. This plugin is not affiliated with, endorsed by, or sponsored by Telegram.
