# 📥 EromeDownloader

> Command-line downloader for public Erome albums, media files and accounts.

🌐 **Язык / Language:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white)
![aiohttp](https://img.shields.io/badge/aiohttp-3.14-2C5BB4)
![BeautifulSoup](https://img.shields.io/badge/beautifulsoup4-4.15-green)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

## 📌 Overview

The program downloads public Erome albums, individual files and new albums from accounts. It supports queues, parallel downloads, resuming and account tracking. The interface is a console menu: after launch the main menu is shown and you pick a mode by number. One mode is a Telegram bot: you send it links to albums and profiles, and it downloads them and posts them to your group.

> [!NOTE]
> Erome authentication, cookies and private albums are not supported.

## ✨ Features

- Five modes: single link, queue, account check, scan-only for new albums, Telegram bot
- Telegram bot: album and profile links from a chat → download → posting to a group or topic from your own account; send as many links as you like without waiting for the previous ones
- Parallel downloading of multiple albums and files
- Resuming via `.part` files and HTTP Range
- Tracking new account albums (`links/accs.txt`)
- Queue status files and a JSON manifest
- Optional sorting of albums by size and `skip_videos` / `skip_images` filters
- Importing links from browser bookmarks (`extract_links.py`)

## 🏗️ How it works

```mermaid
flowchart TD
    A["URL / links/pending.txt / links/accs.txt"] --> B["Download queue"]
    B --> C["downloads/ + .part"]
    C --> D["Statuses + JSON manifest"]
```

Modes:

| Mode | Source | Behavior |
| --- | --- | --- |
| `1` | Console URL | Downloads one direct URL, one album, or all new albums of an account |
| `2` | `links/pending.txt` | Processes the queue, retries failures and updates status files |
| `3` | `links/accs.txt` | Checks tracked accounts and downloads new albums |
| `4` | `links/accs.txt` | Only finds new albums and appends them to `pending.txt` |
| `5` | Telegram bot chat | Takes links, downloads and posts them to a group, see [the section below](#-telegram-bot-mode-5) |

- Mode `1` writes to the manifest but does not maintain `ready.txt`, `failed.txt` and `banned.txt` as a batch queue.
- Mode `3` always collects both images and videos: `skip_images` and `skip_videos` are not applied to it.
- Mode `4` treats URLs from `ready`, `banned`, `failed` and `pending` as known, so a failed URL is not re-queued automatically.

### Console controls

- Main menu: `1`-`5` are the modes from the table above, `0` (or `q`, `exit`) quits. Empty input selects `1`. The menu is shown again after every action.
- `Ctrl+C` interrupts the current action and returns to the menu; `.part` files and queues are kept, so unfinished work continues on the next run.
- While running, a single progress line is updated at the bottom: links, albums and files processed, transferred size, speed and time left.
- Hot keys (interactive console only, also work on a Russian keyboard layout): `S` - status, `P` - pause, `Q` - stop after the current item. Pause and stop take effect between items: queue links and albums, accounts; an album or file already in progress runs to completion. While downloading a single album or file (mode `1`) only `S` works. Unprocessed items stay in the queue.

## 🚀 Quick start

### Requirements

- Python 3.14+
- Internet access
- Dependencies from `requirements.txt`

### Installation

Windows: [start.bat](start.bat) creates `.venv` and installs dependencies itself (`pip install -r requirements.txt` runs on every launch; compatible versions are not downloaded again). Manually:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux / macOS:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

### Configuration

No setup is required: if `config.json` is missing it is created with defaults, and missing keys fall back to built-in values. See the next section.

### Run

```powershell
.\start.bat
```

```bash
./.venv/bin/python main.py
```

The `downloads/` and `links/` directories are created automatically.

## ⚙️ Configuration

`config.json` (defaults):

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

| Variable | Default | Description |
| --- | --- | --- |
| `max_connections` | `6` | Overall target limit of download connections |
| `min_connections_per_parallel_album` | `3` | Minimum connections per parallel album |
| `max_parallel_albums` | `3` | Maximum albums processed at once |
| `auto_sort_links` | `true` | Sort the queue before processing |
| `album_prefetch_connections` | `6` | Limit of parallel album page reads |
| `account_page_connections` | `3` | Limit of account page requests |
| `account_max_pages` | `500` | Page limit per account |
| `sort_albums_by_size` | `true` | Estimate size and sort albums |
| `album_size_probe_connections` | `3` | Parallel size probes |
| `album_size_probe_timeout` | `20` | Size probe timeout, seconds |
| `chunk_size_mb` | `1` | Size of a written chunk, MB |
| `max_attempts` | `4` | File download attempts |
| `page_attempts` | `3` | HTML page read attempts |
| `connect_timeout` | `30` | Connection timeout, seconds |
| `idle_timeout` | `180` | Allowed time without new data, seconds |
| `skip_videos` | `false` | Skip videos in modes that honor the filter |
| `skip_images` | `false` | Skip images in modes that honor the filter |
| `manifest_path` | `links/manifest.json` | JSON manifest path: project-relative or absolute |

Value types and ranges are not validated — edit with care.

### Queues and outputs

| Path | Purpose |
| --- | --- |
| `links/pending.txt` | Queue of direct URLs, albums and accounts |
| `links/accs.txt` | Persistent list of tracked accounts |
| `links/ready.txt` | Successfully completed batch links |
| `links/failed.txt` | Links that failed after the last attempt |
| `links/banned.txt` | Unavailable links, including HTTP 403/404/410 |
| `links/ready_accs.txt` | Successfully processed accounts |
| `links/failed_accs.txt` | Accounts that failed |
| `links/banned_accs.txt` | Unavailable accounts |
| `links/manifest.json` | Records of files, albums and accounts |

One URL per line; blank lines and lines starting with `#` are ignored by batch modes. After a final failure, mode `2` moves the URL from `pending.txt` to `failed.txt`; to retry, add it back to `pending.txt` manually.

### Importing bookmarks

1. Export browser bookmarks to `bookmarks*.html` and put the file next to `main.py`.
2. Run `python extract_links.py`, choose the file and confirm.

> [!WARNING]
> The importer **replaces the contents** of `links/pending.txt` with the found links instead of merging them. Back up the old file or merge lists manually.

## 🤖 Telegram bot (mode 5)

Mode 5 takes Erome album and profile links in a chat with a bot, downloads them and posts them to a group (into the chosen forum topic) from your own account, through a Telethon "userbot". You can send as many links as you like at any time: everything is queued and processed in order, there is no need to wait for the previous one to finish.

```mermaid
flowchart LR
    U["Links in the bot chat"] --> Q["Queue<br/>links/tg_queue.json"]
    Q --> D["Download<br/>downloads/_telegram/"]
    D --> S["Userbot posts<br/>to the group / topic"]
    S --> L["Ledger of what was sent<br/>links/tg_sent.jsonl"]
```

### Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy its token.
2. Start the program and choose item `5`. If `telegram.json` does not exist yet it is created from `telegram.example.json`, and the program asks for the token itself. Fill in the other fields (table below); you can also prepare the file by hand.
3. `api_id` and `api_hash` come from [my.telegram.org](https://my.telegram.org) (API development tools). Instead of them you can set `credentials_config_path` — a path to another JSON file with a `telegram` block (`api_id`, `api_hash`).
4. On the first run a QR code is printed: on your phone open "Settings → Devices → Link Desktop Device" and scan it **with the account that should post**. If `account_id` is set and a different account signs in, the session is closed and the sign-in has to be repeated. The cloud password (2FA), if any, is typed in the console. The session is kept in `data/telegram.session`, so you sign in only once.
5. Send the bot `/start` and then your links.

`telegram.json` (template: `telegram.example.json`):

| Field | Description |
| --- | --- |
| `bot_token` | Bot token from @BotFather |
| `owner_ids` | IDs of the accounts allowed to use the bot; everyone else is ignored (their ID is printed to the console once) |
| `account_id` | ID of the sending account: checked at sign-in; `null` disables the check |
| `chat_id` | Group ID, e.g. `-1001234567890` |
| `thread_id` | Forum topic ID (the number at the end of a `t.me/c/…/<thread_id>` link); `0` posts to the main chat |
| `api_id`, `api_hash` | Telegram application credentials; may stay empty when `credentials_config_path` is set |
| `credentials_config_path` | Path to a JSON file with `api_id` / `api_hash` (`telegram` block), e.g. from another project |
| `session_path` | Session file without extension, `data/telegram` by default |
| `delete_after_upload` | Delete local files right after Telegram accepted them (`true` by default) |
| `upload_connections` | Upper limit of connections when sending big files, 1-24 (`12` by default) |

### Usage

- Send album links (`erome.com/a/…`) and profile links (`erome.com/name`): one per message or several in a single message. The bot deletes your message and shows its state in a single panel message that updates itself: the current album (downloaded and sent counts, speed, time left), the queue and recently finished items (Moscow time).
- Panel buttons: pause / resume, cancel the current album, clear the queue (press twice).
- Commands: `/start` — instructions, `/queue` — bring the panel back to the bottom of the chat, `/resend link` — post an album again.
- A profile expands into the albums that were not sent to the group yet, oldest first; albums that were fully sent are skipped. To pick up new files in an old album, send the album link itself.
- In the console: `S` — status, `P` — pause, `Q` — stop after the current batch of files, `Ctrl+C` — interrupt at once. The queue is stored on disk and continues on the next run.

### What is sent and how

- Photos and videos go out as albums of up to 10, with a caption (title and album link) on the first file. Videos are sent with a preview and play without downloading the whole file. GIFs and photos that do not fit Telegram's photo limits go out as separate messages or files.
- While one batch (up to 10 files) is being posted, the next one is downloaded. Files live in `downloads/_telegram/`, not in the main `downloads/` archive.
- The ledger `links/tg_sent.jsonl` remembers every batch Telegram accepted, and every message has a fixed `random_id`: a repeated link, a lost connection or a restart at any moment never creates duplicates. Only what is missing is sent again.
- Files that failed to download are tried once more at the end of the album; if some never arrive the album is marked as partly sent, and sending the link again fetches the rest.
- Telegram's file limit is 2 GB, 4 GB for a Premium account.
- The mode can run in one window only.

## 🗂️ Project structure

```text
EromeDownloader/
├── main.py                # entry point, mode menu
├── extract_links.py       # bookmarks link importer
├── start.bat              # Windows launcher (.venv + dependencies)
├── config.json            # settings
├── telegram.example.json  # template of the Telegram mode settings
├── telegram.json          # your Telegram mode settings: token and IDs (not in Git)
├── requirements.txt       # dependencies
├── erome/
│   ├── config.py          # settings loading
│   ├── console.py         # logs, progress line, hot keys
│   ├── downloader.py      # file downloading
│   ├── models.py          # data models
│   ├── net.py             # networking
│   ├── storage.py         # queues, statuses, manifest
│   ├── ui.py              # terminal UI: colours, status line, prompts
│   ├── urls.py            # URL parsing and sorting
│   ├── workflow.py        # mode 1-4 workflows
│   ├── tg_settings.py     # mode 5: telegram.json, token, credentials
│   ├── tg_state.py        # mode 5: queue and ledger of what was sent
│   ├── tg_media.py        # mode 5: file kind, video metadata, thumbnails
│   ├── tg_userbot.py      # mode 5: userbot (Telethon), QR sign-in, sending
│   ├── tg_pipeline.py     # mode 5: download → send
│   └── tg_bot.py          # mode 5: bot (aiogram), panel, startup
├── downloads/             # downloaded files; _telegram/ holds mode 5 scratch files (not in Git)
├── links/                 # queues, manifest, tg_queue.json and tg_sent.jsonl (not in Git)
└── data/                  # Telegram session and mode 5 log (not in Git)
```

## 🔒 Security & privacy

- Modes 1-4 need no passwords, cookies or API keys.
- A direct URL is not restricted to the Erome domain; use trusted links only.
- `downloads/`, `links/`, `data/`, `telegram.json`, `.venv/`, `.env`, session files and logs are excluded from Git.
- The bot token and the session file `data/telegram.session` give full access to the bot and the account: never publish them. If a token leaks, reissue it with @BotFather (`/revoke`).
- The manifest may contain source URLs and local file names — consider this before sharing it.

## ⚠️ Limitations

- Parsing depends on Erome's current HTML markup: `og:title`, `<source>` and `img.img-back`.
- Resuming works only if the server supports `Range`; otherwise the file is downloaded again.
- Size may stay unknown if the server does not report it via `HEAD` or a range response.
- A corrupted manifest is moved to a file with a `.bad` suffix.
- Mode 5 is bound by Telegram's limits (upload speed, message rate in a group): when throttled the program waits and carries on by itself.

## 📄 License

[MIT](LICENSE).

## 💬 Support

Feel free to [fork the repository](https://github.com/soroka01/EromeDownloader/fork) and adapt it. If the project helped you, leave a [Star](https://github.com/soroka01/EromeDownloader) so I know it was useful.

---

with love ❤️
