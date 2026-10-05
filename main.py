import asyncio

from colorama import Fore, Style

from erome.config import AUTO_SORT_LINKS, CONFIG, DEFAULT_MAX_CONNECTIONS, SORT_ALBUMS_BY_SIZE
from erome.console import configure_console, log, log_done, log_error, print_boxed
from erome.storage import ensure_runtime_dirs, read_accounts, read_pending
from erome.urls import choose_parallel_album_count
from erome.workflow import (
    batch_download,
    batch_download_accounts,
    dump,
    scan_accounts_to_pending,
)


def ask_mode() -> str:
    while True:
        mode = input(Fore.GREEN + "\nРежим [1-4]: " + Style.RESET_ALL).strip()
        if mode in {"1", "2", "3", "4"}:
            return mode
        log_error("Введите 1, 2, 3 или 4.")


def main() -> None:
    configure_console()
    ensure_runtime_dirs()

    print_boxed("EromeDownloader\nby github.com/soroka01", Fore.MAGENTA)
    print(Fore.YELLOW + "\nДобро пожаловать. Выберите режим работы.")
    print(Fore.GREEN + "\nРежимы:")
    print(
        Fore.CYAN
        + "  1. Скачать одну ссылку: альбом, прямой файл или аккаунт\n"
        + "  2. Скачать все ссылки из links/pending.txt\n"
        + "  3. Проверить аккаунты из links/accs.txt\n"
        + "  4. Найти новые посты аккаунтов и добавить в pending.txt\n"
    )
    print(Fore.WHITE + "Файлы:")
    print(Fore.WHITE + "  links/pending.txt - альбомы, прямые файлы и аккаунты")
    print(Fore.WHITE + "  links/accs.txt   - постоянный список аккаунтов")
    print(
        Fore.WHITE
        + "  Статусы          - ready/failed/banned и ready_accs/failed_accs/banned_accs"
    )

    mode = ask_mode()
    skip_videos = bool(CONFIG["skip_videos"])
    skip_images = bool(CONFIG["skip_images"])
    sort_links = AUTO_SORT_LINKS
    max_connections = DEFAULT_MAX_CONNECTIONS
    parallel_albums = choose_parallel_album_count(99, max_connections)
    log(
        f"{max_connections} соединений, до {parallel_albums} альбомов параллельно, "
        f"сортировка {'включена' if sort_links else 'выключена'}, "
        f"вес альбомов {'включён' if SORT_ALBUMS_BY_SIZE else 'выключен'}",
        Fore.WHITE,
        "CONFIG",
    )

    if mode == "1":
        url = input(Fore.CYAN + "Введите ссылку: " + Style.RESET_ALL).strip()
        result = asyncio.run(dump(url, max_connections, skip_videos, skip_images))
        if result == "success":
            log_done("Готово.")
        elif result == "banned":
            log_done("Ссылка недоступна.", Fore.YELLOW)
        else:
            log_done("Завершено с ошибкой.", Fore.RED)
        return

    if mode == "3":
        asyncio.run(
            batch_download_accounts(
                read_accounts(),
                max_connections,
                skip_videos=False,
                skip_images=False,
                sort_links=sort_links,
            )
        )
        return

    if mode == "4":
        asyncio.run(scan_accounts_to_pending(read_accounts(), sort_links=sort_links))
        return

    asyncio.run(
        batch_download(
            read_pending(),
            max_connections,
            skip_videos,
            skip_images,
            sort_links,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_done("Остановлено пользователем.", Fore.YELLOW)
