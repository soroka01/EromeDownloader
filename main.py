import asyncio
import sys
import traceback
from typing import Callable

from erome.config import (
    AUTO_SORT_LINKS,
    DEFAULT_MAX_CONNECTIONS,
    DOWNLOADS_DIR,
    SKIP_IMAGES,
    SKIP_VIDEOS,
    SORT_ALBUMS_BY_SIZE,
)
from erome.console import (
    configure_console,
    log,
    log_error,
    log_success,
    log_warn,
    with_hotkeys,
)
from erome.manifest import flush_manifest
from erome.models import Outcome
from erome.storage import (
    ensure_runtime_dirs,
    read_accounts,
    read_pending,
    reset_queue_cache,
    status_file_path,
)
from erome.tg_settings import settings_ready
from erome.ui import plural, ui
from erome.urls import choose_parallel_album_count
from erome.workflow import (
    batch_download,
    batch_download_accounts,
    download_link,
    scan_accounts_to_pending,
)

EXIT_KEYS = {"0", "q", "exit", "quit", "в", "выход"}

MENU = (
    ("1", "Одна ссылка", "альбом, прямой файл или аккаунт"),
    ("2", "Очередь", "скачать всё из links/pending.txt"),
    ("3", "Аккаунты", "проверить links/accs.txt и скачать новые альбомы"),
    ("4", "Поиск новых", "найти новые альбомы аккаунтов и добавить в pending.txt"),
    ("5", "Telegram-бот", "ссылки из чата с ботом → скачать → отправить в группу"),
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
    if SKIP_VIDEOS and SKIP_IMAGES:
        media = "ничего (включены оба фильтра)"
    elif SKIP_VIDEOS:
        media = "только фото"
    elif SKIP_IMAGES:
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
    ui.field("Telegram", "бот настроен (пункт 5)" if settings_ready() else "не настроен (пункт 5)")


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
            download_link(url, DEFAULT_MAX_CONNECTIONS, SKIP_VIDEOS, SKIP_IMAGES)
        )
    )
    if result == Outcome.SUCCESS:
        log_success("Готово.")
    elif result == Outcome.BANNED:
        log_warn("Ссылка недоступна.")
    elif result == Outcome.STOPPED:
        log_warn("Остановлено по запросу. Повторите ссылку: готовые альбомы будут пропущены.")
    else:
        log_error("Завершено с ошибкой.")


def run_queue() -> None:
    asyncio.run(
        with_hotkeys(
            batch_download(
                read_pending(),
                DEFAULT_MAX_CONNECTIONS,
                SKIP_VIDEOS,
                SKIP_IMAGES,
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


def run_telegram() -> bool:
    try:
        from erome.tg_mode import run_telegram_bot  # on demand: aiogram and Telethon are needed only here
    except ImportError as exc:
        log_error(
            f"Для режима 5 не хватает пакета «{exc.name}». Запустите start.bat "
            "или выполните: python -m pip install -r requirements.txt"
        )
        return False
    return run_telegram_bot()


def attempt(action: Callable[[], bool | None]) -> bool:
    """Run one menu action; failures and Ctrl+C return to the menu instead of closing the program.

    True when the action did its job; an action that could not says so by returning False.
    """
    reset_queue_cache()  # links/*.txt may have been edited by hand while the menu was open
    try:
        return action() is not False
    except KeyboardInterrupt:
        ui.idle()
        log_warn("Прервано (Ctrl+C). Незавершённые загрузки продолжатся при следующем запуске")
    except Exception as exc:
        ui.idle()
        log_error(f"{type(exc).__name__}: {exc}")
        ui.line(traceback.format_exc().rstrip(), "dim")
    finally:
        flush_manifest()
    return False


def run_chosen(args: list[str], actions: dict[str, Callable[[], bool | None]]) -> int:
    """The program was started with a menu item (start.bat 5): do it once and leave instead of showing the menu, so
    whatever started it (a script manager, a scheduler) sees it end. Exit code: 0 done, 1 not done, 2 unknown item."""
    choice = args[0].lower()
    if choice in EXIT_KEYS:
        return 0
    if choice not in actions:
        ui.line(f"  Неизвестный параметр «{args[0]}». Укажите номер пункта меню, например: start.bat 5", "warn")
        return 2
    if len(args) > 1:
        ui.line(f"  Лишние параметры игнорируются: {' '.join(args[1:])}", "warn")
    return 0 if attempt(actions[choice]) else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    configure_console()
    ui.set_title("EromeDownloader")
    ensure_runtime_dirs()
    actions = {
        "1": run_single_link,
        "2": run_queue,
        "3": run_accounts,
        "4": run_scan,
        "5": run_telegram,
    }
    show_banner()
    if args:
        return run_chosen(args, actions)
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
        log("Остановлено пользователем.", "warn")
