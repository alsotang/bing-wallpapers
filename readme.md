# bing-wallpapers

A small script that mirrors the latest UHD Bing.com wallpapers locally, sourced
from the US market via [bing.npanuhin.me](https://bing.npanuhin.me/US-en.json).

The `images/` directory is **not** committed — run the script when you want to
refresh.

# Usage

```bash
./bing-wallpaper.py
```

On each run it will:

- Fetch the US-en archive.
- Keep at most the latest 1000 unique UHD images on disk.
- Download anything missing into `./images/` (concurrent, atomic writes).
- Prune anything in `./images/` that is no longer in the latest set.

Repeated runs are idempotent: only newly published wallpapers are downloaded,
nothing is re-fetched.

Point your OS wallpaper rotator at `./images/` and you're done.
