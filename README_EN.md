# 📥 EromeDownloader

> An interactive asynchronous downloader for Erome albums you are allowed to save, direct media, and account pages, with queues, resumable downloads, statuses, and a JSON manifest.

🌐 **Language:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Async](https://img.shields.io/badge/Async-aiohttp%203.10.5-2C5BB4)
![Interface](https://img.shields.io/badge/Interface-CLI-4B5563)
![License](https://img.shields.io/badge/License-MIT-green)

## ✨ Overview

EromeDownloader processes individual links and local queues, fetches public album and account pages, downloads discovered images and videos, and saves the result to `downloads/`. Incomplete files retain the `.part` extension, while batch modes maintain separate `ready`, `failed`, and `banned` lists.

> [!IMPORTANT]
> Download only material that you are allowed to save. You are responsible for complying with copyright, privacy, applicable law, and Erome's rules. This project is not affiliated with Erome.

## 🚀 Key Features

| Feature | How it works |
| --- | --- |
| Albums | Parses the title, images, and video sources from the page |
| Direct files | Downloads any entered URL as an individual file |
| Accounts | Collects albums from every discovered page of a public profile |
| Queue | Processes `pending.txt` with deduplication and persistent statuses |
| Resume | Uses `.part` files and HTTP `Range` when the server supports partial responses |
| Concurrency | Shares a global connection limit between files and albums |
| Manifest | Stores URLs, paths, sizes, statuses, and update times in JSON |

Additional capabilities include:

- exponential backoff for temporary HTTP and network errors;
- sorting direct URLs, albums, and accounts;
- estimating album size in advance through `HEAD` or a range request;
- protection against duplicate filenames within one set of download jobs;
- image and video filters;
- importing album URLs from exported browser bookmarks.

## 🧭 Modes

After launch, choose one of four modes:

| Mode | Source | Behavior |
| --- | --- | --- |
| `1` | Console URL | Downloads one direct URL, one album, or all new albums from an account |
| `2` | `links/pending.txt` | Processes the queue, retries failures, and updates link status files |
| `3` | `links/accs.txt` | Checks tracked accounts and downloads new albums |
| `4` | `links/accs.txt` | Only discovers new albums and adds them to `pending.txt` |

Important differences:

- mode `1` writes available data to the manifest but does not maintain `ready.txt`, `failed.txt`, and `banned.txt` as a batch queue;
- mode `3` always collects both images and videos: the current implementation does not apply `skip_images` or `skip_videos` to it;
- mode `4` treats URLs from `ready`, `banned`, `failed`, and `pending` as already known, so a failed URL is not returned to the queue automatically.

## 🏗️ Data Flow and Architecture

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
main.py           # config, queues, parsers, networking, downloads, and CLI
extract_links.py  # separate bookmark importer
config.json       # safe runtime defaults without credentials
requirements.txt  # Python dependencies
start.bat         # Windows bootstrap launcher
```

The main implementation intentionally lives in one large `main.py`; there is currently no separate package API or command-line argument interface.

## 📋 Requirements

- Python 3.10 or newer;
- internet access;
- dependencies from `requirements.txt`.

Erome authentication, cookies, and private albums are not supported.

## ⚙️ Installation and Running

### Windows launcher

[start.bat](start.bat) creates a local `.venv`, installs dependencies, and starts the program:

```powershell
.\start.bat
```

`pip install -r requirements.txt` runs on every launch; already installed compatible versions are not downloaded again.

### Manual launch on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

### Linux or macOS

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python main.py
```

The `downloads/` and `links/` directories are created automatically.

## 🔧 Configuration

`config.json` contains the complete set of defaults:

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

| Field | Purpose |
| --- | --- |
| `max_connections` | Overall target limit for download connections |
| `min_connections_per_parallel_album` | Minimum connections per parallel album |
| `max_parallel_albums` | Maximum albums processed at the same time |
| `auto_sort_links` | Sort a mixed queue before processing |
| `album_prefetch_connections` | Parallel album-page read limit |
| `account_page_connections` | Account-page request limit |
| `account_max_pages` | Safety limit for pages from one account |
| `sort_albums_by_size` | Estimate sizes and sort albums |
| `album_size_probe_connections` | Parallel size probes |
| `album_size_probe_timeout` | Timeout for one size scan in seconds |
| `chunk_size_mb` | Size of each written chunk |
| `max_attempts` | File download attempts |
| `page_attempts` | HTML page read attempts |
| `connect_timeout` | Connection timeout in seconds |
| `idle_timeout` | Allowed time without new data |
| `skip_videos` | Exclude video sources in modes that apply this filter |
| `skip_images` | Exclude images in modes that apply this filter |
| `manifest_path` | JSON manifest path relative to the project, or an absolute path |

If the file is missing, the program creates it with built-in defaults. Keys missing from an older file also receive their built-in values. User-provided value types and ranges are not validated separately, so change them carefully.

## 🗂️ Queues, Statuses, and Manifest

| Path | Purpose |
| --- | --- |
| `links/pending.txt` | Queue of direct URLs, albums, and accounts |
| `links/accs.txt` | Persistent list of tracked accounts |
| `links/ready.txt` | Successfully completed batch links |
| `links/failed.txt` | Links not downloaded after the final retry |
| `links/banned.txt` | Unavailable links, including HTTP 403/404/410 |
| `links/ready_accs.txt` | Successfully processed accounts |
| `links/failed_accs.txt` | Accounts that encountered an error |
| `links/banned_accs.txt` | Unavailable accounts |
| `links/manifest.json` | File, album, and account records |

Add one URL per line. Empty lines and lines beginning with `#` are ignored by the working batch modes.

After a final error, mode `2` removes the URL from `pending.txt` and places it in `failed.txt`. To try again, manually add the URL back to `pending.txt`; a successful result will move it to `ready.txt`.

## 🔖 Importing from Bookmarks

1. Export browser bookmarks to a `bookmarks*.html` file.
2. Put it next to `main.py`.
3. Run:

   ```powershell
   python extract_links.py
   ```

4. Select the file and confirm the operation.

> [!WARNING]
> The current importer **replaces the contents** of `links/pending.txt` with the links it finds. It does not merge them with the existing queue. Back up the old file or merge the lists manually if the queue is already populated.

## 🔐 Security

- The project does not require passwords, cookies, or API keys.
- A direct URL is not restricted to the Erome domain; use trusted links only.
- `downloads/`, `links/`, `.venv/`, and logs are excluded from Git.
- The manifest may contain source URLs and local filenames; consider this before publishing it.

## 🧪 Limitations and Testing

- Parsing depends on Erome's current HTML structure: `og:title`, `<source>`, and `img.img-back`.
- Resume works only when the server correctly supports `Range`; otherwise the file is downloaded again from the beginning.
- Size may remain unknown if the server does not provide it through `HEAD` or a range response.
- The repository has no automated tests or CI.
- You can check syntax without making network requests:

  ```bash
  python -m compileall -q main.py extract_links.py
  ```

## 🩹 Troubleshooting

| Symptom | What to check |
| --- | --- |
| Nothing is found in the queue | URL format and one link per line |
| Album is empty | Erome HTML changes or enabled `skip_*` filters |
| File always restarts from the beginning | HTTP Range support on the media server |
| URL no longer appears after a scan | It is already present in `ready`, `banned`, `failed`, or `pending` |
| Manifest is corrupted | The program moves it to a file with the `.bad` suffix |
| Download stalls | `connect_timeout`, `idle_timeout`, and server availability |

## 📄 License

This project is distributed under the [MIT License](LICENSE).

---

📥 The queue remains transparent: media in `downloads/`, state in `links/`.
