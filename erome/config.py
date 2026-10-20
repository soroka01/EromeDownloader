import json
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


DOWNLOADS_DIR = BASE_DIR / "downloads"


LINKS_DIR = BASE_DIR / "links"


CONFIG_PATH = BASE_DIR / "config.json"


DEFAULT_CONFIG = {
    "max_connections": 6,
    "min_connections_per_parallel_album": 3,
    "max_parallel_albums": 3,
    "auto_sort_links": True,
    "album_prefetch_connections": 6,
    "account_page_connections": 3,
    "account_max_pages": 500,
    "sort_albums_by_size": True,
    "album_size_probe_connections": 3,
    "album_size_probe_timeout": 20,
    "chunk_size_mb": 1,
    "max_attempts": 4,
    "page_attempts": 3,
    "connect_timeout": 30,
    "idle_timeout": 180,
    "skip_videos": False,
    "skip_images": False,
    "manifest_path": "links/manifest.json",
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.write_text(
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            print(f"[WARN] Не удалось создать config.json: {error}. Использую настройки по умолчанию.")
        return DEFAULT_CONFIG.copy()

    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"[WARN] config.json не читается: {error}. Использую настройки по умолчанию.")
        return DEFAULT_CONFIG.copy()

    config = DEFAULT_CONFIG.copy()
    if not isinstance(loaded, dict):
        print("[WARN] config.json должен быть JSON-объектом. Использую настройки по умолчанию.")
        return config
    for key in loaded:
        if key not in DEFAULT_CONFIG:
            print(f"[WARN] config.json: неизвестный параметр «{key}» — игнорирую.")
    config.update(loaded)
    return config


CONFIG = load_config()


def _int_setting(name: str, minimum: int, maximum: int) -> int:
    """Целое из config.json: не число — значение по умолчанию, число вне диапазона — ближайшее допустимое."""
    default = DEFAULT_CONFIG[name]
    value = CONFIG.get(name, default)
    try:
        number = None if isinstance(value, bool) else int(value)
    except (TypeError, ValueError, OverflowError):
        number = None
    if number is None:
        print(f"[WARN] config.json: «{name}» = {value!r} не число. Использую {default}.")
        return default
    if not minimum <= number <= maximum:
        nearest = min(max(number, minimum), maximum)
        print(f"[WARN] config.json: «{name}» = {value!r} вне диапазона {minimum}…{maximum}. Использую {nearest}.")
        return nearest
    return number


def _bool_setting(name: str) -> bool:
    """true/false из config.json; строки вроде "false" и числа 0/1 тоже понимаются, остальное — по умолчанию."""
    default = DEFAULT_CONFIG[name]
    value = CONFIG.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "on", "1", "да", "вкл"):
            return True
        if text in ("false", "no", "off", "0", "нет", "выкл"):
            return False
    print(f"[WARN] config.json: «{name}» = {value!r} не подходит (нужно true или false). Использую {default}.")
    return default


def _path_setting(name: str) -> str:
    default = DEFAULT_CONFIG[name]
    value = CONFIG.get(name, default)
    if isinstance(value, str) and value.strip():
        return value.strip()
    print(f"[WARN] config.json: «{name}» = {value!r} не подходит (нужен путь). Использую {default}.")
    return default


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


EROME_HOSTS = {"erome.com", "www.erome.com"}


ALBUM_URL_RE = re.compile(
    r"(https?://(?:www\.)?erome\.com/a/[^\s\"'<>/?#]+)",
    re.IGNORECASE,
)


ACCOUNT_EXCLUDED_PREFIXES = (
    "/a/",
    "/about",
    "/contact",
    "/explore",
    "/latest",
    "/popular",
    "/privacy",
    "/search",
    "/terms",
    "/user/",
)


ACCOUNT_EXCLUDED_PATHS = {"/a"}


CHUNK_SIZE = _int_setting("chunk_size_mb", 1, 1024) * 1024 * 1024


MAX_ATTEMPTS = _int_setting("max_attempts", 1, 100)


PAGE_ATTEMPTS = _int_setting("page_attempts", 1, 100)


ACCOUNT_PAGE_CONNECTIONS = _int_setting("account_page_connections", 1, 256)


ACCOUNT_MAX_PAGES = _int_setting("account_max_pages", 1, 100_000)


DEFAULT_MAX_CONNECTIONS = _int_setting("max_connections", 1, 256)


MIN_CONNECTIONS_PER_PARALLEL_ALBUM = _int_setting("min_connections_per_parallel_album", 1, 256)


MAX_PARALLEL_ALBUMS = _int_setting("max_parallel_albums", 1, 256)


AUTO_SORT_LINKS = _bool_setting("auto_sort_links")


ALBUM_PREFETCH_CONNECTIONS = _int_setting("album_prefetch_connections", 1, 256)


SORT_ALBUMS_BY_SIZE = _bool_setting("sort_albums_by_size")


ALBUM_SIZE_PROBE_CONNECTIONS = _int_setting("album_size_probe_connections", 1, 256)


ALBUM_SIZE_PROBE_TIMEOUT = _int_setting("album_size_probe_timeout", 1, 86_400)


CONNECT_TIMEOUT = _int_setting("connect_timeout", 1, 86_400)


IDLE_TIMEOUT = _int_setting("idle_timeout", 1, 86_400)


SKIP_VIDEOS = _bool_setting("skip_videos")


SKIP_IMAGES = _bool_setting("skip_images")


MANIFEST_PATH = _path_setting("manifest_path")


RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 522, 524}


UNAVAILABLE_STATUSES = {403, 404, 410}


STATUS_FILES = ("ready", "failed", "banned")


ACCOUNT_STATUS_FILES = ("ready_accs", "failed_accs", "banned_accs")


MAX_LOG_MESSAGE = 180
