# EromeDownloader

EromeDownloader is a Python script for downloading erome.com albums: videos,
images, and gifs. It runs interactively and can resume interrupted downloads.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

Choose a mode after launch:

1. Download one URL: album, direct file, or account.
2. Download all links from `links/pending.txt`.
3. Check tracked accounts from `links/accs.txt`.
4. Only scan tracked accounts and add new posts to `links/pending.txt`.

The `links` directory is created automatically. `links/pending.txt` can contain
album links, direct file links, and account links. Use `links/accs.txt` for a
persistent account watchlist, one URL per line. This file is not cleared after a
run.

The script does not ask technical tuning questions on every launch. Settings are
stored in `config.json`: connections, timeouts, sorting, video/image skipping,
and the manifest path can be changed there.

## Output

Files are saved to `downloads`. Album downloads get a separate subdirectory named
after the album.

## Link Statuses

- `links/pending.txt` — links not processed yet.
- `links/accs.txt` — persistent account watchlist.
- `links/ready.txt` — successfully downloaded links.
- `links/failed.txt` — links that failed after retries.
- `links/banned.txt` — unavailable links, for example 403/404/410.
- `links/ready_accs.txt` — successfully processed accounts.
- `links/failed_accs.txt` — accounts that failed.
- `links/banned_accs.txt` — unavailable accounts.
- `links/manifest.json` — downloaded manifest: URL, folder, size, date, status.

## Download Improvements

- Resumable `.part` downloads.
- Retries for timeouts and temporary server errors.
- Larger chunks for faster file writes.
- Controlled concurrent connections to avoid overloading the server.
- Multiple albums can run in parallel in batch mode: the total connection limit
  is shared between active albums instead of being multiplied.
- Photo-only albums are downloaded before video albums and sorted by photo count
  from smaller to larger.
- Photo-only albums use wider parallelism: more albums at the same time without
  exceeding the total connection limit.
- Fast first-pass queue sorting without network requests: direct files, then
  albums, then accounts. Detailed album sorting reuses the album page that is
  needed for downloading anyway.
- Duplicate filename protection inside albums.
- Honest final status: an album is successful only when its files were actually
  downloaded or already existed.
- Account tracking: the script crawls `?page=N` pages, collects all `/a/...`
  posts, skips albums already present in `ready.txt`/`banned.txt`, and keeps
  accounts in `accs.txt` for the next run.
- Scan-only account mode: add new albums to `pending.txt` without downloading.
- The manifest is updated after file downloads, album downloads, and account
  processing.

## Import Links From Bookmarks

1. Export browser bookmarks to `bookmarks.html`.
2. Put the file next to the scripts.
3. Run:

```bash
python extract_links.py
```

The script extracts album links and saves them to `links/pending.txt`.
