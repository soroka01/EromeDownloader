import re
from pathlib import Path


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
    print("\n=== EromeDownloader Link Extractor ===\n")

    html_files = sorted(Path(".").glob("bookmarks*.html"))
    if not html_files:
        print("Не найдено bookmarks*.html в текущей папке.")
        return

    print("Найдены файлы:")
    for index, path in enumerate(html_files, 1):
        print(f"  [{index}] {path}")

    if len(html_files) == 1:
        selected_index = 1
    else:
        while True:
            try:
                selected_index = int(input(f"Выберите файл [1-{len(html_files)}]: "))
            except ValueError:
                selected_index = 0

            if 1 <= selected_index <= len(html_files):
                break
            print("Некорректный выбор.")

    html_path = html_files[selected_index - 1]
    default_output = Path("links") / "pending.txt"
    print(f"\nСохранять ссылки в: {default_output}")

    confirm = input("Продолжить? (Y/n): ").strip().lower()
    if confirm not in {"", "y", "yes", "д", "да"}:
        return

    links = extract_links(html_path, default_output)
    print(f"\nИзвлечено {len(links)} ссылок:")
    for link in links:
        print(link)
    print(f"\nВсе ссылки сохранены в {default_output}")


if __name__ == "__main__":
    main()
