"""
Meta Scraper v2 — yt-dlp powered extraction for reliable data.

Usage:
    python -m app.main scrape --platform facebook --target NVIDIA --max-posts 10
    python -m app.main scrape --platform instagram --target natgeo --max-posts 10
"""
import asyncio
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.browser.browser import BrowserManager
from app.scraper.extract import (
    # Facebook
    extract_fb_profile, extract_fb_reel_links, extract_fb_post_links,
    extract_fb_reel_metadata_ytdlp, download_fb_reel_ytdlp, download_fb_image,
    # Instagram
    extract_ig_profile, extract_ig_post_links, check_ig_login,
    extract_ig_post_metadata_ytdlp, download_ig_post_ytdlp,
    # Helpers
    _extract_hashtags,
)
from app.pipeline.normalize import normalize_posts, deduplicate_posts
from app.pipeline.store import save_posts
from app.config import DB_PATH, get_cdp_endpoint
from app.database.db import PostDatabase


async def scrape_facebook(target: str, max_posts: int = 10) -> Dict:
    """Scrape a Facebook page: profile + reels + posts."""
    username = target.strip("@/ ")
    started = datetime.now(timezone.utc).isoformat()
    cdp = get_cdp_endpoint("facebook")

    print(f"\n{'='*60}")
    print(f"  FACEBOOK: {username}")
    print(f"{'='*60}")

    async with BrowserManager(cdp_endpoint=cdp) as bm:
        page = await bm.new_page()
        try:
            # 1. Profile
            await page.goto(f"https://www.facebook.com/{username}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            profile_raw = await extract_fb_profile(page)
            profile_data = {"platform": "facebook", "username": username,
                           "profile_url": f"https://www.facebook.com/{username}", **profile_raw}
            print(f"  Profile: {profile_data.get('display_name', username)} ({profile_data.get('follower_count', '?')} followers)")

            # Check if page exists (not redirected to search/404)
            current_url = page.url.lower()
            if "/search/" in current_url or "page_not_found" in current_url or "login" in current_url:
                print(f"  [!] Page '{username}' not found on Facebook. Use exact page names like NVIDIA, Meta, NASA.")
                await page.close()
                return {"platform": "facebook", "target": target, "posts_scraped": 0, "posts_inserted": 0,
                        "error": f"Page '{username}' not found. Use exact FB page names."}

            # 2. Collect reel links
            reel_links = await extract_fb_reel_links(page, username, max_scrolls=max(max_posts // 3, 4))

            # 3. Collect post links
            post_links = await extract_fb_post_links(page, username, max_scrolls=max(max_posts // 3, 4))

            # Combine and limit
            all_links = list(dict.fromkeys(reel_links + post_links))[:max_posts]
            print(f"  Total links to process: {len(all_links)}")

            # 4. Extract metadata + download for each link via yt-dlp
            posts = []
            for i, link in enumerate(all_links):
                print(f"  [{i+1}/{len(all_links)}] {link[:70]}...")

                # Get metadata
                meta = await asyncio.to_thread(extract_fb_reel_metadata_ytdlp, link)
                if not meta:
                    print(f"    Skipped (no metadata)")
                    continue

                # Download video
                video_path = await asyncio.to_thread(download_fb_reel_ytdlp, link)

                # Download thumbnail
                thumb_path = None
                if meta.get("thumbnail_url"):
                    thumb_path = await download_fb_image(page, meta["thumbnail_url"])

                # Build post
                post = {
                    "platform": "facebook",
                    "post_url": link,
                    "author": username,
                    "text": meta.get("text", ""),
                    "hashtags": meta.get("hashtags", []),
                    "timestamp": _format_ytdlp_date(meta.get("timestamp")),
                    "likes": meta.get("likes", 0),
                    "comments": meta.get("comments", 0),
                    "shares": meta.get("shares", 0),
                    "views": meta.get("views", 0),
                    "media": [{
                        "url": link,
                        "type": "video",
                        "alt_text": "",
                        "thumbnail_url": meta.get("thumbnail_url", ""),
                        "local_path": video_path,
                        "local_thumbnail": thumb_path,
                    }],
                }
                posts.append(post)
                print(f"    OK: {len(meta.get('text','')[:40])} chars, {meta.get('views',0)} views, video={'yes' if video_path else 'no'}")

            # 5. Pipeline: normalize -> dedup -> store
            normalized = normalize_posts(posts, "facebook", username)
            normalized = deduplicate_posts(normalized)
            inserted = save_posts(normalized, profile_data=profile_data)

            with PostDatabase(DB_PATH) as db:
                db.log_scrape("facebook", target, len(normalized), started)

            return {"platform": "facebook", "target": target,
                    "posts_scraped": len(normalized), "posts_inserted": inserted}
        finally:
            await page.close()


async def scrape_instagram(target: str, max_posts: int = 10) -> Dict:
    """Scrape an Instagram profile or hashtag."""
    is_hashtag = target.startswith("#")
    username = target.lstrip("#@/ ")
    started = datetime.now(timezone.utc).isoformat()
    cdp = get_cdp_endpoint("instagram")

    print(f"\n{'='*60}")
    print(f"  INSTAGRAM: {'#' + username if is_hashtag else '@' + username}")
    print(f"{'='*60}")

    async with BrowserManager(cdp_endpoint=cdp) as bm:
        page = await bm.new_page()
        try:
            if is_hashtag:
                url = f"https://www.instagram.com/explore/tags/{username}/"
            else:
                url = f"https://www.instagram.com/{username}/"

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Check login
            logged_in = await check_ig_login(page)
            if not logged_in:
                print(f"  [!] Not logged in to Instagram. Open port 9223 Chrome and log in.")
                return {"platform": "instagram", "target": target, "posts_scraped": 0, "posts_inserted": 0,
                        "error": "Not logged in"}

            # Profile
            profile_raw = await extract_ig_profile(page) if not is_hashtag else {}
            profile_data = {"platform": "instagram",
                           "username": f"#{username}" if is_hashtag else username,
                           "profile_url": url, **profile_raw}
            print(f"  Profile: {profile_data.get('display_name', username)} ({profile_data.get('follower_count', '?')} followers)")

            # Collect post links
            for _ in range(max(max_posts // 12, 3)):
                await page.evaluate("window.scrollBy(0, 600)")
                await asyncio.sleep(1.5)

            post_links = await extract_ig_post_links(page)
            post_links = post_links[:max_posts]
            print(f"  Found {len(post_links)} post links")

            # Extract metadata + download via yt-dlp
            posts = []
            for i, link in enumerate(post_links):
                print(f"  [{i+1}/{len(post_links)}] {link[:60]}...")

                meta = await asyncio.to_thread(extract_ig_post_metadata_ytdlp, link)
                if not meta:
                    print(f"    Skipped")
                    continue

                # Download media
                media_path = await asyncio.to_thread(download_ig_post_ytdlp, link)

                # Download thumbnail
                thumb_path = None
                if meta.get("thumbnail_url"):
                    thumb_path = await download_fb_image(page, meta["thumbnail_url"])

                media_type = "video" if meta.get("has_video") else "image"
                post = {
                    "platform": "instagram",
                    "post_url": link,
                    "author": username,
                    "text": meta.get("text", ""),
                    "hashtags": meta.get("hashtags", []),
                    "timestamp": _format_ytdlp_date(meta.get("timestamp")),
                    "likes": meta.get("likes", 0),
                    "comments": meta.get("comments", 0),
                    "shares": meta.get("shares", 0),
                    "views": meta.get("views", 0),
                    "media": [{
                        "url": link,
                        "type": media_type,
                        "alt_text": "",
                        "thumbnail_url": meta.get("thumbnail_url", ""),
                        "local_path": media_path,
                        "local_thumbnail": thumb_path,
                    }],
                }
                posts.append(post)
                print(f"    OK: {media_type}, {meta.get('likes',0)} likes")

            normalized = normalize_posts(posts, "instagram", username)
            normalized = deduplicate_posts(normalized)
            inserted = save_posts(normalized, profile_data=profile_data)

            with PostDatabase(DB_PATH) as db:
                db.log_scrape("instagram", target, len(normalized), started)

            return {"platform": "instagram", "target": target,
                    "posts_scraped": len(normalized), "posts_inserted": inserted}
        finally:
            await page.close()


def _format_ytdlp_date(date_str: str | None) -> str | None:
    """Convert yt-dlp date (YYYYMMDD) to ISO format."""
    if not date_str or len(date_str) != 8:
        return None
    try:
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T00:00:00Z"
    except:
        return None


async def scrape(platform: str, target: str, max_posts: int = 10, topic_id: int = None) -> Dict:
    if platform == "facebook":
        result = await scrape_facebook(target, max_posts)
    elif platform == "instagram":
        result = await scrape_instagram(target, max_posts)
    else:
        raise ValueError(f"Unknown platform: {platform}")

    if topic_id and result.get("posts_inserted", 0) > 0:
        with PostDatabase(DB_PATH) as db:
            posts = db.get_posts(author=target.strip("#@/ "), platform=platform,
                                limit=result["posts_inserted"])
            for p in posts:
                db.link_post_to_topic(p["id"], topic_id)

    return result


async def scrape_topic(topic: Dict, max_posts_per_item: int = 5) -> Dict:
    """Scrape all keywords and handles for a topic."""
    print(f"\n{'#'*60}")
    print(f"# TOPIC: {topic['name']}")
    print(f"{'#'*60}")

    with PostDatabase(DB_PATH) as db:
        db_topic = db.get_topic_by_name(topic["name"])
        topic_id = db_topic["id"] if db_topic else None

    total_scraped = 0
    total_inserted = 0

    # Scrape handles (pages/profiles)
    for handle in topic.get("handles", []):
        h = handle.lstrip("@")
        for plat in topic.get("platforms", ["facebook"]):
            try:
                r = await scrape(plat, h, max_posts_per_item, topic_id)
                total_scraped += r.get("posts_scraped", 0)
                total_inserted += r.get("posts_inserted", 0)
            except Exception as e:
                print(f"  Error: {plat}/{h}: {e}")
            await asyncio.sleep(2)

    return {"topic": topic["name"], "total_scraped": total_scraped, "total_inserted": total_inserted}


def main():
    parser = argparse.ArgumentParser(description="Meta Scraper")
    sub = parser.add_subparsers(dest="command")
    sp = sub.add_parser("scrape")
    sp.add_argument("--platform", "-p", required=True, choices=["instagram", "facebook"])
    sp.add_argument("--target", "-t", required=True)
    sp.add_argument("--max-posts", "-n", type=int, default=10)
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    result = asyncio.run(scrape(args.platform, args.target, args.max_posts))
    print(f"\nDone! {result['posts_scraped']} posts, {result['posts_inserted']} new.")


if __name__ == "__main__":
    main()
