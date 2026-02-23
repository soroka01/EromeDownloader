import re
import glob
import os
from pathlib import Path

def extract_links(html_path, output_path=None):
    html = Path(html_path).read_text(encoding='utf-8')
    links = re.findall(r'<A\s+HREF="([^"]+)"', html, re.IGNORECASE)
    links = [link for link in links if re.search(r'erome\.com/a/', link, re.IGNORECASE)]
    if output_path:
        Path(output_path).write_text('\n'.join(links), encoding='utf-8')
    return links

def main():
    print("\n=== EromeDownloader Link Extractor ===\n")
    html_files = glob.glob("bookmarks*.html")
    if not html_files:
        print("Не найдено bookmarks*.html в текущей папке.")
        return
    print("Найдены файлы:")
    for i, fname in enumerate(html_files, 1):
        print(f"  [{i}] {fname}")
    if len(html_files) == 1:
        idx = 1
    else:
        while True:
            try:
                idx = int(input(f"Выберите файл [1-{len(html_files)}]: "))
                if 1 <= idx <= len(html_files): break
            except Exception:
                pass
            print("Некорректный выбор.")
    html_path = html_files[idx-1]
    default_out = os.path.join("links", "pending.txt")
    print(f"\nСохранять ссылки в: {default_out}")
    confirm = input("Продолжить? (Y/n): ").strip().lower()
    if confirm not in ("", "y", "yes"): return
    links = extract_links(html_path, default_out)
    print(f"\nИзвлечено {len(links)} ссылок:")
    for link in links:
        print(link)
    print(f"\nВсе ссылки сохранены в {default_out}")

if __name__ == '__main__':
    main()
