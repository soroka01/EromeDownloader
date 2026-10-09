import asyncio
import sys
import traceback

from erome.config import (
    AUTO_SORT_LINKS,
    CONFIG,
    DEFAULT_MAX_CONNECTIONS,
    DOWNLOADS_DIR,
    SORT_ALBUMS_BY_SIZE,
)
from erome.console import (
    configure_console,
    log_done,
    log_error,
    log_warn,
    with_hotkeys,
)
from erome.storage import (
    ensure_runtime_dirs,
    read_accounts,
    read_pending,
    status_file_path,
)
from erome.ui import plural, ui
from erome.urls import choose_parallel_album_count
from erome.workflow import (
    batch_download,
    batch_download_accounts,
    dump,
    scan_accounts_to_pending,
)

EXIT_KEYS = {"0", "q", "exit", "quit", "в", "выход"}

MENU = (
    ("1", "Одна ссылка", "альбом, прямой файл или аккаунт"),
    ("2", "Очередь", "скачать всё из links/pending.txt"),
    ("3", "Аккаунты", "проверить links/accs.txt и скачать новые альбомы"),
    ("4", "Поиск новых", "найти новые альбомы аккаунтов и добавить в pending.txt"),
    ("0", "Выход", ""),
)


def on_off(value: bool) -> str:
    return "вкл" if value else "выкл"


def queue_summary() -> str:
    try:
        pending = len(read_pending()) if status_file_path("pending").exists() else 0
        accounts = len(read_accounts()) if status_file_path("accs").exists() else 0
    except OSError:
        return "недоступна"
    return (
        f"{pending} {plural(pending, 'ссылка', 'ссылки', 'ссылок')} в очереди · "
        f"{accounts} {plural(accounts, 'аккаунт', 'аккаунта', 'аккаунтов')} в списке"
    )


def show_banner() -> None:
    skip_videos, skip_images = bool(CONFIG["skip_videos"]), bool(CONFIG["skip_images"])
    if skip_videos and skip_images:
        media = "ничего (включены оба фильтра)"
    elif skip_videos:
        media = "только фото"
    elif skip_images:
        media = "только видео"
    else:
        media = "видео и фото"
    parallel_albums = choose_parallel_album_count(99, DEFAULT_MAX_CONNECTIONS)
    ui.title("EromeDownloader  ·  github.com/soroka01")
    ui.field("Загрузки", DOWNLOADS_DIR)
    ui.field("Соединения", f"{DEFAULT_MAX_CONNECTIONS} · до {parallel_albums} альбомов параллельно")
    ui.field(
        "Сортировка",
        f"ссылки: {on_off(AUTO_SORT_LINKS)} · альбомы по весу: {on_off(SORT_ALBUMS_BY_SIZE)}",
    )
    ui.field("Скачивать", media)
    ui.field("Очередь", queue_summary())


def show_menu() -> None:
    ui.title("Главное меню")
    for key, label, hint in MENU:
        ui.line(f"  {ui.paint(key, 'key')}  {label:<14}{ui.paint(hint, 'dim')}")


def run_single_link() -> None:
    url = ui.ask("Ссылка (альбом, прямой файл или аккаунт)")
    if not url:
        ui.line("  Ссылка не введена.", "warn")
        return
    result = asyncio.run(
        with_hotkeys(
            dump(
                url,
                DEFAULT_MAX_CONNECTIONS,
                bool(CONFIG["skip_videos"]),
                bool(CONFIG["skip_images"]),
            )
        )
    )
    if result == "success":
        log_done("Готово.")
    elif result == "banned":
        log_warn("Ссылка недоступна.")
    elif result == "stopped":
        log_warn("Остановлено по запросу. Повторите ссылку: готовые альбомы будут пропущены.")
    else:
        log_error("Завершено с ошибкой.")


def run_queue() -> None:
    asyncio.run(
        with_hotkeys(
            batch_download(
                read_pending(),
                DEFAULT_MAX_CONNECTIONS,
                bool(CONFIG["skip_videos"]),
                bool(CONFIG["skip_images"]),
                AUTO_SORT_LINKS,
            )
        )
    )


def run_accounts() -> None:
    asyncio.run(
        with_hotkeys(
            batch_download_accounts(
                read_accounts(),
                DEFAULT_MAX_CONNECTIONS,
                skip_videos=False,
                skip_images=False,
                sort_links=AUTO_SORT_LINKS,
            )
        )
    )


def run_scan() -> None:
    asyncio.run(
        with_hotkeys(scan_accounts_to_pending(read_accounts(), sort_links=AUTO_SORT_LINKS))
    )


def attempt(action) -> None:
    """Run one menu action; failures and Ctrl+C return to the menu instead of closing the program."""
    try:
        action()
    except KeyboardInterrupt:
        ui.idle()
        log_warn("Прервано (Ctrl+C). Незавершённые загрузки продолжатся при следующем запуске")
    except Exception as exc:
        ui.idle()
        log_error(f"{type(exc).__name__}: {exc}")
        ui.line(traceback.format_exc().rstrip(), "dim")


def main() -> int:
    configure_console()
    ui.set_title("EromeDownloader")
    ensure_runtime_dirs()
    actions = {
        "1": run_single_link,
        "2": run_queue,
        "3": run_accounts,
        "4": run_scan,
    }
    show_banner()
    show_menu()
    while True:
        try:
            choice = ui.ask("Выберите действие", "1").lower()
        except (EOFError, KeyboardInterrupt):
            ui.line()
            return 0
        if choice in EXIT_KEYS:
            return 0
        if choice not in actions:
            ui.line("  Нет такого пункта. Введите число из меню.", "warn")
            continue
        attempt(actions[choice])
        show_menu()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log_done("Остановлено пользователем.", "warn")
