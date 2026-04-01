"""Download media files locally — images via Playwright, videos via yt-dlp."""
import asyncio
import hashlib
import subprocess
import re
from pathlib import Path
from playwright.async_api import Page


MEDIA_DIR = Path("data/media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def url_to_filename(url: str, ext: str = None) -> str:
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    if not ext:
        if ".mp4" in url:
            ext = ".mp4"
        elif ".webp" in url:
            ext = ".webp"
        elif ".png" in url:
            ext = ".png"
        else:
            ext = ".jpg"
    return f"{url_hash}{ext}"


async def download_image_via_page(page: Page, url: str) -> str | None:
    """Download an image using Playwright's request API."""
    filename = url_to_filename(url)
    local_path = MEDIA_DIR / filename

    if local_path.exists() and local_path.stat().st_size > 1000:
        return f"data/media/{filename}"

    try:
        response = await page.context.request.get(url)
        if response.ok:
            body = await response.body()
            ct = response.headers.get("content-type", "")
            if "png" in ct:
                filename = url_to_filename(url, ".png")
            elif "webp" in ct:
                filename = url_to_filename(url, ".webp")
            elif "gif" in ct:
                filename = url_to_filename(url, ".gif")
            local_path = MEDIA_DIR / filename
            local_path.write_bytes(body)
            size_kb = len(body) / 1024
            if size_kb > 1:
                print(f"    DL image: {filename} ({size_kb:.0f}KB)")
                return f"data/media/{filename}"
        return None
    except Exception as e:
        return None


def download_video_ytdlp(url: str, cookies_from: str = None) -> str | None:
    """
    Download a video using yt-dlp (handles DASH, HLS, Facebook's video format).
    Returns local path or None.
    """
    video_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    output_template = str(MEDIA_DIR / f"{video_hash}.%(ext)s")

    # Check if already downloaded
    for ext in [".mp4", ".webm", ".mkv"]:
        existing = MEDIA_DIR / f"{video_hash}{ext}"
        if existing.exists() and existing.stat().st_size > 10000:
            return f"data/media/{video_hash}{ext}"

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "-f", "best[ext=mp4]/best",
        "--max-filesize", "50M",
        "-o", output_template,
        "--no-playlist",
        "--socket-timeout", "15",
        url,
    ]

    try:
        print(f"    DL video: {url[:70]}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        # Find the downloaded file
        for ext in [".mp4", ".webm", ".mkv"]:
            path = MEDIA_DIR / f"{video_hash}{ext}"
            if path.exists() and path.stat().st_size > 1000:
                size_mb = path.stat().st_size / (1024 * 1024)
                print(f"    DL video OK: {path.name} ({size_mb:.1f}MB)")
                return f"data/media/{path.name}"

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "")[:100]
            print(f"    DL video failed: {err}")
        return None

    except subprocess.TimeoutExpired:
        print(f"    DL video timeout: {url[:60]}")
        return None
    except FileNotFoundError:
        print(f"    yt-dlp not found — install with: pip install yt-dlp")
        return None
    except Exception as e:
        print(f"    DL video error: {str(e)[:80]}")
        return None


async def download_video_from_post_url(post_url: str) -> str | None:
    """Download video from a Facebook/Instagram post URL using yt-dlp."""
    if not post_url:
        return None
    return await asyncio.to_thread(download_video_ytdlp, post_url, "chrome")


async def download_post_media(page: Page, media_list: list, post_url: str = None) -> list:
    """Download all media for a post. Images via Playwright, videos via yt-dlp."""
    updated = []
    has_video = any(m.get("type") == "video" for m in media_list)

    for m in media_list:
        url = m.get("url", "")
        if not url:
            updated.append(m)
            continue

        if m.get("type") == "image":
            local = await download_image_via_page(page, url)
            m["local_path"] = local

        # Download thumbnail for videos too
        thumb = m.get("thumbnail_url", "")
        if thumb and thumb != url and not thumb.endswith(".mp4"):
            local_thumb = await download_image_via_page(page, thumb)
            m["local_thumbnail"] = local_thumb

        updated.append(m)

    # Download actual video using yt-dlp — try post URL first, then direct video URL
    if has_video:
        video_path = None
        # Try post URL first (works for reels, watch pages)
        if post_url:
            video_path = await download_video_from_post_url(post_url)
        # If that failed, try each video URL directly
        if not video_path:
            for m in updated:
                if m.get("type") == "video" and m.get("url"):
                    video_path = await download_video_from_post_url(m["url"])
                    if video_path:
                        break
        # Assign the downloaded video to the first video media entry
        if video_path:
            for m in updated:
                if m.get("type") == "video":
                    m["local_path"] = video_path
                    break

    return updated
