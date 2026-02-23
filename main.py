import argparse
import asyncio
import sys
import re
import os
import time
import platform
import aiohttp
import aiofiles
from colorama import Fore, Style, init
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from tqdm.asyncio import tqdm, tqdm_asyncio
from pathlib import Path

def print_boxed(text, color=Fore.CYAN):
    lines = text.split('\n')
    width = max(len(line) for line in lines)
    print(color + '┌' + '─'*width + '┐')
    for line in lines:
        print(color + '│' + line.ljust(width) + '│')
    print(color + '└' + '─'*width + '┘' + Style.RESET_ALL)

USER_AGENT = "Mozilla/5.0"
HOST = "www.erome.com"
CHUNK_SIZE = 65536
DELETED_MARKER = "__DELETED__"

def status_file_path(name: str) -> str:
    return os.path.join(os.path.dirname(__file__), "links", f"{name}.txt")

def add_status(url: str, status: str):
    path = status_file_path(status)
    with open(path, "a", encoding="utf-8") as f:
        f.write(url+"\n")

def write_pending(links):
    path = status_file_path("pending")
    with open(path, "w", encoding="utf-8") as f:
        for url in links:
            url = url.strip()
            if url and not url.startswith('#'):
                f.write(url+"\n")
    
def clear_console():
    if platform.system() == "Windows":
        os.system("cls")
    else:
        os.system("clear")

def _clean_album_title(title: str, default_title="temp") -> str:
    """Remove illegal characters from the album title"""
    illegal_chars = r'[\\/:*?"<>|]'
    title = re.sub(illegal_chars, "_", title)
    title = title.strip(". ")
    return title if title else default_title

def _get_final_download_path(album_title: str) -> Path:
    """Create a directory with the title of the album"""
    final_path = Path("downloads") / album_title
    if not final_path.exists():
        final_path.mkdir(parents=True)
    return final_path

async def dump(
    url: str,
    max_connections: int,
    skip_videos: bool,
    skip_images: bool,
    album_idx: int = None,
    album_total: int = None,
):
    """Collect album data and download the album"""
    parsed = urlparse(url)
    # 🔹 Прямой файл (не альбом)
    if parsed.hostname != HOST:
        file_name = Path(parsed.path).name
        download_path = Path("downloads")
        download_path.mkdir(exist_ok=True)
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            try:
                async with session.get(url, timeout=ClientTimeout(total=10)) as r:
                    if r.ok:
                        total_size = int(r.headers.get("content-length", 0))
                        file_path = download_path / file_name
                        progress = tqdm(
                            desc=f"{file_name} (direct)",
                            total=total_size,
                            unit="B",
                            unit_scale=True,
                            unit_divisor=CHUNK_SIZE,
                            colour="YELLOW",
                            leave=False,
                            ncols=120,
                        )
                        async with aiofiles.open(file_path, "wb") as f:
                            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                                progress.update(await f.write(chunk))
                        progress.close()
                        return "success"
                    if r.status == 403:
                        tqdm.write(f"[INFO] {url} удалён / запрещён (403)")
                        return "banned"
                    tqdm.write(f"[ERROR] {url} status {r.status}")
                    return "fail"
            except Exception as e:
                tqdm.write(f"[ERROR] {url}: {e}")
                return "fail"
    # 🔹 Альбом
    title, urls = await _collect_album_data(
        url=url,
        skip_videos=skip_videos,
        skip_images=skip_images,
    )
    download_path = _get_final_download_path(title)
    desc = f"[{album_idx}/{album_total}] Album Progress" if album_idx else "Album Progress"
    await _download(
        album=url,
        urls=urls,
        max_connections=max_connections,
        download_path=download_path,
        desc=desc,
    )
    # ✅ КРИТИЧНО: возвращаем success
    return "success"

async def _download(
    album: str,
    urls: list[str],
    max_connections: int,
    download_path: Path,
    desc: str = "Album Progress"
):
    """Download the album"""
    semaphore = asyncio.Semaphore(max_connections)
    async with aiohttp.ClientSession(
        headers={"Referer": album, "User-Agent": USER_AGENT},
        timeout=ClientTimeout(total=None),
    ) as session:
        tasks = [
            _download_file(
                session=session,
                url=url,
                semaphore=semaphore,
                download_path=download_path,
            )
            for url in urls
        ]
        await tqdm_asyncio.gather(
            *tasks,
            colour="MAGENTA",
            desc=desc,
            unit="file",
            leave=True,
        )

async def _download_file(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore,
    download_path: Path,
):
    """Download file with idle-timeout (no data for 180s)"""
    max_attempts = 3
    request_timeout = 180
    idle_timeout = 180
    for attempt in range(1, max_attempts + 1):
        last_data_time = time.monotonic()
        try:
            async with semaphore:
                async with session.get(
                    url,
                    timeout=ClientTimeout(total=request_timeout),
                ) as r:
                    if not r.ok:
                        tqdm.write(f"[ERROR] {url} status {r.status}")
                        continue
                    file_name = Path(urlparse(url).path).name
                    total_size = int(r.headers.get("content-length", 0))
                    file_path = download_path / file_name
                    # Уже скачан
                    if file_path.exists() and total_size > 0:
                        if abs(file_path.stat().st_size - total_size) <= 50:
                            tqdm.write(f"[SKIP] {file_name} already downloaded")
                            return "success"
                    progress = tqdm(
                        desc=f"[{download_path.name}] {file_name}",
                        total=total_size,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=CHUNK_SIZE,
                        colour="MAGENTA",
                        leave=False,
                        ncols=120,
                        ascii=True,
                    )
                    try:
                        async with aiofiles.open(file_path, "wb") as f:
                            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                                now = time.monotonic()
                                if now - last_data_time > idle_timeout:
                                    raise asyncio.TimeoutError("idle timeout")
                                if chunk:
                                    last_data_time = now
                                    progress.update(await f.write(chunk))
                        return "success"
                    finally:
                        progress.close()
        except asyncio.TimeoutError:
            tqdm.write(f"[TIMEOUT] {url} idle {idle_timeout}s ({attempt}/{max_attempts})")
        except Exception as e:
            tqdm.write(f"[ERROR] {url}: {e} ({attempt}/{max_attempts})")
    return "failed"

async def _collect_album_data(
    url: str, skip_videos: bool, skip_images: bool
) -> tuple[str, list[str]]:
    """Collect videos and images from the album"""
    headers = {"User-Agent": USER_AGENT}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as response:
            html_content = await response.text()
            soup = BeautifulSoup(html_content, "html.parser")
            meta_title = soup.find("meta", property="og:title")
            if meta_title and meta_title.has_attr("content"):
                album_title = _clean_album_title(meta_title["content"])
            else:
                album_title = _clean_album_title("temp")
            videos = []
            if not skip_videos:
                for video_source in soup.find_all("source"):
                    src = video_source.get("src")
                    if src:
                        videos.append(src)
            images = []
            if not skip_images:
                for image in soup.find_all("img", {"class": "img-back"}):
                    data_src = image.get("data-src")
                    if data_src:
                        images.append(data_src)
            album_urls = list({*videos, *images})
            return album_title, album_urls

if __name__ == "__main__":
    def ask_bool(prompt):
        while True:
            ans = input(f"{prompt} (y/n): ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            print("Введите y или n.")

    def ask_int(prompt, default=None):
        while True:
            ans = input(f"{prompt} [{default}]: ").strip()
            if not ans and default is not None:
                return default
            try:
                return int(ans)
            except ValueError:
                print("Введите число.")

    init(autoreset=True)

    def print_boxed(text, color=Fore.CYAN):
        lines = text.split("\n")
        width = max(len(line) for line in lines)
        print(color + "┌" + "─" * width + "┐")
        for line in lines:
            print(color + "│" + line.ljust(width) + "│")
        print(color + "└" + "─" * width + "┘" + Style.RESET_ALL)

    print_boxed("EromeDownloader\nby github.com/soroka01", Fore.MAGENTA)
    print(Fore.YELLOW + "\nДобро пожаловать!\nWelcome!")
    print(Fore.GREEN + "\nМеню:")
    print(
        Fore.CYAN
        + "  1. Скачать один альбом по ссылке\n"
        + "  2. Скачать все альбомы из links/pending.txt\n"
    )
    print(Fore.WHITE + "Для batch-режима добавьте ссылки в links/pending.txt")
    print(Fore.WHITE + "Статусы: ready.txt — успешно, failed.txt — ошибки")

    while True:
        mode = input(Fore.GREEN + "\nВыберите режим (1/2): " + Style.RESET_ALL).strip()
        if mode in ("1", "2"):
            break
        print(Fore.RED + "Введите 1 или 2.")

    skip_videos = ask_bool("Пропустить видео?") if mode == "1" else False
    skip_images = ask_bool("Пропустить изображения?") if mode == "1" else False
    sort_links = ask_bool("Отсортировать ссылки перед загрузкой?") if mode == "2" else False
    max_connections = ask_int("Максимум одновременных соединений?", default=5)

    async def batch_download(links, max_connections, skip_videos, skip_images, sort_links):
        def normalize(url):
            m = re.search(r"(https?://(?:www\.)?erome\.com/a/\w+)", url.strip())
            return m.group(1) if m else url.strip()

        # Нормализация и базовая очистка
        raw_links = [normalize(u) for u in links if u.strip() and not u.startswith("#")]
        # Удаляем дубликаты (сохраняя порядок, если сортировка не включена)
        seen = set()
        unique_links = []
        for url in raw_links:
            if url not in seen:
                seen.add(url)
                unique_links.append(url)

        valid_links = unique_links

        # Сортировка, если выбрана
        if sort_links:
            valid_links.sort()
            print(Fore.CYAN + "[INFO] Ссылки отсортированы.")

        # Записываем все ссылки в pending (они будут загружаться)
        pending_set = set(valid_links)
        write_pending(list(pending_set))

        print(Fore.GREEN + f"[INFO] Загружаем {len(valid_links)} ссылок")

        failed_urls = []
        current_connections = max_connections
        success_streak = 0
        total = len(valid_links)

        for idx, url in enumerate(valid_links, 1):
            print(Fore.CYAN + f"\n[{idx}/{total}] {url}")
            try:
                result = await dump(
                    url,
                    current_connections,
                    skip_videos,
                    skip_images,
                    album_idx=idx,
                    album_total=total,
                )
                if result == "success":
                    add_status(url, "ready")
                    success_streak += 1
                elif result == "banned":
                    success_streak = 0
                else:
                    success_streak = 0
                    failed_urls.append(url)
                pending_set.discard(url)
            except Exception as e:
                print(Fore.RED + f"[ERROR] {url}: {e}")
                failed_urls.append(url)
                pending_set.discard(url)
                success_streak = 0

            if success_streak >= 3 and current_connections < max_connections * 2:
                current_connections += 1
                success_streak = 0
                print(Fore.GREEN + f"[AUTO] connections -> {current_connections}")

        # Обновляем pending.txt – убираем обработанные ссылки
        write_pending(list(pending_set))

        # Повторная попытка для упавших
        if failed_urls:
            print(Fore.YELLOW + f"\n[RETRY] {len(failed_urls)} неудачных")
            for url in failed_urls:
                result = await dump(
                    url,
                    max_connections,
                    skip_videos,
                    skip_images,
                )
                if result == "success":
                    add_status(url, "ready")
                else:
                    add_status(url, "failed")

    if mode == "1":
        url = input(Fore.CYAN + "Введите ссылку: " + Style.RESET_ALL).strip()
        asyncio.run(dump(url, max_connections, skip_videos, skip_images))
    else:
        pending_path = os.path.join("links", "pending.txt")
        try:
            with open(pending_path, encoding="utf-8") as f:
                links = f.readlines()
        except Exception as e:
            print(Fore.RED + f"Ошибка чтения pending.txt: {e}")
            sys.exit(1)
        asyncio.run(batch_download(links, max_connections, skip_videos, skip_images, sort_links))

# Что нужно ввести в консоль чтобы залить код в сущесвтующий репозиторий на GitHub:
# git init
# git add README.md
# git commit -m "first commit"
# git branch -M main
# git remote add origin git@github.com:soroka01/EromeDownloader.git
# git push -u origin main
