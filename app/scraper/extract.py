"""Extract post data from Instagram and Facebook pages."""
from playwright.async_api import Page
from typing import List, Dict
import asyncio
import subprocess
import hashlib
import json
import re
from pathlib import Path


MEDIA_DIR = Path("data/media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────

def _parse_count(text: str) -> int:
    if not text:
        return 0
    text = text.strip().replace(",", "")
    m = re.match(r"([\d.]+)([KMB]?)", text, re.IGNORECASE)
    if not m:
        return 0
    n = float(m.group(1))
    s = m.group(2).upper()
    if s == "K": n *= 1000
    elif s == "M": n *= 1_000_000
    elif s == "B": n *= 1_000_000_000
    return int(n)


def _extract_hashtags(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"#(\w+)", text)


# ── Facebook ─────────────────────────────────────────────────────

async def extract_fb_profile(page: Page) -> Dict:
    """Extract page metadata from Open Graph tags."""
    return await page.evaluate(r"""
        () => {
            const get = (sel) => {
                const el = document.querySelector(sel);
                return el ? el.getAttribute('content') : '';
            };
            let followers = null;
            const body = document.body?.textContent || '';
            const fm = body.match(/([\d,.]+[KkMm]?)\s+(?:followers|people follow)/i);
            if (fm) {
                let n = fm[1].replace(/,/g, '');
                const m2 = n.match(/([\d.]+)([KkMm]?)/);
                if (m2) {
                    let v = parseFloat(m2[1]);
                    const s = (m2[2] || '').toUpperCase();
                    if (s === 'K') v *= 1000;
                    if (s === 'M') v *= 1000000;
                    followers = Math.round(v);
                }
            }
            return {
                display_name: get('meta[property="og:title"]'),
                bio: (get('meta[property="og:description"]') || '').slice(0, 500),
                follower_count: followers,
                following_count: null,
                post_count: null,
                profile_picture_url: get('meta[property="og:image"]'),
            };
        }
    """)


async def extract_fb_reel_links(page: Page, target: str, max_scrolls: int = 6) -> List[str]:
    """Navigate to the reels tab and collect reel URLs."""
    url = f"https://www.facebook.com/{target}/reels/"
    print(f"  Navigating to reels: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(4)

    for _ in range(max_scrolls):
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(1.2)

    links = await page.evaluate(r"""
        () => {
            const links = new Set();
            document.querySelectorAll('a[href*="/reel/"]').forEach(a => {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\/reel\/(\d+)/);
                if (m) links.add('https://www.facebook.com/reel/' + m[1] + '/');
            });
            return [...links];
        }
    """)
    print(f"  Found {len(links)} reel links")
    return links


async def extract_fb_post_links(page: Page, target: str, max_scrolls: int = 6) -> List[str]:
    """Navigate to page and collect post/photo/video links."""
    url = f"https://www.facebook.com/{target}"
    print(f"  Navigating to page: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(4)

    for _ in range(max_scrolls):
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(1.2)

    links = await page.evaluate(r"""
        () => {
            const links = new Set();
            document.querySelectorAll('a[href*="/posts/"], a[href*="/photos/"], a[href*="/videos/"]').forEach(a => {
                let href = a.href || a.getAttribute('href') || '';
                if (!href.startsWith('http')) href = 'https://www.facebook.com' + href;
                href = href.split('?')[0].split('#')[0];
                if (href.match(/\/(posts|photos|videos)\//)) links.add(href);
            });
            return [...links];
        }
    """)
    print(f"  Found {len(links)} post links")
    return links


def extract_fb_reel_metadata_ytdlp(reel_url: str) -> Dict | None:
    """Use yt-dlp to extract reel metadata WITHOUT downloading."""
    try:
        cmd = [
            "yt-dlp", "--no-download", "--print-json",
            "--no-warnings", "--quiet",
            "--socket-timeout", "15",
            reel_url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        text = data.get("description") or data.get("title") or ""
        return {
            "post_url": reel_url,
            "text": text,
            "hashtags": _extract_hashtags(text),
            "timestamp": data.get("upload_date"),  # YYYYMMDD format
            "likes": data.get("like_count", 0) or 0,
            "comments": data.get("comment_count", 0) or 0,
            "shares": data.get("repost_count", 0) or 0,
            "views": data.get("view_count", 0) or 0,
            "author": data.get("uploader") or data.get("channel") or "",
            "thumbnail_url": data.get("thumbnail") or "",
            "video_id": data.get("id") or "",
            "duration": data.get("duration"),
            "media": [{"url": reel_url, "type": "video", "alt_text": "", "thumbnail_url": data.get("thumbnail", "")}],
        }
    except Exception as e:
        print(f"    yt-dlp metadata error: {str(e)[:60]}")
        return None


def download_fb_reel_ytdlp(reel_url: str) -> str | None:
    """Download a Facebook reel video via yt-dlp. Returns local path."""
    video_hash = hashlib.md5(reel_url.encode()).hexdigest()[:12]

    # Check already downloaded
    for ext in [".mp4", ".webm"]:
        p = MEDIA_DIR / f"{video_hash}{ext}"
        if p.exists() and p.stat().st_size > 10000:
            return f"data/media/{video_hash}{ext}"

    output = str(MEDIA_DIR / f"{video_hash}.%(ext)s")
    try:
        cmd = [
            "yt-dlp", "--no-warnings", "--quiet",
            "-f", "best[ext=mp4]/best",
            "--max-filesize", "50M",
            "-o", output,
            "--no-playlist",
            "--socket-timeout", "20",
            reel_url,
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=180)

        for ext in [".mp4", ".webm", ".mkv"]:
            p = MEDIA_DIR / f"{video_hash}{ext}"
            if p.exists() and p.stat().st_size > 10000:
                size_mb = p.stat().st_size / (1024 * 1024)
                print(f"    Downloaded: {p.name} ({size_mb:.1f}MB)")
                return f"data/media/{p.name}"
        return None
    except Exception as e:
        print(f"    Download error: {str(e)[:60]}")
        return None


async def download_fb_image(page: Page, url: str) -> str | None:
    """Download an image via Playwright (has cookies)."""
    file_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    local = MEDIA_DIR / f"{file_hash}.jpg"
    if local.exists() and local.stat().st_size > 1000:
        return f"data/media/{file_hash}.jpg"
    try:
        resp = await page.context.request.get(url)
        if resp.ok:
            body = await resp.body()
            ct = resp.headers.get("content-type", "")
            ext = ".jpg"
            if "png" in ct: ext = ".png"
            elif "webp" in ct: ext = ".webp"
            local = MEDIA_DIR / f"{file_hash}{ext}"
            local.write_bytes(body)
            if local.stat().st_size > 1000:
                return f"data/media/{local.name}"
        return None
    except:
        return None


# ── Instagram ────────────────────────────────────────────────────

async def check_ig_login(page: Page) -> bool:
    """Check if we're logged into Instagram. Returns True if logged in."""
    url = page.url.lower()
    if "/accounts/login" in url or "/challenge/" in url:
        return False
    # Try to find profile content (only shows when logged in)
    try:
        has_content = await page.locator("article, header section, main").first.is_visible(timeout=3000)
        return has_content
    except:
        return "/accounts/login" not in url


async def extract_ig_profile(page: Page) -> Dict:
    """Extract profile metadata from meta tags."""
    return await page.evaluate(r"""
        () => {
            const get = (sel) => {
                const el = document.querySelector(sel);
                return el ? (el.getAttribute('content') || '') : '';
            };
            function pc(t) {
                if (!t) return 0;
                t = t.replace(/,/g, '');
                const m = t.match(/([\d.]+)([KkMm]?)/);
                if (!m) return 0;
                let n = parseFloat(m[1]);
                if (m[2].toUpperCase() === 'K') n *= 1000;
                if (m[2].toUpperCase() === 'M') n *= 1000000;
                return Math.round(n);
            }
            const desc = get('meta[name="description"]');
            let followers=null, following=null, posts=null, bio='';
            const nums = desc.match(/([\d,.]+[KkMm]?)\s+(Followers|Following|Posts)/g) || [];
            for (const m of nums) {
                const p = m.match(/([\d,.]+[KkMm]?)\s+(Followers|Following|Posts)/);
                if (p) {
                    if (p[2]==='Followers') followers = pc(p[1]);
                    else if (p[2]==='Following') following = pc(p[1]);
                    else if (p[2]==='Posts') posts = pc(p[1]);
                }
            }
            const ds = desc.split(' - ');
            if (ds.length > 1) bio = ds.slice(1).join(' - ').split('See Instagram')[0].trim();
            return {
                display_name: (get('meta[property="og:title"]') || '').split('(')[0].trim(),
                bio, follower_count: followers, following_count: following, post_count: posts,
                profile_picture_url: get('meta[property="og:image"]') || null,
            };
        }
    """)


async def extract_ig_post_links(page: Page) -> List[str]:
    """Collect post/reel links from profile page."""
    return await page.evaluate(r"""
        () => {
            const links = new Set();
            document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]').forEach(a => {
                const href = a.getAttribute('href');
                if (href) {
                    const full = href.startsWith('/') ? 'https://www.instagram.com' + href : href;
                    links.add(full.split('?')[0]);
                }
            });
            return [...links];
        }
    """)


def extract_ig_post_metadata_ytdlp(post_url: str) -> Dict | None:
    """Use yt-dlp to extract Instagram post metadata."""
    try:
        cmd = [
            "yt-dlp", "--no-download", "--print-json",
            "--no-warnings", "--quiet",
            "--socket-timeout", "15",
            post_url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        text = data.get("description") or data.get("title") or ""
        return {
            "post_url": post_url,
            "text": text,
            "hashtags": _extract_hashtags(text),
            "timestamp": data.get("upload_date"),
            "likes": data.get("like_count", 0) or 0,
            "comments": data.get("comment_count", 0) or 0,
            "views": data.get("view_count", 0) or 0,
            "shares": 0,
            "author": data.get("uploader") or data.get("channel") or "",
            "thumbnail_url": data.get("thumbnail") or "",
            "has_video": data.get("ext") in ("mp4", "webm") or "video" in (data.get("format") or ""),
            "media": [{
                "url": post_url,
                "type": "video" if data.get("ext") in ("mp4", "webm") else "image",
                "alt_text": "",
                "thumbnail_url": data.get("thumbnail", ""),
            }],
        }
    except Exception as e:
        print(f"    yt-dlp IG metadata error: {str(e)[:60]}")
        return None


def download_ig_post_ytdlp(post_url: str) -> str | None:
    """Download Instagram post media via yt-dlp."""
    media_hash = hashlib.md5(post_url.encode()).hexdigest()[:12]

    for ext in [".mp4", ".webm", ".jpg"]:
        p = MEDIA_DIR / f"{media_hash}{ext}"
        if p.exists() and p.stat().st_size > 10000:
            return f"data/media/{media_hash}{ext}"

    output = str(MEDIA_DIR / f"{media_hash}.%(ext)s")
    try:
        cmd = [
            "yt-dlp", "--no-warnings", "--quiet",
            "-f", "best[ext=mp4]/best",
            "--max-filesize", "50M",
            "-o", output,
            "--no-playlist",
            "--write-thumbnail",
            "--socket-timeout", "20",
            post_url,
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=180)

        for ext in [".mp4", ".webm", ".mkv", ".jpg", ".webp"]:
            p = MEDIA_DIR / f"{media_hash}{ext}"
            if p.exists() and p.stat().st_size > 5000:
                size = p.stat().st_size / 1024
                unit = "KB" if size < 1024 else f"{size/1024:.1f}MB"
                print(f"    Downloaded: {p.name} ({unit})")
                return f"data/media/{p.name}"
        return None
    except Exception as e:
        print(f"    Download error: {str(e)[:60]}")
        return None
