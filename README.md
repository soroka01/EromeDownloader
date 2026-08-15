# 📥 EromeDownloader

> Интерактивный асинхронный загрузчик разрешённых пользователю Erome-альбомов, прямых медиа и страниц аккаунтов с очередью, докачкой, статусами и JSON-manifest.

🌐 **Язык:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white)
![Async](https://img.shields.io/badge/Async-aiohttp%203.10.5-2C5BB4)
![Interface](https://img.shields.io/badge/Interface-CLI-4B5563)
![License](https://img.shields.io/badge/License-MIT-green)

## ✨ Обзор

EromeDownloader обрабатывает одиночные ссылки и локальные очереди, получает публичные страницы альбомов и аккаунтов, скачивает найденные изображения и видео и сохраняет результат в `downloads/`. Незавершённые файлы остаются с расширением `.part`, а batch-режимы ведут отдельные списки `ready`, `failed` и `banned`.

> [!IMPORTANT]
> Скачивайте только материалы, которые вам разрешено сохранять. Вы отвечаете за соблюдение авторских прав, приватности, законодательства и правил Erome. Проект не связан с Erome.

## 🚀 Основные возможности

| Возможность | Как работает |
| --- | --- |
| Альбомы | Парсинг названия, изображений и video sources со страницы |
| Прямые файлы | Любой введённый URL скачивается как отдельный файл |
| Аккаунты | Сбор альбомов со всех найденных страниц публичного профиля |
| Очередь | `pending.txt` обрабатывается с deduplication и сохранением статусов |
| Докачка | `.part` + HTTP `Range`, если сервер поддерживает partial responses |
| Параллельность | Общий лимит соединений распределяется между файлами и альбомами |
| Manifest | JSON с URL, путями, размерами, статусами и временем обновления |

Дополнительно доступны:

- exponential backoff для временных HTTP и network errors;
- сортировка direct URLs, альбомов и аккаунтов;
- предварительная оценка размера альбомов через `HEAD` или range request;
- защита от одинаковых имён внутри одного набора download jobs;
- фильтры изображений и видео;
- импорт album URLs из экспортированных browser bookmarks.

## 🧭 Режимы

После запуска выберите один из четырёх режимов:

| Режим | Источник | Поведение |
| --- | --- | --- |
| `1` | URL из консоли | Скачивает один direct URL, альбом или все новые альбомы аккаунта |
| `2` | `links/pending.txt` | Обрабатывает очередь, повторяет failures и обновляет link status-файлы |
| `3` | `links/accs.txt` | Проверяет отслеживаемые аккаунты и скачивает новые альбомы |
| `4` | `links/accs.txt` | Только находит новые альбомы и добавляет их в `pending.txt` |

Важные различия:

- режим `1` пишет доступные данные в manifest, но не ведёт `ready.txt`, `failed.txt` и `banned.txt` как batch-очередь;
- режим `3` всегда собирает и изображения, и видео: текущая реализация не применяет к нему `skip_images` и `skip_videos`;
- режим `4` считает уже известными URL из `ready`, `banned`, `failed` и `pending`, поэтому failed URL автоматически в очередь не возвращается.

## 🏗️ Поток данных и архитектура

```text
console URL / pending.txt / accs.txt
                 ↓
       normalize + classify
         ↙       ↓       ↘
    direct     album    account pages
                 ↓          ↓
             media URLs ← album URLs
                 ↓
        async download workers
                 ↓
 downloads/ + status files + manifest.json
```

```text
main.py           # config, queues, parsers, network, downloads и CLI
extract_links.py  # отдельный bookmark importer
config.json       # безопасные runtime defaults без credentials
requirements.txt  # Python dependencies
start.bat         # Windows bootstrap launcher
```

Основная реализация намеренно находится в одном крупном `main.py`; отдельного package API или command-line arguments сейчас нет.

## 📋 Требования

- Python 3.14 или новее (рекомендуется актуальный патч 3.14.6);
- pip 26.1.2, setuptools 84.0.0 и wheel 0.48.0 (launcher обновляет их автоматически);
- интернет-доступ;
- зависимости из `requirements.txt`.

Авторизация Erome, cookies и private albums не поддерживаются.

## ⚙️ Установка и запуск

### Windows launcher

[start.bat](start.bat) создаёт локальную `.venv`, устанавливает зависимости и запускает программу:

```powershell
.\start.bat
```

`pip install -r requirements.txt` выполняется при каждом запуске; уже установленные подходящие версии повторно не скачиваются.

### Ручной запуск в Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

### Linux или macOS

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python main.py
```

Папки `downloads/` и `links/` создаются автоматически.

## 🔧 Конфигурация

`config.json` содержит полный набор defaults:

```json
{
  "max_connections": 6,
  "min_connections_per_parallel_album": 3,
  "max_parallel_albums": 3,
  "auto_sort_links": true,
  "album_prefetch_connections": 6,
  "account_page_connections": 3,
  "account_max_pages": 500,
  "sort_albums_by_size": true,
  "album_size_probe_connections": 3,
  "album_size_probe_timeout": 20,
  "chunk_size_mb": 1,
  "max_attempts": 4,
  "page_attempts": 3,
  "connect_timeout": 30,
  "idle_timeout": 180,
  "skip_videos": false,
  "skip_images": false,
  "manifest_path": "links/manifest.json"
}
```

| Поле | Назначение |
| --- | --- |
| `max_connections` | Общий целевой лимит download connections |
| `min_connections_per_parallel_album` | Минимум соединений на параллельный альбом |
| `max_parallel_albums` | Максимум одновременно обрабатываемых альбомов |
| `auto_sort_links` | Сортировать mixed queue перед обработкой |
| `album_prefetch_connections` | Лимит параллельного чтения album pages |
| `account_page_connections` | Лимит запросов страниц аккаунта |
| `account_max_pages` | Safety limit страниц одного аккаунта |
| `sort_albums_by_size` | Оценивать размер и сортировать альбомы |
| `album_size_probe_connections` | Параллельные size probes |
| `album_size_probe_timeout` | Timeout одного size scan в секундах |
| `chunk_size_mb` | Размер записываемого chunk |
| `max_attempts` | Попытки скачивания файла |
| `page_attempts` | Попытки чтения HTML page |
| `connect_timeout` | Connection timeout в секундах |
| `idle_timeout` | Допустимое время без новых данных |
| `skip_videos` | Не включать video sources там, где режим учитывает фильтр |
| `skip_images` | Не включать images там, где режим учитывает фильтр |
| `manifest_path` | Путь к JSON manifest относительно проекта или absolute path |

Если файла нет, программа создаёт его со встроенными defaults. Отсутствующие в старом файле ключи также получают встроенные значения. Типы и диапазоны пользовательских значений отдельно не валидируются, поэтому изменяйте их осторожно.

## 🗂️ Очереди, статусы и manifest

| Путь | Назначение |
| --- | --- |
| `links/pending.txt` | Очередь direct URLs, альбомов и аккаунтов |
| `links/accs.txt` | Постоянный список отслеживаемых аккаунтов |
| `links/ready.txt` | Успешно завершённые batch-ссылки |
| `links/failed.txt` | Ссылки, не скачанные после финальной retry |
| `links/banned.txt` | Недоступные ссылки, включая HTTP 403/404/410 |
| `links/ready_accs.txt` | Успешно обработанные аккаунты |
| `links/failed_accs.txt` | Аккаунты с ошибкой |
| `links/banned_accs.txt` | Недоступные аккаунты |
| `links/manifest.json` | Записи файлов, альбомов и аккаунтов |

Добавляйте по одному URL на строку. Пустые строки и строки, начинающиеся с `#`, игнорируются рабочими batch-режимами.

После финальной ошибки mode `2` удаляет URL из `pending.txt` и помещает его в `failed.txt`. Чтобы попробовать снова, вручную добавьте URL обратно в `pending.txt`; успешный результат перенесёт его в `ready.txt`.

## 🔖 Импорт из закладок

1. Экспортируйте browser bookmarks в файл `bookmarks*.html`.
2. Положите его рядом с `main.py`.
3. Запустите:

   ```powershell
   python extract_links.py
   ```

4. Выберите файл и подтвердите операцию.

> [!WARNING]
> Текущий importer **заменяет содержимое** `links/pending.txt` найденными ссылками. Он не объединяет их с существующей очередью. Сохраните старый файл или объедините списки вручную, если очередь уже заполнена.

## 🔐 Безопасность

- Проект не требует паролей, cookies или API keys.
- Direct URL не ограничен доменом Erome; используйте только доверенные ссылки.
- `downloads/`, `links/`, `.venv/` и logs исключены из Git.
- Manifest может содержать исходные URL и локальные имена файлов — учитывайте это перед публикацией.

## 🧪 Ограничения и тестирование

- Парсинг зависит от текущей HTML-разметки Erome: `og:title`, `<source>` и `img.img-back`.
- Resume работает только при корректной поддержке `Range` сервером; иначе файл загружается заново.
- Размер может остаться неизвестным, если сервер не сообщает его через `HEAD` или range response.
- Автоматизированных тестов и CI в репозитории нет.
- Синтаксис можно проверить без сетевых запросов:

  ```bash
  python -m compileall -q main.py extract_links.py
  ```

## 🩹 Решение проблем

| Симптом | Что проверить |
| --- | --- |
| В очереди ничего не найдено | Формат URL и по одной ссылке на строку |
| Альбом пуст | Изменение HTML Erome или включённые `skip_*` filters |
| Файл постоянно начинается заново | Поддержку HTTP Range у media server |
| URL больше не появляется после scan | Он уже есть в `ready`, `banned`, `failed` или `pending` |
| Manifest повреждён | Программа перенесёт его в файл с суффиксом `.bad` |
| Скачивание зависает | `connect_timeout`, `idle_timeout` и доступность сервера |

## 📄 Лицензия

Проект распространяется по [лицензии MIT](LICENSE).

---

📥 Очередь остаётся прозрачной: media — в `downloads/`, состояние — в `links/`.
