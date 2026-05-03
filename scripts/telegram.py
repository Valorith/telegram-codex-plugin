#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import html
import json
import math
import os
import pathlib
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PLUGIN_NAME = "telegram"
PLUGIN_VERSION = "0.1.0"
API_ROOT = "https://api.telegram.org"
MESSAGE_BODY_LIMIT = 3300
COMPLETION_RESULT_LIMIT = 1500
AUTO_NOTIFY_SOURCE = "auto-threshold"
USER_PROMPT_SOURCE = "UserPromptSubmit hook"
PLUGIN_UPDATE_REPOSITORY_KEY = "plugin_update_repository"
PLUGIN_UPDATE_REF_KEY = "plugin_update_ref"
PLUGIN_OWNED_PATHS = (
    ".codex-plugin",
    ".mcp.json",
    "skills",
    "scripts",
    "hooks",
    "assets",
    "README.md",
    "LICENSE",
)
PLUGIN_COPY_IGNORE_NAMES = {
    ".DS_Store",
    ".git",
    ".github",
    "__pycache__",
}
TELEGRAM_RE = re.compile(r"\b(telegram|tg)\b", re.IGNORECASE)
NOTIFY_INTENT_RE = re.compile(
    r"\b(send me|notify me|ping me|alert me|message me|let me know|tell me|telegram me)\b"
    r"|\b(send|notify|ping|alert)\b.{0,80}\b(telegram|tg)\b"
    r"|\b(telegram|tg)\b.{0,80}\b(notify|ping|alert)\b",
    re.IGNORECASE | re.DOTALL,
)
COMPLETION_CONTEXT_RE = re.compile(
    r"\b(when|after|once)\b.{0,120}\b(done|finished|finishes|finish|complete|completes|completed|completion|ends|ended)\b"
    r"|\b(done|finished|complete|completed)\b.{0,80}\b(send|notify|ping|alert|message|telegram|tg|let me know|tell me)\b",
    re.IGNORECASE | re.DOTALL,
)
INTERNAL_PROMPT_PREFIXES = (
    "You are an expert at upholding safety and compliance standards for Codex ambient suggestions.",
)


class TelegramError(RuntimeError):
    pass


def codex_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")).expanduser()


def state_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("TELEGRAM_CODEX_CONFIG_DIR", codex_home() / "telegram")).expanduser()


def config_path() -> pathlib.Path:
    return state_root() / "config.json"


def pending_dir() -> pathlib.Path:
    return state_root() / "pending"


def sent_dir() -> pathlib.Path:
    return state_root() / "sent"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_private_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def write_private_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)


def load_json(path: pathlib.Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_config() -> dict[str, Any]:
    return load_json(config_path(), {})


def save_config(config: dict[str, Any]) -> None:
    config = dict(config)
    config["updated_at"] = now_iso()
    write_private_json(config_path(), config)


def plugin_root() -> pathlib.Path:
    script_path = pathlib.Path(__file__).resolve()
    for candidate in (script_path.parent, *script_path.parents):
        if (candidate / ".codex-plugin" / "plugin.json").exists():
            return candidate
    return script_path.parent.parent


def load_plugin_manifest(root: pathlib.Path | None = None) -> dict[str, Any]:
    root = plugin_root() if root is None else root
    manifest_path = root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TelegramError(f"Could not find plugin manifest at {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise TelegramError(f"Could not parse plugin manifest at {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise TelegramError(f"Plugin manifest at {manifest_path} is not a JSON object.")
    return manifest


def configured_update_repository(config: dict[str, Any] | None = None, root: pathlib.Path | None = None) -> str:
    config = load_config() if config is None else config
    configured = str(config.get(PLUGIN_UPDATE_REPOSITORY_KEY) or "").strip()
    if configured:
        return configured
    manifest = load_plugin_manifest(root)
    repository = manifest.get("repository")
    if isinstance(repository, dict):
        repository = repository.get("url")
    repository = str(repository or manifest.get("homepage") or "").strip()
    if not repository:
        raise TelegramError(
            "No plugin update repository is configured. Add repository to .codex-plugin/plugin.json or run update-plugin with --repo."
        )
    return repository


def configured_update_ref(config: dict[str, Any] | None = None) -> str | None:
    config = load_config() if config is None else config
    value = str(config.get(PLUGIN_UPDATE_REF_KEY) or "").strip()
    return value or None


def configure_plugin_update_source(repository: str | None, ref: str | None, reset: bool = False) -> dict[str, Any]:
    config = load_config()
    if reset:
        config.pop(PLUGIN_UPDATE_REPOSITORY_KEY, None)
        config.pop(PLUGIN_UPDATE_REF_KEY, None)
    if repository is not None:
        repository = repository.strip()
        if not repository:
            raise TelegramError("Update repository cannot be empty.")
        config[PLUGIN_UPDATE_REPOSITORY_KEY] = repository
    if ref is not None:
        ref = ref.strip()
        if ref:
            config[PLUGIN_UPDATE_REF_KEY] = ref
        else:
            config.pop(PLUGIN_UPDATE_REF_KEY, None)
    save_config(config)
    return config


def is_linked(config: dict[str, Any] | None = None) -> bool:
    config = load_config() if config is None else config
    return bool(config.get("bot_token") and config.get("chat_id"))


def auto_notify_minutes(config: dict[str, Any] | None = None) -> float | None:
    config = load_config() if config is None else config
    value = config.get("auto_notify_min_minutes")
    if value in (None, "", False):
        return None
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(minutes) or minutes <= 0:
        return None
    return minutes


def parse_minutes(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"off", "disable", "disabled", "none", "false"}:
        return None
    try:
        minutes = float(value)
    except (TypeError, ValueError) as exc:
        raise TelegramError("Auto-notify minutes must be a positive number, or 'off'.") from exc
    if not math.isfinite(minutes) or minutes < 0:
        raise TelegramError("Auto-notify minutes must be zero or greater.")
    if minutes == 0:
        return None
    return minutes


def configure_auto_notify(minutes: float | None) -> dict[str, Any]:
    config = load_config()
    if minutes is None:
        config.pop("auto_notify_min_minutes", None)
    else:
        config["auto_notify_min_minutes"] = minutes
    save_config(config)
    return config


def format_minutes(minutes: float) -> str:
    if minutes.is_integer():
        return str(int(minutes))
    return f"{minutes:g}"


def format_duration(minutes: float) -> str:
    amount = format_minutes(minutes)
    unit = "minute" if amount == "1" else "minutes"
    return f"{amount} {unit}"


def auto_notify_status_text(config: dict[str, Any] | None = None) -> str:
    minutes = auto_notify_minutes(config)
    if minutes is None:
        return "off"
    return f"tasks >= {format_minutes(minutes)} minute(s)"


def update_source_status(config: dict[str, Any] | None = None, root: pathlib.Path | None = None) -> dict[str, Any]:
    config = load_config() if config is None else config
    repository = configured_update_repository(config, root)
    ref = configured_update_ref(config)
    return {
        "repository": repository,
        "ref": ref,
        "configured": bool(config.get(PLUGIN_UPDATE_REPOSITORY_KEY) or config.get(PLUGIN_UPDATE_REF_KEY)),
    }


def update_restart_notice() -> str:
    return "Restart Codex after updating so this running session reloads changed plugin skills and MCP tools."


def update_plugin(args: argparse.Namespace) -> int:
    result = perform_plugin_update(
        repository=args.repo,
        ref=args.ref,
        save_source=args.save_source,
        reset_source=args.reset_source,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("updated"):
        print(update_restart_notice())
    return 0


def perform_plugin_update(
    repository: str | None = None,
    ref: str | None = None,
    save_source: bool = False,
    reset_source: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    target_root = plugin_root()
    if reset_source:
        configure_plugin_update_source(None, None, reset=True)
    config = load_config()
    repository = (repository or configured_update_repository(config, target_root)).strip()
    ref = (ref or configured_update_ref(config) or "").strip() or None

    with tempfile.TemporaryDirectory(prefix="telegram-plugin-update-") as tmp:
        source_root = fetch_plugin_update_source(repository, ref, pathlib.Path(tmp))
        validate_plugin_update_payload(source_root)
        if save_source:
            configure_plugin_update_source(repository, ref, reset=False)
        changes = diff_plugin_payload(source_root, target_root)
        result = {
            "repository": repository,
            "ref": ref,
            "target_root": str(target_root),
            "changed": changes,
            "changed_count": len(changes),
            "dry_run": dry_run,
            "updated": False,
            "restart_notice": update_restart_notice(),
        }
        if dry_run:
            return result
        apply_plugin_update(source_root, target_root)
        result["updated"] = True
        return result


def fetch_plugin_update_source(repository: str, ref: str | None, tmp_root: pathlib.Path) -> pathlib.Path:
    local_path = pathlib.Path(repository).expanduser()
    if local_path.exists():
        if not local_path.is_dir():
            raise TelegramError(f"Update source is not a directory: {local_path}")
        return local_path.resolve()

    git = shutil.which("git")
    if not git:
        raise TelegramError("Updating from a remote repository requires git on PATH. Local directory sources also work.")

    destination = tmp_root / "source"
    command = [git, "clone", "--depth", "1"]
    if ref:
        command.extend(["--branch", ref])
    command.extend([repository, str(destination)])
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"git clone exited with {completed.returncode}"
        raise TelegramError(f"Could not fetch plugin update source: {detail}")
    return destination


def validate_plugin_update_payload(source_root: pathlib.Path) -> dict[str, Any]:
    manifest = load_plugin_manifest(source_root)
    if manifest.get("name") != PLUGIN_NAME:
        raise TelegramError(
            f"Update payload is for plugin {manifest.get('name')!r}, expected {PLUGIN_NAME!r}."
        )
    required_files = [
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "scripts/telegram.py",
        "skills/telegram/SKILL.md",
    ]
    missing = [relative for relative in required_files if not (source_root / relative).is_file()]
    if missing:
        raise TelegramError(f"Update payload is missing required file(s): {', '.join(missing)}")
    forbidden = [
        "telegram/config.json",
        ".codex/telegram/config.json",
        "config.json",
    ]
    present = [relative for relative in forbidden if (source_root / relative).exists()]
    if present:
        raise TelegramError(f"Update payload contains user-private config file(s): {', '.join(present)}")
    return manifest


def diff_plugin_payload(source_root: pathlib.Path, target_root: pathlib.Path) -> list[str]:
    source_files = collect_owned_files(source_root)
    target_files = collect_owned_files(target_root)
    changed: list[str] = []
    for relative in sorted(source_files | target_files):
        source_path = source_root / relative
        target_path = target_root / relative
        if not source_path.exists() or not target_path.exists():
            changed.append(relative)
            continue
        if source_path.read_bytes() != target_path.read_bytes():
            changed.append(relative)
    return changed


def collect_owned_files(root: pathlib.Path) -> set[str]:
    files: set[str] = set()
    for relative in PLUGIN_OWNED_PATHS:
        path = root / relative
        if not path.exists():
            continue
        if path.is_file():
            files.add(relative)
            continue
        for current_root, dirs, filenames in os.walk(path):
            dirs[:] = [name for name in dirs if name not in PLUGIN_COPY_IGNORE_NAMES]
            for filename in filenames:
                if filename in PLUGIN_COPY_IGNORE_NAMES:
                    continue
                file_path = pathlib.Path(current_root) / filename
                files.add(file_path.relative_to(root).as_posix())
    return files


def apply_plugin_update(source_root: pathlib.Path, target_root: pathlib.Path) -> None:
    validate_plugin_update_payload(source_root)
    for relative in PLUGIN_OWNED_PATHS:
        source_path = source_root / relative
        target_path = target_root / relative
        if not source_path.exists():
            if target_path.exists():
                remove_path(target_path)
            continue
        if source_path.is_file():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            continue
        mirror_directory(source_path, target_path)


def mirror_directory(source: pathlib.Path, target: pathlib.Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    source_files = collect_relative_files(source)
    target_files = collect_relative_files(target)
    for relative in sorted(target_files - source_files, reverse=True):
        remove_path(target / relative)
    for relative in sorted(source_files):
        source_file = source / relative
        target_file = target / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
    remove_empty_dirs(target)


def collect_relative_files(root: pathlib.Path) -> set[pathlib.Path]:
    files: set[pathlib.Path] = set()
    if not root.exists():
        return files
    for current_root, dirs, filenames in os.walk(root):
        dirs[:] = [name for name in dirs if name not in PLUGIN_COPY_IGNORE_NAMES]
        for filename in filenames:
            if filename in PLUGIN_COPY_IGNORE_NAMES:
                continue
            file_path = pathlib.Path(current_root) / filename
            files.add(file_path.relative_to(root))
    return files


def remove_empty_dirs(root: pathlib.Path) -> None:
    if not root.exists() or not root.is_dir():
        return
    for current_root, dirs, _ in os.walk(root, topdown=False):
        for dirname in dirs:
            path = pathlib.Path(current_root) / dirname
            try:
                path.rmdir()
            except OSError:
                pass


def remove_path(path: pathlib.Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def redact(value: str | None, keep: int = 4) -> str:
    if not value:
        return "not set"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def api_call(method: str, payload: dict[str, Any] | None = None, token: str | None = None, timeout: int = 20) -> Any:
    token = token or load_config().get("bot_token")
    if not token:
        raise TelegramError("Telegram is not linked yet. Run setup first.")
    url = f"{API_ROOT}/bot{token}/{method}"
    data = urllib.parse.urlencode(payload or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TelegramError(f"Telegram API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc.reason) and shutil.which("curl"):
            return api_call_with_curl(url, payload or {}, timeout=timeout)
        raise TelegramError(f"Could not reach Telegram: {exc.reason}") from exc
    result = json.loads(body)
    if not result.get("ok"):
        raise TelegramError(result.get("description") or "Telegram API request failed.")
    return result.get("result")


def api_call_with_curl(url: str, payload: dict[str, Any], timeout: int) -> Any:
    data = urllib.parse.urlencode(payload)
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--max-time",
        str(timeout),
        "--request",
        "POST",
        "--data",
        data,
        url,
    ]
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    body = completed.stdout.strip()
    if completed.returncode != 0:
        detail = body or completed.stderr.strip() or f"curl exited with {completed.returncode}"
        raise TelegramError(f"Telegram API request failed: {detail}")
    result = json.loads(body)
    if not result.get("ok"):
        raise TelegramError(result.get("description") or "Telegram API request failed.")
    return result.get("result")


def get_bot(token: str) -> dict[str, Any]:
    result = api_call("getMe", token=token)
    if not isinstance(result, dict) or not result.get("username"):
        raise TelegramError("The token worked, but Telegram did not return a bot username.")
    return result


def send_message(text: str, title: str | None = None, disable_preview: bool = True) -> dict[str, Any]:
    config = load_config()
    chat_id = config.get("chat_id")
    if not chat_id:
        raise TelegramError("Telegram is not linked to a chat yet. Run setup first.")
    rendered = render_message(text, title=title)
    result = send_rendered_message(rendered, chat_id=chat_id, disable_preview=disable_preview)
    return {
        "ok": True,
        "chat_id": chat_id,
        "message_id": result.get("message_id") if isinstance(result, dict) else None,
    }


def send_rendered_message(rendered: str, chat_id: Any, disable_preview: bool = True) -> dict[str, Any]:
    payload = {
        "chat_id": str(chat_id),
        "text": rendered,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    return api_call("sendMessage", payload)


def render_message(text: str, title: str | None = None) -> str:
    clean_text = text.strip() or "Codex finished a task."
    if len(clean_text) > MESSAGE_BODY_LIMIT:
        clean_text = clean_text[: MESSAGE_BODY_LIMIT - 10].rstrip() + "..."
    title = title.strip() if title else "Codex"
    return f"<b>{html.escape(title)}</b>\n\n{html.escape(clean_text)}"


def render_completion_message(
    *,
    title: str,
    request: str = "",
    workspace: str = "",
    duration: str = "",
    result: str = "",
) -> str:
    sections: list[tuple[str, str, bool]] = []
    if request:
        sections.append(("Request", request, False))
    if workspace:
        sections.append(("Workspace", workspace, True))
    if duration:
        sections.append(("Duration", duration, False))
    if result:
        sections.append(("Result", result, False))
    if not sections:
        return render_message("Codex finished the task.", title=title)

    rendered_sections = []
    for label, value, monospace in sections:
        escaped_value = html.escape(value.strip())
        if monospace:
            escaped_value = f"<code>{escaped_value}</code>"
        rendered_sections.append(f"<b>{html.escape(label)}</b>\n{escaped_value}")
    return f"<b>{html.escape(title.strip() or 'Codex task complete')}</b>\n\n" + "\n\n".join(rendered_sections)


def print_header(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{suffix}] ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def setup(args: argparse.Namespace) -> int:
    previous_config = load_config()
    print_header("Telegram for Codex setup")
    print(
        textwrap.fill(
            "This links Codex to a Telegram bot that you control. The bot token and chat id are stored locally under "
            f"{config_path()} with user-only file permissions where the platform supports them.",
            width=88,
        )
    )
    print()
    print("1. Open Telegram and start @BotFather.")
    print("2. Send /newbot, choose a display name and username, then copy the bot token.")
    print("3. Paste the token below. It will not be printed back.")
    print()

    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        token = getpass.getpass("Bot token: ").strip()
    if not token:
        raise TelegramError("No bot token provided.")

    bot = get_bot(token)
    username = str(bot["username"])
    print(f"Connected to @{username}.")

    offset = latest_update_offset(token)
    code = f"codex-{random.randint(100000, 999999)}"
    print()
    print("Now link your Telegram account:")
    print(f"1. Open https://t.me/{username}")
    print("2. Press Start if Telegram shows that button.")
    print(f"3. Send this one-time code to the bot: {code}")
    print()

    chat = wait_for_chat(token, code, offset=offset, timeout_seconds=args.timeout)
    config = {
        "bot_token": token,
        "bot_username": username,
        "bot_id": bot.get("id"),
        "chat_id": chat["id"],
        "chat_type": chat.get("type"),
        "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name"),
        "created_at": now_iso(),
        "hooks_enabled": False,
    }
    if auto_notify_minutes(previous_config) is not None:
        config["auto_notify_min_minutes"] = auto_notify_minutes(previous_config)
    for key in (PLUGIN_UPDATE_REPOSITORY_KEY, PLUGIN_UPDATE_REF_KEY):
        if previous_config.get(key):
            config[key] = previous_config[key]
    if args.auto_notify_minutes is not None:
        parsed_minutes = parse_minutes(args.auto_notify_minutes)
        if parsed_minutes is None:
            config.pop("auto_notify_min_minutes", None)
        else:
            config["auto_notify_min_minutes"] = parsed_minutes
    save_config(config)
    send_message("Telegram is linked. Codex can now send notifications here.", title="Codex Telegram setup")
    print(f"Linked chat {chat.get('id')} and sent a test message.")

    if args.install_hooks or (not args.no_hooks and ask_yes_no("Enable automatic task-completion notifications with Codex hooks?", default=True)):
        install_hooks()
        config = load_config()
        config["hooks_enabled"] = True
        save_config(config)
        print_hooks_restart_notice()
    else:
        print("Skipped hook installation. You can run `telegram.py install-hooks` later.")
    print()
    print("Try: ask Codex, 'Send me a Telegram when this task is done.'")
    return 0


def latest_update_offset(token: str) -> int | None:
    updates = api_call("getUpdates", {"timeout": "0", "limit": "100"}, token=token, timeout=10)
    if not isinstance(updates, list) or not updates:
        return None
    return max(int(update.get("update_id", 0)) for update in updates) + 1


def wait_for_chat(token: str, code: str, offset: int | None, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        payload: dict[str, Any] = {"timeout": "10", "limit": "25"}
        if offset is not None:
            payload["offset"] = str(offset)
        updates = api_call("getUpdates", payload, token=token, timeout=15)
        if not isinstance(updates, list):
            updates = []
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue
            if str(message.get("text", "")).strip() != code:
                continue
            chat = message.get("chat")
            if isinstance(chat, dict) and chat.get("id") is not None:
                return chat
        print(".", end="", flush=True)
    print()
    raise TelegramError("Timed out waiting for the setup code. Run setup again and send the new code to your bot.")


def status(_: argparse.Namespace) -> int:
    config = load_config()
    print_header("Telegram plugin status")
    update_source = update_source_status(config)
    if not is_linked(config):
        print("Not linked.")
        print(f"Update repository: {update_source['repository']}")
        print(f"Update ref: {update_source['ref'] or 'default branch'}")
        print(f"Config path: {config_path()}")
        if config:
            print(f"Auto-notify threshold: {auto_notify_status_text(config)}")
        return 1
    print(f"Bot: @{config.get('bot_username', 'unknown')}")
    print(f"Bot token: {redact(config.get('bot_token'))}")
    print(f"Chat: {config.get('chat_title') or config.get('chat_id')} ({config.get('chat_type', 'unknown')})")
    print(f"Hooks enabled: {'yes' if config.get('hooks_enabled') else 'no'}")
    print(f"Auto-notify threshold: {auto_notify_status_text(config)}")
    print(f"Update repository: {update_source['repository']}")
    print(f"Update ref: {update_source['ref'] or 'default branch'}")
    print(f"Config path: {config_path()}")
    return 0


def send_command(args: argparse.Namespace) -> int:
    result = send_message(args.message, title=args.title, disable_preview=not args.preview)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def test_command(_: argparse.Namespace) -> int:
    result = send_message("This is a test notification from Codex.", title="Codex Telegram test")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def clear(_: argparse.Namespace) -> int:
    path = config_path()
    if path.exists():
        path.unlink()
    print(f"Removed {path}")
    return 0


def pending_key(session_id: str | None, turn_id: str | None) -> str:
    base = f"{session_id or 'session'}-{turn_id or 'next'}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", base)[:180]


def save_pending(payload: dict[str, Any]) -> pathlib.Path:
    ensure_private_dir(pending_dir())
    key = pending_key(payload.get("session_id"), payload.get("turn_id"))
    path = pending_dir() / f"{key}.json"
    write_private_json(path, payload)
    return path


def notify_next(args: argparse.Namespace) -> int:
    payload = {
        "session_id": args.session_id,
        "turn_id": args.turn_id,
        "title": args.title or "Codex task complete",
        "prompt": args.prompt,
        "created_at": now_iso(),
        "created_epoch": time.time(),
        "source": "manual",
    }
    path = save_pending(payload)
    print(f"Telegram notification armed: {path}")
    return 0


def auto_notify_command(args: argparse.Namespace) -> int:
    if args.minutes is None:
        print(f"Auto-notify threshold: {auto_notify_status_text(load_config())}")
        print(f"Config path: {config_path()}")
        return 0
    minutes = parse_minutes(args.minutes)
    config = configure_auto_notify(minutes)
    if minutes is None:
        print("Auto-notify disabled.")
    else:
        print(f"Auto-notify enabled for Codex tasks taking {format_minutes(minutes)} minute(s) or longer.")
    if not is_linked(config):
        print("Telegram is not linked yet. Run setup before auto notifications can send.")
    elif not config.get("hooks_enabled"):
        print("Codex hooks are not marked enabled. Run install-hooks and restart Codex.")
    return 0


def hook_user_prompt(_: argparse.Namespace) -> int:
    payload = read_stdin_json()
    prompt = str(payload.get("prompt") or "")
    config = load_config()
    if not is_linked(config):
        return 0
    cwd = str(payload.get("cwd") or "")
    if should_ignore_prompt(prompt, cwd):
        return 0
    created_epoch = time.time()
    base_payload = {
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "prompt": prompt[:1000],
        "cwd": payload.get("cwd"),
        "created_at": now_iso(),
        "created_epoch": created_epoch,
    }
    if has_telegram_completion_intent(prompt):
        save_pending(
            {
                **base_payload,
                "title": "Codex task complete",
                "source": USER_PROMPT_SOURCE,
            }
        )
        return 0
    threshold_minutes = auto_notify_minutes(config)
    if threshold_minutes is None:
        return 0
    save_pending(
        {
            **base_payload,
            "title": "Codex task complete",
            "source": AUTO_NOTIFY_SOURCE,
            "threshold_minutes": threshold_minutes,
        }
    )
    return 0


def hook_stop(_: argparse.Namespace) -> int:
    payload = read_stdin_json()
    response = {"continue": True, "suppressOutput": True}
    try:
        sent = send_pending_for_stop(payload)
        if sent:
            response["systemMessage"] = "Telegram notification sent."
    except Exception as exc:
        response["systemMessage"] = f"Telegram notification failed: {exc}"
    print(json.dumps(response))
    return 0


def send_pending_for_stop(hook_payload: dict[str, Any]) -> bool:
    session_id = hook_payload.get("session_id")
    turn_id = hook_payload.get("turn_id")
    candidates = [
        pending_dir() / f"{pending_key(session_id, turn_id)}.json",
        pending_dir() / f"{pending_key(None, None)}.json",
    ]
    for pending_path in dict.fromkeys(candidates):
        if not pending_path.exists():
            continue
        pending = load_json(pending_path, {})
        if not should_send_pending(pending):
            pending_path.unlink(missing_ok=True)
            continue
        send_pending(pending_path, pending, hook_payload)
        return True
    return False


def should_send_pending(pending: dict[str, Any]) -> bool:
    if pending.get("source") == USER_PROMPT_SOURCE:
        prompt = str(pending.get("prompt") or "")
        cwd = str(pending.get("cwd") or "")
        return not should_ignore_prompt(prompt, cwd) and has_telegram_completion_intent(prompt)
    if pending.get("source") != AUTO_NOTIFY_SOURCE:
        return True
    threshold_minutes = auto_notify_minutes()
    if threshold_minutes is None:
        return False
    elapsed_minutes = elapsed_pending_minutes(pending)
    return elapsed_minutes is not None and elapsed_minutes >= threshold_minutes


def elapsed_pending_minutes(pending: dict[str, Any]) -> float | None:
    created_epoch = pending.get("created_epoch")
    try:
        return max(0.0, (time.time() - float(created_epoch)) / 60)
    except (TypeError, ValueError):
        pass
    created_at = pending.get("created_at")
    if not created_at:
        return None
    try:
        created = dt.datetime.fromisoformat(str(created_at))
        if created.tzinfo is None:
            created = created.astimezone()
        return max(0.0, (dt.datetime.now(created.tzinfo) - created).total_seconds() / 60)
    except ValueError:
        return None


def send_pending(pending_path: pathlib.Path, pending: dict[str, Any], hook_payload: dict[str, Any]) -> None:
    session_id = hook_payload.get("session_id")
    turn_id = hook_payload.get("turn_id")
    last_message = str(hook_payload.get("last_assistant_message") or "").strip()
    prompt = str(pending.get("prompt") or "").strip()
    cwd = str(hook_payload.get("cwd") or pending.get("cwd") or "").strip()
    duration = ""
    if pending.get("source") == AUTO_NOTIFY_SOURCE:
        elapsed_minutes = elapsed_pending_minutes(pending)
        threshold_minutes = auto_notify_minutes()
        if elapsed_minutes is not None and threshold_minutes is not None:
            duration = f"{format_duration(elapsed_minutes)} elapsed; threshold was {format_duration(threshold_minutes)}"
    rendered = render_completion_message(
        title=str(pending.get("title") or "Codex task complete"),
        request=single_line(prompt, 220) if prompt else "",
        workspace=cwd,
        duration=duration,
        result=single_line(last_message, COMPLETION_RESULT_LIMIT) if last_message else "",
    )
    config = load_config()
    chat_id = config.get("chat_id")
    if not chat_id:
        raise TelegramError("Telegram is not linked to a chat yet. Run setup first.")
    send_rendered_message(rendered, chat_id=chat_id)
    ensure_private_dir(sent_dir())
    sent_path = sent_dir() / f"{pending_path.stem}-{int(time.time())}.json"
    pending["sent_at"] = now_iso()
    pending["hook_session_id"] = session_id
    pending["hook_turn_id"] = turn_id
    write_private_json(sent_path, pending)
    pending_path.unlink(missing_ok=True)


def single_line(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def should_ignore_prompt(prompt: str, cwd: str | None = None) -> bool:
    stripped = prompt.lstrip()
    if any(stripped.startswith(prefix) for prefix in INTERNAL_PROMPT_PREFIXES):
        return True
    return False


def has_telegram_completion_intent(prompt: str) -> bool:
    if should_ignore_prompt(prompt):
        return False
    if not TELEGRAM_RE.search(prompt):
        return False
    if not NOTIFY_INTENT_RE.search(prompt):
        return False
    return bool(COMPLETION_CONTEXT_RE.search(prompt))


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def install_hooks_command(args: argparse.Namespace) -> int:
    install_hooks()
    config = load_config()
    if args.auto_notify_minutes is not None:
        config = configure_auto_notify(parse_minutes(args.auto_notify_minutes))
    if config:
        config["hooks_enabled"] = True
        save_config(config)
    print_hooks_restart_notice()
    return 0


def uninstall_hooks_command(_: argparse.Namespace) -> int:
    removed = remove_hooks()
    config = load_config()
    if config:
        config["hooks_enabled"] = False
        save_config(config)
    print(f"Removed {removed} Telegram hook handler(s).")
    return 0


def install_hooks() -> None:
    ensure_hooks_feature_enabled()
    hooks_path = codex_home() / "hooks.json"
    payload = load_json(hooks_path, {"hooks": {}})
    if not isinstance(payload, dict):
        payload = {"hooks": {}}
    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        payload["hooks"] = hooks = {}
    remove_telegram_hooks_from_payload(payload)
    script = pathlib.Path(__file__).resolve()
    python = pathlib.Path(sys.executable).resolve()
    add_hook(hooks, "UserPromptSubmit", f"{shell_quote(str(python))} {shell_quote(str(script))} hook-user-prompt", timeout=10)
    add_hook(hooks, "Stop", f"{shell_quote(str(python))} {shell_quote(str(script))} hook-stop", timeout=20)
    write_private_json(hooks_path, payload)


def print_hooks_restart_notice() -> None:
    print("Installed Codex hooks for Telegram completion notifications.")
    print("Restart Codex now so this running session loads the new hook configuration.")
    print("After restarting, try: Send me a Telegram when this task is done.")


def remove_hooks() -> int:
    hooks_path = codex_home() / "hooks.json"
    payload = load_json(hooks_path, {})
    if not isinstance(payload, dict):
        return 0
    removed = remove_telegram_hooks_from_payload(payload)
    if removed:
        write_private_json(hooks_path, payload)
    return removed


def remove_telegram_hooks_from_payload(payload: dict[str, Any]) -> int:
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                new_groups.append(group)
                continue
            kept = [handler for handler in handlers if not is_telegram_hook(handler)]
            removed += len(handlers) - len(kept)
            if kept:
                group = dict(group)
                group["hooks"] = kept
                new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            hooks.pop(event, None)
    return removed


def is_telegram_hook(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    command = str(handler.get("command") or "")
    return "telegram.py" in command and ("hook-user-prompt" in command or "hook-stop" in command)


def add_hook(hooks: dict[str, Any], event: str, command: str, timeout: int) -> None:
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        hooks[event] = groups = []
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})


def shell_quote(value: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def ensure_hooks_feature_enabled() -> None:
    config = codex_home() / "config.toml"
    ensure_private_dir(config.parent)
    if config.exists():
        text = config.read_text(encoding="utf-8")
    else:
        text = ""
    if re.search(r"(?m)^\s*codex_hooks\s*=\s*true\s*$", text):
        return
    if re.search(r"(?m)^\s*codex_hooks\s*=", text):
        text = re.sub(r"(?m)^(\s*)codex_hooks\s*=.*$", r"\1codex_hooks = true", text, count=1)
        config.write_text(text, encoding="utf-8")
        if os.name != "nt":
            config.chmod(0o600)
        return
    if re.search(r"(?m)^\s*\[features\]\s*$", text):
        text = re.sub(r"(?m)^(\s*\[features\]\s*)$", r"\1\ncodex_hooks = true", text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n[features]\ncodex_hooks = true\n"
    config.write_text(text, encoding="utf-8")
    if os.name != "nt":
        config.chmod(0o600)


class McpServer:
    def _reply(self, request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        return payload

    def _read_message(self) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("utf-8").partition(":")
            headers[name.lower()] = value.strip()
        body = sys.stdin.buffer.read(int(headers.get("content-length", "0")))
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def _write_message(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()

    def _tool_spec(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "telegram_status",
                "description": "Check whether Telegram is linked for this Codex profile.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "telegram_send_message",
                "description": "Send a Telegram message through the linked bot.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "telegram_notify_next",
                "description": "Arm a Telegram notification for the next Codex Stop hook.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "title": {"type": "string"},
                    },
                },
            },
            {
                "name": "telegram_install_hooks",
                "description": "Install user-local Codex hooks that send Telegram completion notifications.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "autoNotifyMinutes": {
                            "type": "number",
                            "description": "Optional threshold in minutes for automatic completion notifications. Use 0 to disable.",
                        }
                    },
                },
            },
            {
                "name": "telegram_configure_auto_notify",
                "description": "Configure automatic Telegram notifications for Codex tasks that take at least the given number of minutes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "minutes": {
                            "type": "number",
                            "description": "Threshold in minutes. Use 0 to disable automatic duration-based notifications.",
                        }
                    },
                    "required": ["minutes"],
                },
            },
            {
                "name": "telegram_check_plugin_update",
                "description": "Fetch and validate the configured Telegram plugin update source, then report changed files without copying.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repository": {"type": "string", "description": "Optional repository URL or local plugin directory."},
                        "ref": {"type": "string", "description": "Optional branch, tag, or commit."},
                    },
                },
            },
            {
                "name": "telegram_update_plugin",
                "description": "Update this installed Telegram plugin from the configured repository. Restart Codex afterward to reload changed plugin files.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repository": {"type": "string", "description": "Optional repository URL or local plugin directory."},
                        "ref": {"type": "string", "description": "Optional branch, tag, or commit."},
                        "saveSource": {"type": "boolean", "description": "Persist repository/ref as the default update source."},
                        "resetSource": {"type": "boolean", "description": "Clear saved source overrides before updating."},
                    },
                },
            },
        ]

    def _content(self, result: Any) -> dict[str, Any]:
        text = result if isinstance(result, str) else json.dumps(result, indent=2, sort_keys=True, default=str)
        return {"content": [{"type": "text", "text": text}]}

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "telegram_status":
            config = load_config()
            update_source = update_source_status(config)
            if not is_linked(config):
                return {
                    "linked": False,
                    "setup": setup_hint(),
                    "auto_notify_min_minutes": auto_notify_minutes(config),
                    "config_path": str(config_path()),
                    "plugin_update": update_source,
                }
            return {
                "linked": True,
                "bot_username": config.get("bot_username"),
                "chat_title": config.get("chat_title"),
                "chat_type": config.get("chat_type"),
                "hooks_enabled": bool(config.get("hooks_enabled")),
                "auto_notify_min_minutes": auto_notify_minutes(config),
                "config_path": str(config_path()),
                "plugin_update": update_source,
            }
        if name == "telegram_send_message":
            return send_message(str(arguments["message"]), title=arguments.get("title") or "Codex")
        if name == "telegram_notify_next":
            save_pending(
                {
                    "session_id": None,
                    "turn_id": None,
                    "title": arguments.get("title") or "Codex task complete",
                    "prompt": arguments.get("prompt") or "",
                    "created_at": now_iso(),
                    "created_epoch": time.time(),
                    "source": "MCP",
                }
            )
            return {"armed": True}
        if name == "telegram_install_hooks":
            install_hooks()
            if "autoNotifyMinutes" in arguments:
                configure_auto_notify(parse_minutes(arguments.get("autoNotifyMinutes")))
            config = load_config()
            if config:
                config["hooks_enabled"] = True
                save_config(config)
            return {
                "installed": True,
                "hooks_path": str(codex_home() / "hooks.json"),
                "auto_notify_min_minutes": auto_notify_minutes(config),
            }
        if name == "telegram_configure_auto_notify":
            config = configure_auto_notify(parse_minutes(arguments.get("minutes")))
            return {
                "auto_notify_min_minutes": auto_notify_minutes(config),
                "linked": is_linked(config),
                "hooks_enabled": bool(config.get("hooks_enabled")),
            }
        if name == "telegram_check_plugin_update":
            return perform_plugin_update(
                repository=arguments.get("repository"),
                ref=arguments.get("ref"),
                dry_run=True,
            )
        if name == "telegram_update_plugin":
            return perform_plugin_update(
                repository=arguments.get("repository"),
                ref=arguments.get("ref"),
                save_source=bool(arguments.get("saveSource")),
                reset_source=bool(arguments.get("resetSource")),
                dry_run=False,
            )
        raise TelegramError(f"Unknown tool: {name}")

    def serve(self) -> int:
        while True:
            request = self._read_message()
            if request is None:
                return 0
            request_id = request.get("id")
            method = request.get("method")
            try:
                if method == "initialize":
                    result = {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "telegram", "version": PLUGIN_VERSION},
                        "capabilities": {"tools": {}},
                    }
                elif method == "tools/list":
                    result = {"tools": self._tool_spec()}
                elif method == "tools/call":
                    params = request.get("params") or {}
                    result = self._content(self._call_tool(str(params.get("name")), params.get("arguments") or {}))
                elif method == "notifications/initialized":
                    continue
                else:
                    result = {}
                self._write_message(self._reply(request_id, result))
            except Exception as exc:
                self._write_message(
                    self._reply(
                        request_id,
                        error={"code": -32000, "message": str(exc), "data": {"type": exc.__class__.__name__}},
                    )
                )


def setup_hint() -> str:
    script = pathlib.Path(__file__).resolve()
    return f"Run `{sys.executable} {script} setup` and follow the Telegram linking prompts."


def mcp_serve(_: argparse.Namespace) -> int:
    return McpServer().serve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram.py", description="Telegram notifications for Codex.")
    sub = parser.add_subparsers(dest="command", required=True)

    setup_parser = sub.add_parser("setup", help="Guide first-time Telegram linking.")
    setup_parser.add_argument("--token", help="Bot token. Prefer the interactive prompt or TELEGRAM_BOT_TOKEN.")
    setup_parser.add_argument("--timeout", type=int, default=180, help="Seconds to wait for the Telegram setup code.")
    setup_parser.add_argument("--install-hooks", action="store_true", help="Install Codex completion hooks without prompting.")
    setup_parser.add_argument("--no-hooks", action="store_true", help="Skip the hook prompt.")
    setup_parser.add_argument(
        "--auto-notify-minutes",
        help="Automatically notify when any Codex task takes this many minutes or longer. Use 0 or off to disable.",
    )
    setup_parser.set_defaults(func=setup)

    status_parser = sub.add_parser("status", help="Show linked bot and hook status.")
    status_parser.set_defaults(func=status)

    send_parser = sub.add_parser("send", help="Send a Telegram message.")
    send_parser.add_argument("message")
    send_parser.add_argument("--title", default="Codex")
    send_parser.add_argument("--preview", action="store_true", help="Allow Telegram link previews.")
    send_parser.set_defaults(func=send_command)

    test_parser = sub.add_parser("test", help="Send a test notification.")
    test_parser.set_defaults(func=test_command)

    notify_parser = sub.add_parser("notify-next", help="Arm a notification for the next Stop hook.")
    notify_parser.add_argument("--prompt", default="")
    notify_parser.add_argument("--title", default="Codex task complete")
    notify_parser.add_argument("--session-id")
    notify_parser.add_argument("--turn-id")
    notify_parser.set_defaults(func=notify_next)

    auto_parser = sub.add_parser("auto-notify", help="Show or set the automatic task-duration notification threshold.")
    auto_parser.add_argument("minutes", nargs="?", help="Minutes threshold. Use 0 or off to disable.")
    auto_parser.set_defaults(func=auto_notify_command)

    update_parser = sub.add_parser("update-plugin", help="Update this installed Telegram plugin from its configured repository.")
    update_parser.add_argument("--repo", help="Repository URL or local plugin directory to use for this update.")
    update_parser.add_argument("--ref", help="Branch, tag, or commit to fetch. Defaults to the configured ref or repository default branch.")
    update_parser.add_argument("--save-source", action="store_true", help="Persist --repo and/or --ref as the default update source.")
    update_parser.add_argument("--reset-source", action="store_true", help="Clear the saved update source before resolving defaults.")
    update_parser.add_argument("--dry-run", action="store_true", help="Fetch and validate the update, then list changed files without copying.")
    update_parser.set_defaults(func=update_plugin)

    install_parser = sub.add_parser("install-hooks", help="Install user-local Codex hooks.")
    install_parser.add_argument(
        "--auto-notify-minutes",
        help="Automatically notify when any Codex task takes this many minutes or longer. Use 0 or off to disable.",
    )
    install_parser.set_defaults(func=install_hooks_command)

    uninstall_parser = sub.add_parser("uninstall-hooks", help="Remove user-local Telegram hooks.")
    uninstall_parser.set_defaults(func=uninstall_hooks_command)

    clear_parser = sub.add_parser("clear", help="Remove local Telegram link configuration.")
    clear_parser.set_defaults(func=clear)

    mcp_parser = sub.add_parser("mcp-serve", help="Run the Telegram MCP server over stdio.")
    mcp_parser.set_defaults(func=mcp_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    hidden_commands = {
        "hook-user-prompt": hook_user_prompt,
        "hook-stop": hook_stop,
    }
    if argv and argv[0] in hidden_commands:
        try:
            return int(hidden_commands[argv[0]](argparse.Namespace()))
        except Exception as exc:
            if argv[0] == "hook-stop":
                print(json.dumps({"continue": True, "suppressOutput": True, "systemMessage": f"Telegram hook failed: {exc}"}))
                return 0
            return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except TelegramError as exc:
        print(f"Telegram plugin error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
