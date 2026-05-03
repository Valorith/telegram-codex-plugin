# Telegram Hooks

Hooks are opt-in because Telegram must be linked before automatic notifications can work.

Run:

```bash
python3 plugins/telegram/scripts/telegram.py install-hooks
```

The installer writes user-local hook entries with absolute script paths to:

```text
~/.codex/hooks.json
```

It also enables:

```toml
[features]
codex_hooks = true
```

in:

```text
~/.codex/config.toml
```

Restart Codex after installing hooks so the running app session loads the new hook configuration.

Use this command to remove only the Telegram hook handlers:

```bash
python3 plugins/telegram/scripts/telegram.py uninstall-hooks
```
