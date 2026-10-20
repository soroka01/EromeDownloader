"""Settings of the Telegram mode: `telegram.json` (kept out of git), bot token and userbot credentials."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from erome.config import BASE_DIR
from erome.storage import write_text_atomic

SETTINGS_PATH = BASE_DIR / "telegram.json"
EXAMPLE_PATH = BASE_DIR / "telegram.example.json"
DEFAULT_SESSION_PATH = "data/telegram"
PLACEHOLDER_PREFIX = "PUT_"
# Values shipped in telegram.example.json (keep them in step with it): treated as "not filled in yet".
PLACEHOLDER_CHAT_ID = -1001234567890
PLACEHOLDER_OWNER_ID = 123456789
BOT_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")

DEFAULT_UPLOAD_CONNECTIONS = 12
MAX_UPLOAD_CONNECTIONS = 24

class SettingsError(Exception):
    """telegram.json is missing or incomplete; the message tells the user what to fix."""


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str
    owner_ids: frozenset[int]
    account_id: int | None
    chat_id: int
    thread_id: int
    api_id: int
    api_hash: str
    session_path: Path
    delete_after_upload: bool
    upload_connections: int


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SettingsError(f"Файл {path} не найден") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"Не удалось прочитать {path.name}: {exc}") from None
    if not isinstance(value, dict):
        raise SettingsError(f"{path.name} должен содержать объект JSON")
    return value


def _is_set(value: Any) -> bool:
    return bool(value) and not str(value).startswith(PLACEHOLDER_PREFIX)


def load_credentials(path: Path, visited: set[Path] | None = None) -> dict[str, Any]:
    """api_id/api_hash from another project's config (`telegram` block), following `credentials_config_path`."""
    resolved = path.resolve()
    seen = visited if visited is not None else set()
    if resolved in seen:
        raise SettingsError(f"credentials_config_path ссылается сам на себя: {resolved}")
    seen.add(resolved)
    source = _read_json(resolved)
    candidate = source.get("telegram", source)
    if not isinstance(candidate, dict):
        raise SettingsError(f"В {resolved.name} нет блока telegram")
    nested_path = candidate.get("credentials_config_path")
    if not nested_path:
        return candidate
    nested = load_credentials(resolve_path(resolved.parent, str(nested_path)), seen)
    return {**nested, **{key: value for key, value in candidate.items() if value is not None}}


def ensure_settings_file() -> bool:
    """Create telegram.json from telegram.example.json when it does not exist yet. Returns True if it was created."""
    if SETTINGS_PATH.exists():
        return False
    write_text_atomic(SETTINGS_PATH, json.dumps(_read_json(EXAMPLE_PATH), ensure_ascii=False, indent=2) + "\n")
    return True


def read_raw() -> dict[str, Any]:
    return _read_json(SETTINGS_PATH)


def bot_token_is_set(raw: dict[str, Any]) -> bool:
    return _is_set(raw.get("bot_token"))


def valid_bot_token(token: str) -> bool:
    return bool(BOT_TOKEN_RE.match(token.strip()))


def save_bot_token(token: str) -> None:
    raw = read_raw()
    raw["bot_token"] = token.strip()
    write_text_atomic(SETTINGS_PATH, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")


def _int_value(raw: dict[str, Any], key: str, *, required: bool = True) -> int | None:
    value = raw.get(key)
    if value in (None, ""):
        if required:
            raise SettingsError(f"В telegram.json не задано поле {key}")
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"Поле {key} в telegram.json должно быть целым числом") from None


def _owner_ids(raw: dict[str, Any]) -> frozenset[int]:
    value = raw.get("owner_ids")
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list) or not value:
        raise SettingsError("В telegram.json задайте owner_ids — список ID аккаунтов, которым разрешён бот")
    try:
        owners = frozenset(int(item) for item in value)
    except (TypeError, ValueError):
        raise SettingsError("owner_ids в telegram.json должен состоять из целых чисел") from None
    if owners == {PLACEHOLDER_OWNER_ID}:
        raise SettingsError("В telegram.json замените owner_ids образца на свой Telegram ID")
    return owners


def _api_credentials(raw: dict[str, Any]) -> tuple[int, str]:
    """api_id/api_hash from telegram.json, then TG_API_ID/TG_API_HASH, then `credentials_config_path`."""
    api_id = raw.get("api_id") if _is_set(raw.get("api_id")) else os.getenv("TG_API_ID")
    api_hash = raw.get("api_hash") if _is_set(raw.get("api_hash")) else os.getenv("TG_API_HASH")
    credentials_path = raw.get("credentials_config_path")
    if (not api_id or not api_hash) and credentials_path:
        inherited = load_credentials(resolve_path(BASE_DIR, str(credentials_path)))
        api_id = api_id or (inherited.get("api_id") if _is_set(inherited.get("api_id")) else None)
        api_hash = api_hash or (inherited.get("api_hash") if _is_set(inherited.get("api_hash")) else None)
    if not api_id or not api_hash:
        raise SettingsError(
            "Не заданы api_id и api_hash: впишите их в telegram.json (my.telegram.org), "
            "в TG_API_ID/TG_API_HASH или укажите credentials_config_path"
        )
    try:
        return int(api_id), str(api_hash)
    except (TypeError, ValueError):
        raise SettingsError("api_id в telegram.json должен быть целым числом") from None


def load_settings() -> TelegramSettings:
    raw = read_raw()
    token = str(raw.get("bot_token") or "").strip()
    if not (bot_token_is_set(raw) and valid_bot_token(token)):
        raise SettingsError("В telegram.json не задан токен бота (bot_token)")

    chat_id = _int_value(raw, "chat_id")
    if chat_id in (None, PLACEHOLDER_CHAT_ID):
        raise SettingsError("В telegram.json замените chat_id образца на ID своей группы")
    thread_id = _int_value(raw, "thread_id", required=False) or 0
    if thread_id < 0:
        raise SettingsError("thread_id в telegram.json не может быть отрицательным")
    account_id = _int_value(raw, "account_id", required=False)
    api_id, api_hash = _api_credentials(raw)

    connections = _int_value(raw, "upload_connections", required=False) or DEFAULT_UPLOAD_CONNECTIONS
    return TelegramSettings(
        bot_token=token,
        owner_ids=_owner_ids(raw),
        account_id=account_id,
        chat_id=chat_id,
        thread_id=thread_id,
        api_id=api_id,
        api_hash=api_hash,
        session_path=resolve_path(BASE_DIR, str(raw.get("session_path") or DEFAULT_SESSION_PATH)),
        delete_after_upload=bool(raw.get("delete_after_upload", True)),
        upload_connections=max(1, min(MAX_UPLOAD_CONNECTIONS, connections)),
    )


def settings_ready() -> bool:
    """True when telegram.json exists and holds a bot token (used by the menu banner)."""
    try:
        return bot_token_is_set(read_raw())
    except SettingsError:
        return False
