# EromeDownloader V2

EromeDownloader is a compact and powerful Python script for downloading albums from erome.com (videos, images, gifs).

## How to use?

1. Install dependencies:

```
pip install -r requirements.txt
```

2. Run the script:

```
python dump.py
```

3. Follow the interactive console instructions:
- Choose mode (single link or batch)
- Set download parameters
- For batch mode, add links to `links/pending.txt` (one per line)

## Arguments

- All parameters are entered via console dialog.
- Links for batch mode are taken from `links/pending.txt`.

## Where are files saved?

Files are saved in the `downloads` folder, with a subfolder named after the album.

## Link statuses

- `links/pending.txt` — waiting for download
- `links/ready.txt` — successfully downloaded
- `links/failed.txt` — errors (can be re-added to pending.txt for retry)

## Features

- Automatic retry on failure/hang
- Dynamic connection adaptation
- Beautiful progress bar
- User-friendly interactive interface
- Link sorting by weight: downloads start from the smallest files (lowest size)
- For bulk import of links from Chrome bookmarks, use extract_links.py:
	1. Export bookmarks from Chrome to bookmarks.html
	2. Run: python extract_links.py
	3. Links will be automatically saved to links/pending.txt
