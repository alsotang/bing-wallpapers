#!/usr/bin/env python3
"""Sync the latest UHD Bing wallpapers from bing.npanuhin.me/US-en.json.

Sources only the US market (other markets duplicate the same scene under
different filenames). Keeps at most KEEP_LATEST unique images on disk:
downloads any latest entries that are missing, and deletes anything in the
images directory that isn't part of the latest set.
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
IMAGES_DIR = ROOT_DIR / 'images'
ARCHIVE_URL = 'https://bing.npanuhin.me/US-en.json'
USER_AGENT = 'bing-wallpapers/1.0 (+https://github.com)'
TIMEOUT = 60
# Maximum number of unique UHD images to retain.
KEEP_LATEST = 2000
WORKERS = 8
RETRIES = 2
# Reject anything Bing returns below 2560x1440 — sometimes the UHD URL falls
# back to a smaller render and we'd rather skip than save a degraded copy.
MIN_WIDTH = 2560
MIN_HEIGHT = 1440


def http_open(url):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def fetch_archive():
    for attempt in range(RETRIES + 1):
        try:
            with http_open(ARCHIVE_URL) as response:
                entries = json.load(response)
            if not isinstance(entries, list):
                raise ValueError('archive must contain a list of entries')
            return entries
        except (OSError, ValueError):
            if attempt == RETRIES:
                raise


def parse_image_name(bing_url):
    """Pull the `id` query param out of a bing.com/th?id=... URL, or None."""
    if not bing_url:
        return None
    try:
        query = urllib.parse.urlsplit(bing_url).query
    except ValueError:
        return None
    ids = urllib.parse.parse_qs(query).get('id') or []
    return ids[0] if ids else None


def latest_unique_images(entries, limit):
    """Return up to `limit` (image_name, image_url) pairs, newest first.

    Also returns counters describing how many feed rows were considered vs.
    usable, since the US feed is dominated (~85%) by older rows with no
    `bing_url` that we have to skip.
    """
    rows = []
    usable = 0
    for entry in entries or []:
        if not entry:
            continue
        image_name = parse_image_name(entry.get('bing_url'))
        date = entry.get('date') or ''
        # The pre-UHD-era rows in the feed have null bing_url and are unusable;
        # we also keep a defensive `_UHD` suffix check in case the format drifts.
        if not image_name or not date or '_UHD' not in image_name:
            continue
        usable += 1
        rows.append((date, image_name, entry['bing_url']))

    # Newest-first so the dedupe loop below keeps the most recent occurrence
    # of any image Bing has reused on multiple dates.
    rows.sort(key=lambda row: row[0], reverse=True)

    seen = {}
    for _, image_name, bing_url in rows:
        if image_name in seen:
            continue
        seen[image_name] = bing_url
        if len(seen) >= limit:
            break
    return seen, usable


def jpeg_size(path):
    """Best-effort (width, height) for a JPEG, or None if unparseable.

    Hand-rolled SOF (Start Of Frame) scan so we don't pull in Pillow just to
    reject undersized images.
    """
    with open(path, 'rb') as file:
        if file.read(2) != b'\xff\xd8':
            return None
        while True:
            byte = file.read(1)
            if byte == b'':
                return None
            if byte != b'\xff':
                continue
            marker = file.read(1)
            # 0xFF padding bytes are legal between markers; skip them.
            while marker == b'\xff':
                marker = file.read(1)
            # SOI/EOI carry no payload; standalone, just keep scanning.
            if marker in {b'', b'\xd8', b'\xd9'}:
                continue
            size_bytes = file.read(2)
            if len(size_bytes) != 2:
                return None
            block_size = int.from_bytes(size_bytes, 'big')
            if block_size < 2:
                return None
            # SOF0..SOF15, excluding DHT (C4), JPG (C8), DAC (CC) — these carry
            # the frame header whose bytes 1..4 are height then width (big-endian).
            if marker in {
                b'\xc0', b'\xc1', b'\xc2', b'\xc3', b'\xc5', b'\xc6',
                b'\xc7', b'\xc9', b'\xca', b'\xcb', b'\xcd', b'\xce', b'\xcf',
            }:
                block = file.read(block_size - 2)
                if len(block) < 5:
                    return None
                height = int.from_bytes(block[1:3], 'big')
                width = int.from_bytes(block[3:5], 'big')
                return width, height
            file.seek(block_size - 2, os.SEEK_CUR)


def download_image(url, destination):
    # Stream into a sibling tempfile and rename atomically, so an interrupted
    # run never leaves a half-written file at the real destination path.
    fd, temp_name = tempfile.mkstemp(
        prefix=destination.name + '.', suffix='.tmp', dir=destination.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with http_open(url) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'image' not in content_type:
                raise RuntimeError(f'expected image, got {content_type!r}')
            with open(temp_path, 'wb') as file:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    file.write(chunk)

        size = jpeg_size(temp_path)
        if size is None:
            raise RuntimeError('not a readable JPEG')
        width, height = size
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            raise RuntimeError(f'too small {width}x{height}')

        temp_path.replace(destination)
        return width, height
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def fetch_with_retries(url, destination):
    # Bing/CDN occasionally hiccups on bulk fetches; one or two extra tries
    # covers most transient failures without bothering the user.
    last_error = None
    for _ in range(RETRIES + 1):
        try:
            return download_image(url, destination)
        except (OSError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as error:
            last_error = error
    raise last_error


def main():
    print(f'fetching archive: {ARCHIVE_URL}', flush=True)
    entries = fetch_archive()

    wanted, usable = latest_unique_images(entries, KEEP_LATEST)
    if not wanted:
        raise RuntimeError('archive contains no usable UHD images; keeping existing files')
    print(
        f'feed entries: {len(entries)} | with UHD url: {usable} | '
        f'unique to keep: {len(wanted)} (cap={KEEP_LATEST})',
        flush=True,
    )

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Diff disk against the desired set: anything missing gets fetched, anything
    # extra (e.g. images that fell out of the latest window) gets pruned. This
    # is the dedupe — we never re-download a file that's already on disk.
    on_disk = {p.name for p in IMAGES_DIR.iterdir() if p.is_file() and p.suffix == '.jpg'}
    to_delete = on_disk - wanted.keys()
    to_download = [(name, url) for name, url in wanted.items() if name not in on_disk]

    removed = 0
    removal_failed = 0
    for name in sorted(to_delete):
        try:
            (IMAGES_DIR / name).unlink()
            removed += 1
            print(f'removed {name}', flush=True)
        except OSError as error:
            removal_failed += 1
            print(f'failed to remove {name}: {error}', flush=True)

    if not to_download:
        print('nothing to download', flush=True)
        print(f'done: kept={len(on_disk) - removed} removed={removed} failed={removal_failed}', flush=True)
        return int(bool(removal_failed))

    print(f'downloading {len(to_download)} new images...', flush=True)
    downloaded = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(fetch_with_retries, url, IMAGES_DIR / name): name
            for name, url in to_download
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                width, height = future.result()
                downloaded += 1
                print(f'[{downloaded}/{len(to_download)}] {name} {width}x{height}', flush=True)
            except (OSError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as error:
                failed += 1
                print(f'failed {name}: {error}', flush=True)

    print(
        f'done: kept={len(on_disk) - removed + downloaded} '
        f'downloaded={downloaded} removed={removed} failed={failed + removal_failed}',
        flush=True,
    )
    return int(bool(failed or removal_failed))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f'error: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
