import re
from pathlib import Path

from erome.config import BASE_DIR, LINKS_DIR
from erome.storage import write_text_atomic
from erome.ui import ui
from erome.urls import dedupe_preserve_order, is_erome_album_url, normalize_album_url


HREF_RE = re.compile(r"<a\s+[^>]*href=[\"']([^\"']+)[\"']", re.IGNORECASE)


def extract_links(html_path: str | Path, output_path: str | Path | None = None) -> list[str]:
    html = Path(html_path).read_text(encoding="utf-8", errors="replace")
    links = []

    for match in HREF_RE.finditer(html):
        link = normalize_album_url(match.group(1))
        if is_erome_album_url(link):
            links.append(link)

    links = dedupe_preserve_order(links)

    if output_path:
        write_text_atomic(Path(output_path), "\n".join(links) + ("\n" if links else ""))

    return links


def main():
    ui.set_title("EromeDownloader: извлечение ссылок")
    ui.title("EromeDownloader  ·  извлечение ссылок из закладок")

    html_files = sorted(BASE_DIR.glob("bookmarks*.html"))
    if not html_files:
        ui.line(f"  Не найдено bookmarks*.html в папке {BASE_DIR}.", "warn")
        return

    ui.line("  Найдены файлы:")
    for index, path in enumerate(html_files, 1):
        ui.line(f"  {ui.paint(str(index), 'key')}  {path.name}")

    if len(html_files) == 1:
        selected_index = 1
    else:
        while True:
            try:
                selected_index = int(ui.ask(f"Выберите файл [1-{len(html_files)}]", "1"))
            except ValueError:
                selected_index = 0

            if 1 <= selected_index <= len(html_files):
                break
            ui.line("  Некорректный выбор.", "warn")

    html_path = html_files[selected_index - 1]
    default_output = LINKS_DIR / "pending.txt"
    ui.field("Сохранить в", default_output)

    if not ui.confirm("Продолжить? Содержимое файла будет заменено", default=True):
        return

    links = extract_links(html_path, default_output)
    ui.title(f"Извлечено ссылок: {len(links)}")
    for link in links:
        ui.line(f"  {link}", "dim")
    ui.line(f"  Все ссылки сохранены в {default_output}", "ok")


if __name__ == "__main__":
    main()
