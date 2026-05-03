# Scripts

`telegram.py` is intentionally dependency-free and portable across macOS, Linux, and Windows Python 3 environments.

`telegram_launcher.cmd` is a dual shell/batch launcher for Codex MCP stdio startup. On Unix-like systems it execs `python3`; on Windows it prefers `py -3` and falls back to `python`.

Use `telegram.py update-plugin` to refresh the current plugin files from the manifest repository or a saved user-local override. Remote updates require `git` on PATH; local directory update sources also work. Restart Codex after updating so changed skills and MCP tools are reloaded.
