import re
from pathlib import Path

from erome.ui import ui


ALBUM_URL_RE = re.compile(
    r"(https?://(?:www\.)?erome\.com/a/[^\s\"'<>?#]+)",
    re.IGNORECASE,
)
HREF_RE = re.compile(r"<a\s+[^>]*href=[\"']([^\"']+)[\"']", re.IGNORECASE)


def normalize_album_url(url: str) -> str:
    match = ALBUM_URL_RE.search(url.strip())
    return match.group(1) if match else url.strip()


def dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def extract_links(html_path, output_path=None):
    html = Path(html_path).read_text(encoding="utf-8")
    links = []

    for match in HREF_RE.finditer(html):
        link = normalize_album_url(match.group(1))
        if ALBUM_URL_RE.search(link):
            links.append(link)

    links = dedupe_preserve_order(links)

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            "\n".join(links) + ("\n" if links else ""),
            encoding="utf-8",
        )

    return links


def main():
    ui.set_title("EromeDownloader: извлечение ссылок")
    ui.title("EromeDownloader  ·  извлечение ссылок из закладок")

    html_files = sorted(Path(".").glob("bookmarks*.html"))
    if not html_files:
        ui.line("  Не найдено bookmarks*.html в текущей папке.", "warn")
        return

    ui.line("  Найдены файлы:")
    for index, path in enumerate(html_files, 1):
        ui.line(f"  {ui.paint(str(index), 'key')}  {path}")

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
    default_output = Path("links") / "pending.txt"
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
