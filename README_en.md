# EromeDownloader

EromeDownloader is an interactive Python downloader for erome.com albums, direct
media files, and accounts. It can process a link queue, resume interrupted
downloads, track account updates, keep link statuses, and write a JSON manifest
with download results.

## Features

- Download one URL: album, direct file, or account.
- Batch mode for `links/pending.txt`.
- Persistent tracked-account list in `links/accs.txt`.
- Scan-only account mode: add new albums to `links/pending.txt` without
  downloading them immediately.
- Parallel file and album downloads with a shared connection limit.
- Automatic queue sorting: direct files, albums, then accounts.
- Album size probing and size-aware album sorting.
- Resumable downloads through temporary `.part` files.
- Retries for timeouts and temporary server errors.
- Duplicate filename protection inside albums.
- `ready`, `failed`, and `banned` status files for links and accounts.
- JSON manifest with URLs, paths, sizes, statuses, and timestamps.
- Album link import from exported browser bookmarks.

## Requirements

- Python 3.10 or newer.
- Dependencies from `requirements.txt`.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

On Linux/macOS, activate the environment with:

```bash
source .venv/bin/activate
```

## Run

```bash
python main.py
```

Choose a mode after launch:

1. Download one URL: album, direct file, or account.
2. Download all links from `links/pending.txt`.
3. Check tracked accounts from `links/accs.txt` and download new posts.
4. Only scan tracked accounts and add new posts to `links/pending.txt`.

The `downloads` and `links` directories are created automatically.

## Queue And Status Files

- `links/pending.txt` - queued albums, direct files, and accounts.
- `links/accs.txt` - persistent account watchlist.
- `links/ready.txt` - successfully downloaded links.
- `links/failed.txt` - links that failed after retries.
- `links/banned.txt` - unavailable links, for example 403/404/410.
- `links/ready_accs.txt` - successfully processed accounts.
- `links/failed_accs.txt` - accounts that failed.
- `links/banned_accs.txt` - unavailable accounts.
- `links/manifest.json` - download result database.

Add one URL per line to `pending.txt` or `accs.txt`. Empty lines and lines
starting with `#` are ignored.

## Output

Downloaded files are saved to `downloads`. Albums get separate folders named
after the cleaned album title. If an album contains files with the same name, the
script adds a suffix instead of overwriting existing files.

## Configuration

Main settings live in `config.json`. If the file is missing, it is created
automatically with default values.

Common settings:

- `max_connections` - total concurrent connection limit.
- `max_parallel_albums` - how many albums can run at the same time in batch mode.
- `min_connections_per_parallel_album` - minimum connections per active album.
- `auto_sort_links` - sort the queue before processing.
- `sort_albums_by_size` - probe album sizes and sort albums before downloading.
- `connect_timeout` and `idle_timeout` - network timeouts.
- `max_attempts` and `page_attempts` - retry counts.
- `skip_videos` and `skip_images` - skip videos or images.
- `manifest_path` - manifest file path.

If an existing `config.json` does not contain a newer option, the script falls
back to the built-in default for that option.

## Import Links From Bookmarks

1. Export browser bookmarks to `bookmarks.html`.
2. Put the file next to `main.py`.
3. Run:

```bash
python extract_links.py
```

The script extracts erome.com album links and appends them to
`links/pending.txt`.

## License

MIT. See `LICENSE` for details.
