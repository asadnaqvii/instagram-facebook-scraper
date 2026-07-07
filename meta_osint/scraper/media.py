"""Media + metadata extraction via yt-dlp, and image download via Playwright.

yt-dlp talks to Meta's own internal endpoints, so it returns reliable post
metadata (caption, view/like/comment counts, upload date, uploader) and can
download the actual video — all without depending on the page DOM. That is
the backbone of this scraper's durability: even when Meta reshuffles their
markup, yt-dlp keeps working.

Images that yt-dlp doesn't fetch are downloaded through the authenticated
Playwright context (it carries your cookies, so scontent URLs resolve).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from .. import config
from .helpers import extract_hashtags, extract_mentions


def _hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


# yt-dlp cannot read Instagram (and some FB) content without authenticated
# cookies. The orchestrator exports them per platform from the live CDP
# session and registers the file paths here; every yt-dlp call then passes
# --cookies. Keyed by platform.
_COOKIE_FILES: dict[str, str] = {}


def set_cookie_file(platform: str, path: Optional[str]) -> None:
    if path:
        _COOKIE_FILES[platform] = path
    else:
        _COOKIE_FILES.pop(platform, None)


def _cookies_for(url: str) -> list[str]:
    """--cookies args for the platform this URL belongs to (empty if none)."""
    platform = "instagram" if "instagram.com" in url else "facebook"
    path = _COOKIE_FILES.get(platform)
    return ["--cookies", path] if path else []


def ytdlp_metadata(url: str) -> Optional[dict]:
    """Fetch post/reel metadata via yt-dlp without downloading. None on failure."""
    try:
        result = subprocess.run(
            [
                "yt-dlp", "--no-download", "--print-json",
                "--no-warnings", "--quiet", "--socket-timeout", "15",
                *_cookies_for(url),
                url,
            ],
            capture_output=True, text=True, timeout=40,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None

    text = data.get("description") or data.get("title") or ""
    ext = data.get("ext")
    is_video = ext in ("mp4", "webm", "mkv") or "video" in (data.get("format") or "").lower()
    return {
        "post_url": url,
        "text": text,
        "hashtags": extract_hashtags(text),
        "mentions": extract_mentions(text),
        "upload_date": data.get("upload_date"),      # YYYYMMDD
        "timestamp_epoch": data.get("timestamp"),
        "likes": data.get("like_count"),
        "comments": data.get("comment_count"),
        "shares": data.get("repost_count"),
        "views": data.get("view_count"),
        "uploader": data.get("uploader") or data.get("channel") or data.get("uploader_id"),
        "uploader_id": data.get("uploader_id"),
        "thumbnail_url": data.get("thumbnail"),
        "media_id": data.get("id"),
        "duration_s": data.get("duration"),
        "is_video": is_video,
        "width": data.get("width"),
        "height": data.get("height"),
    }


def ytdlp_download(url: str) -> Optional[str]:
    """Download the post's video/media via yt-dlp. Returns a repo-relative path."""
    if not config.DOWNLOAD_MEDIA:
        return None
    h = _hash(url)
    media_dir = config.MEDIA_DIR
    for ext in (".mp4", ".webm", ".mkv", ".jpg", ".webp"):
        p = media_dir / f"{h}{ext}"
        if p.exists() and p.stat().st_size > 10_000:
            return str(p)

    out = str(media_dir / f"{h}.%(ext)s")
    try:
        subprocess.run(
            [
                "yt-dlp", "--no-warnings", "--quiet",
                "-f", "best[ext=mp4]/best",
                "--max-filesize", config.MAX_MEDIA_FILESIZE,
                "-o", out, "--no-playlist",
                "--write-thumbnail", "--socket-timeout", "20",
                *_cookies_for(url),
                url,
            ],
            capture_output=True, text=True, timeout=240,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    for ext in (".mp4", ".webm", ".mkv", ".jpg", ".webp"):
        p = media_dir / f"{h}{ext}"
        if p.exists() and p.stat().st_size > 5_000:
            return str(p)
    return None


async def download_image(page: Page, url: str) -> Optional[str]:
    """Download an image through the authenticated Playwright context."""
    if not config.DOWNLOAD_MEDIA or not url:
        return None
    h = _hash(url)
    existing = list(config.MEDIA_DIR.glob(f"{h}.*"))
    for p in existing:
        if p.stat().st_size > 1_000:
            return str(p)
    try:
        resp = await page.context.request.get(url)
        if not resp.ok:
            return None
        body = await resp.body()
        ct = resp.headers.get("content-type", "")
        ext = ".jpg"
        if "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"
        elif "gif" in ct:
            ext = ".gif"
        dest = config.MEDIA_DIR / f"{h}{ext}"
        dest.write_bytes(body)
        if dest.stat().st_size > 1_000:
            return str(dest)
    except Exception:
        return None
    return None


async def ytdlp_metadata_async(url: str) -> Optional[dict]:
    return await asyncio.to_thread(ytdlp_metadata, url)


async def ytdlp_download_async(url: str) -> Optional[str]:
    return await asyncio.to_thread(ytdlp_download, url)
