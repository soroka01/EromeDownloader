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
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return DEFAULT_CONFIG.copy()

    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"[WARN] config.json повреждён: {error}. Использую настройки по умолчанию.")
        return DEFAULT_CONFIG.copy()

    config = DEFAULT_CONFIG.copy()
    if isinstance(loaded, dict):
        config.update(loaded)
    return config


CONFIG = load_config()


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


CHUNK_SIZE = max(1, int(CONFIG["chunk_size_mb"])) * 1024 * 1024


MAX_ATTEMPTS = int(CONFIG["max_attempts"])


PAGE_ATTEMPTS = int(CONFIG["page_attempts"])


ACCOUNT_PAGE_CONNECTIONS = int(CONFIG["account_page_connections"])


ACCOUNT_MAX_PAGES = int(CONFIG["account_max_pages"])


DEFAULT_MAX_CONNECTIONS = int(CONFIG["max_connections"])


MIN_CONNECTIONS_PER_PARALLEL_ALBUM = int(CONFIG["min_connections_per_parallel_album"])


MAX_PARALLEL_ALBUMS = int(CONFIG["max_parallel_albums"])


AUTO_SORT_LINKS = bool(CONFIG["auto_sort_links"])


ALBUM_PREFETCH_CONNECTIONS = int(CONFIG["album_prefetch_connections"])


SORT_ALBUMS_BY_SIZE = bool(CONFIG["sort_albums_by_size"])


ALBUM_SIZE_PROBE_CONNECTIONS = int(CONFIG["album_size_probe_connections"])


ALBUM_SIZE_PROBE_TIMEOUT = int(CONFIG["album_size_probe_timeout"])


CONNECT_TIMEOUT = int(CONFIG["connect_timeout"])


IDLE_TIMEOUT = int(CONFIG["idle_timeout"])


RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 522, 524}


UNAVAILABLE_STATUSES = {403, 404, 410}


STATUS_FILES = ("ready", "failed", "banned")


ACCOUNT_STATUS_FILES = ("ready_accs", "failed_accs", "banned_accs")


BAR_FORMAT = "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]"


GROUP_BAR_FORMAT = "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}, {rate_fmt}{postfix}]"


MAX_PROGRESS_WIDTH = 112


MAX_LOG_MESSAGE = 180
