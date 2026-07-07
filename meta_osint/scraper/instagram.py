"""Instagram extraction.

Covers the three entry points a keyword can hit:
  * search_keyword() — the "entire search results": top accounts, hashtags,
    and places for a term (from IG's web search endpoint), plus posts from
    the matching hashtag feed.
  * scrape_profile()  — a single account's info + recent posts.
  * scrape_hashtag()  — a hashtag feed's posts.

Per post we go deep: caption, media (images + video via yt-dlp), hashtags,
mentions, location, likes, comment count, views, and a sample of comments.

DOM-dependent reads go through the SelectorHealer so they self-repair; the
heavy lifting (metadata, media) goes through yt-dlp so it barely touches the
DOM at all.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from playwright.async_api import Page

from .. import config
from ..browser.manager import human_delay, scroll_page
from ..llm.healer import SelectorHealer
from ..models import (
    Account,
    Comment,
    Hashtag,
    Location,
    Media,
    MediaType,
    Platform,
    Post,
    SearchResult,
)
from . import media as media_mod
from .helpers import (
    absolute_url,
    clean_text,
    extract_hashtags,
    extract_mentions,
    now_iso,
    parse_count,
    ytdlp_date_to_iso,
    epoch_to_iso,
)

IG = "https://www.instagram.com"


# ── login check ──────────────────────────────────────────────────────

async def check_login(page: Page) -> bool:
    url = page.url.lower()
    if "/accounts/login" in url or "/challenge/" in url:
        return False
    try:
        return await page.locator("article, header section, main, nav").first.is_visible(timeout=4000)
    except Exception:
        return "/accounts/login" not in url


# ── profile ──────────────────────────────────────────────────────────

async def extract_profile(page: Page, username: str) -> Account:
    """Read profile metadata. Followers/following/posts + bio live in the
    description meta tag; verified/private/category we sniff from the DOM."""
    raw = await page.evaluate(
        r"""
        () => {
            const meta = (s) => { const e=document.querySelector(s); return e ? (e.getAttribute('content')||'') : ''; };
            const desc = meta('meta[name="description"]');
            const title = meta('meta[property="og:title"]');
            const img = meta('meta[property="og:image"]');
            // Verified badge / private markers from visible text.
            const bodyTxt = document.body ? document.body.textContent || '' : '';
            const isPrivate = /This account is private/i.test(bodyTxt);
            return { desc, title, img, isPrivate };
        }
        """
    )
    followers = following = posts = None
    bio = None
    desc = raw.get("desc") or ""
    import re

    for val, label in re.findall(r"([\d,.]+[KkMmBb]?)\s+(Followers|Following|Posts)", desc):
        n = parse_count(val)
        if label == "Followers":
            followers = n
        elif label == "Following":
            following = n
        elif label == "Posts":
            posts = n
    parts = desc.split(" - ", 1)
    if len(parts) > 1:
        bio = parts[1].split("See Instagram")[0].strip() or None

    display_name = (raw.get("title") or "").split("(")[0].strip() or None

    return Account(
        platform=Platform.instagram,
        username=username,
        display_name=display_name,
        profile_url=f"{IG}/{username}/",
        bio=bio,
        profile_picture_url=raw.get("img") or None,
        is_private=bool(raw.get("isPrivate")) or None,
        follower_count=followers,
        following_count=following,
        post_count=posts,
        scraped_at=now_iso(),
    )


async def collect_post_links(page: Page, limit: int) -> list[str]:
    """Scroll a profile/hashtag grid and gather /p/ and /reel/ links."""
    links: dict[str, None] = {}
    scrolls = max(limit // 10, 3)
    for _ in range(scrolls):
        found = await page.evaluate(
            r"""
            () => {
                const out = [];
                document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]').forEach(a => {
                    const href = a.getAttribute('href');
                    if (href) out.push(href.split('?')[0]);
                });
                return out;
            }
            """
        )
        for href in found:
            full = absolute_url(href, "instagram")
            links.setdefault(full, None)
        if len(links) >= limit:
            break
        await scroll_page(page, 1)
    return list(links.keys())[:limit]


# ── per-post deep extraction ─────────────────────────────────────────

async def extract_post(
    page: Page,
    url: str,
    healer: SelectorHealer,
    author_fallback: Optional[str] = None,
    with_comments: bool = True,
) -> Optional[Post]:
    """Full extraction for a single post URL: yt-dlp metadata + media, plus
    DOM reads (caption/location/comments) with self-healing selectors."""
    # 1. Reliable metadata + media via yt-dlp (DOM-independent).
    meta = await media_mod.ytdlp_metadata_async(url)

    post = Post(platform=Platform.instagram, post_url=url, author_username=author_fallback)
    if meta:
        post.text = clean_text(meta.get("text"))
        post.hashtags = meta.get("hashtags") or extract_hashtags(post.text)
        post.mentions = meta.get("mentions") or extract_mentions(post.text)
        post.likes = meta.get("likes")
        post.comments_count = meta.get("comments")
        post.shares = meta.get("shares")
        post.views = meta.get("views")
        post.author_username = post.author_username or meta.get("uploader")
        post.timestamp = ytdlp_date_to_iso(meta.get("upload_date")) or epoch_to_iso(meta.get("timestamp_epoch"))
        post.post_id = meta.get("media_id")
        post.kind = MediaType.video if meta.get("is_video") else MediaType.image
        thumb = meta.get("thumbnail_url")
        local = await media_mod.ytdlp_download_async(url)
        if local or thumb:
            local_thumb = await media_mod.download_image(page, thumb) if thumb else None
            post.media.append(
                Media(
                    url=url,
                    type=MediaType.video if meta.get("is_video") else MediaType.image,
                    thumbnail_url=thumb,
                    duration_s=meta.get("duration_s"),
                    width=meta.get("width"),
                    height=meta.get("height"),
                    local_path=local,
                    local_thumbnail=local_thumb,
                )
            )
        post.raw_meta = {"source": "yt-dlp"}

    # 2. Navigate for DOM-only fields (caption fallback, location, comments).
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
    except Exception:
        return post if (post.text or post.media) else None

    # Caption fallback if yt-dlp gave nothing.
    if not post.text:
        cap = await _first_text(
            page, healer, "instagram.post.caption",
            "the post caption / main body text written by the author (NOT the like count or username)",
            expect="text",
        )
        if cap:
            post.text = clean_text(cap)
            post.hashtags = post.hashtags or extract_hashtags(post.text)
            post.mentions = post.mentions or extract_mentions(post.text)

    # If neither yt-dlp nor the DOM gave us any real content, this is a failed
    # extraction (private/deleted/unsupported post, or a login/consent wall).
    # Bail out before spending time on location/likes/comments — and crucially
    # before backfilling comments_count, which would make an empty shell look
    # like a real post to the pipeline.
    if not post.text and not post.media:
        return None

    # Location.
    loc = await _extract_location(page, healer)
    if loc:
        post.location = loc

    # Likes fallback from DOM if metadata missed it.
    if post.likes is None:
        like_txt = await _first_text(
            page, healer, "instagram.post.like_count",
            "the like count number under the post (e.g. '48,213 likes')",
            expect="digit",
        )
        post.likes = parse_count(like_txt)

    # Comments sample.
    if with_comments:
        try:
            post.comments = await extract_comments(page, url, healer, post_author=post.author_username)
            if post.comments_count is None:
                post.comments_count = len(post.comments)
        except Exception:
            pass

    return post if (post.text or post.media or post.likes is not None) else post


async def _extract_location(page: Page, healer: SelectorHealer) -> Optional[Location]:
    res = await healer.resolve(
        page, "instagram.post.location",
        description="the post location / place link (an <a> pointing to /explore/locations/)",
        expect="link",
    )
    if not res.found:
        return None
    try:
        data = await page.evaluate(
            r"""(sel) => {
                const a = document.querySelector(sel);
                if (!a) return null;
                return { name: (a.textContent||'').trim(), href: a.getAttribute('href')||'' };
            }""",
            res.selector,
        )
    except Exception:
        return None
    if not data or not data.get("name"):
        return None
    return Location(
        name=data["name"][:200],
        url=absolute_url(data.get("href", ""), "instagram"),
    )


async def extract_comments(
    page: Page, url: str, healer: SelectorHealer, post_author: Optional[str] = None
) -> list[Comment]:
    """Sample comments from a post view. IG's comment DOM is volatile, so this
    anchors on commenter profile links and stays best-effort."""
    # Scroll the comments region so more load (IG lazy-loads them).
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, 500)")
        await page.wait_for_timeout(900)

    # IG dropped the old <ul>/<li> comment markup for a div-based structure, so
    # a fixed selector is unreliable. Instead we anchor on each commenter's
    # profile link (href like "/username/") and climb to the ancestor that also
    # holds the comment text — the structurally stable relationship. `author`
    # (the post's own handle) is excluded so the caption isn't mistaken for a
    # comment, and obvious UI noise ("N likes", "Reply", "Verified") is dropped.
    try:
        raw = await page.evaluate(
            r"""(postAuthor) => {
                const out = [];
                const seen = new Set();
                const NAV = new Set(['reels','explore','profile','reel','direct','stories','accounts','p']);
                const isNoise = (t) => (
                    !t || t.length < 2 ||
                    /^\d[\d,.]*\s*(likes?|views?)$/i.test(t) ||
                    /^(Reply|Verified|Follow|Following|Profile|Reels|Like|Home|Search|Notifications|Messages|Create|More|See translation|View replies|Edited|Author|Original audio)$/i.test(t) ||
                    /^View all \d/i.test(t) ||
                    /^\d+[wdhms]$/.test(t)
                );
                const anchors = document.querySelectorAll("a[href^='/'][role='link'], a[href^='/']");
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (!/^\/[A-Za-z0-9._]+\/$/.test(href)) continue;
                    const user = href.replace(/\//g, '');
                    if (NAV.has(user)) continue;
                    if (postAuthor && user.toLowerCase() === postAuthor.toLowerCase()) continue;
                    let node = a;
                    for (let depth = 0; depth < 7 && node; depth++) {
                        node = node.parentElement;
                        if (!node) break;
                        const dirs = [...node.querySelectorAll("span[dir='auto'], div[dir='auto']")]
                            .map(e => (e.textContent || '').trim())
                            .filter(t => t.length > 0 && t !== user && !isNoise(t));
                        const comment = dirs.sort((a, b) => b.length - a.length)[0];
                        if (comment && comment.length >= 2) {
                            const key = user + '::' + comment.slice(0, 60);
                            if (!seen.has(key)) { seen.add(key); out.push({ username: user, text: comment }); }
                            break;
                        }
                    }
                    if (out.length >= 120) break;
                }
                return out;
            }""",
            post_author,
        )
    except Exception:
        raw = []

    # If the anchor method found nothing, fall back to the healed selector.
    if not raw:
        res = await healer.resolve(
            page, "instagram.comment.list",
            description="a comment: the commenter's username link and their comment text",
            min_matches=1, expect="text",
        )
        if res.found:
            try:
                raw = await page.evaluate(
                    r"""(sel) => {
                        const out = [];
                        document.querySelectorAll(sel).forEach(node => {
                            const link = node.querySelector('a[href^="/"]');
                            const username = link ? link.getAttribute('href').replace(/\//g,'') : '';
                            const spans = [...node.querySelectorAll('span,div')].map(s => (s.textContent||'').trim()).filter(t => t.length > 2);
                            const text = spans.sort((a,b)=>b.length-a.length)[0] || '';
                            if (text) out.push({ username, text });
                        });
                        return out.slice(0, 120);
                    }""",
                    res.selector,
                )
            except Exception:
                raw = []

    comments: list[Comment] = []
    seen: set[str] = set()
    for r in raw:
        text = clean_text(r.get("text"), 1000)
        if not text:
            continue
        key = f"{r.get('username','')}::{text[:80]}"
        if key in seen:
            continue
        seen.add(key)
        comments.append(
            Comment(
                platform=Platform.instagram,
                post_url=url,
                author_username=(r.get("username") or None),
                text=text,
                scraped_at=now_iso(),
            )
        )
        if len(comments) >= config.MAX_COMMENTS_PER_POST:
            break
    return comments


async def _first_text(page: Page, healer: SelectorHealer, key: str, desc: str, expect: str | None = None) -> Optional[str]:
    res = await healer.resolve(page, key, description=desc, expect=expect)
    if not res.found:
        return None
    try:
        return await page.evaluate(
            r"""(sel) => { const e = document.querySelector(sel); return e ? (e.textContent||'').trim() : ''; }""",
            res.selector,
        )
    except Exception:
        return None


# ── search: the "entire search results" ──────────────────────────────

async def search_accounts_and_hashtags(page: Page, keyword: str) -> tuple[list[Account], list[Hashtag], list[Location]]:
    """Use IG's web search endpoint to get top accounts, hashtags, and places
    for a keyword — the mixed result set you see in the IG search box."""
    accounts: list[Account] = []
    hashtags: list[Hashtag] = []
    places: list[Location] = []

    # IG's topsearch endpoint returns structured JSON — far more reliable than
    # scraping the search dropdown DOM.
    try:
        data = await page.evaluate(
            r"""async (kw) => {
                try {
                    const res = await fetch(`/web/search/topsearch/?context=blended&query=${encodeURIComponent(kw)}`,
                        { headers: { 'X-IG-App-ID': '936619743392459' } });
                    if (!res.ok) return null;
                    return await res.json();
                } catch (e) { return null; }
            }""",
            keyword,
        )
    except Exception:
        data = None

    if data:
        for u in (data.get("users") or [])[: config.MAX_ACCOUNTS_PER_SEARCH]:
            user = u.get("user") or {}
            uname = user.get("username")
            if not uname:
                continue
            accounts.append(
                Account(
                    platform=Platform.instagram,
                    username=uname,
                    display_name=user.get("full_name") or None,
                    profile_url=f"{IG}/{uname}/",
                    profile_picture_url=user.get("profile_pic_url") or None,
                    is_verified=user.get("is_verified"),
                    is_private=user.get("is_private"),
                    follower_count=(user.get("follower_count") or None),
                    scraped_at=now_iso(),
                )
            )
        for h in (data.get("hashtags") or [])[: config.MAX_HASHTAGS_PER_SEARCH]:
            tag = (h.get("hashtag") or {}).get("name")
            if not tag:
                continue
            hashtags.append(
                Hashtag(
                    tag=tag.lower(),
                    url=f"{IG}/explore/tags/{tag}/",
                    post_count=(h.get("hashtag") or {}).get("media_count"),
                )
            )
        for p in (data.get("places") or []):
            place = (p.get("place") or {}).get("location") or {}
            name = place.get("name")
            if not name:
                continue
            places.append(
                Location(
                    name=name,
                    id=str(place.get("pk")) if place.get("pk") else None,
                    latitude=place.get("lat"),
                    longitude=place.get("lng"),
                )
            )

    return accounts, hashtags, places


async def search_keyword(
    page: Page,
    keyword: str,
    healer: SelectorHealer,
    max_posts: int = 15,
    with_comments: bool = True,
    progress=None,
    known_urls: set | None = None,
) -> SearchResult:
    """Full keyword search: accounts + hashtags + places + hashtag-feed posts.

    `known_urls` are post URLs already in the DB — they get a cheap engagement
    refresh instead of full re-extraction (delta scraping)."""
    result = SearchResult(platform=Platform.instagram, keyword=keyword, started_at=now_iso())

    def _tick(msg: str) -> None:
        if progress:
            progress(msg)

    # Land on the site first so the fetch() runs same-origin & authenticated.
    try:
        if "instagram.com" not in (page.url or ""):
            await page.goto(f"{IG}/", wait_until="domcontentloaded", timeout=25000)
            await human_delay("action")
    except Exception:
        pass

    if not await check_login(page):
        result.error = "not_logged_in"
        result.finished_at = now_iso()
        return result

    # 1. Accounts + hashtags + places.
    try:
        accounts, hashtags, places = await search_accounts_and_hashtags(page, keyword)
        result.accounts = accounts
        result.hashtags = hashtags
        result.places = places
        _tick(f"[instagram] {keyword!r}: {len(accounts)} accounts, {len(hashtags)} hashtags found; fetching posts...")
    except Exception as e:  # noqa: BLE001
        result.error = f"search_meta_failed: {e}"

    # 2. Posts from the hashtag feed for this keyword (spaces -> single tag).
    known_urls = known_urls or set()
    tag = keyword.replace(" ", "").lstrip("#")
    try:
        await page.goto(f"{IG}/explore/tags/{tag}/", wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
        if await check_login(page):
            links = await collect_post_links(page, max_posts)
            for i, link in enumerate(links):
                # Delta scraping: a post we already have gets a cheap engagement
                # refresh (metadata only, no media re-download, no comments)
                # instead of the full expensive extraction.
                if link in known_urls:
                    meta = await media_mod.ytdlp_metadata_async(link)
                    if meta:
                        result.refreshed.append({
                            "post_url": link,
                            "likes": meta.get("likes"),
                            "comments_count": meta.get("comments"),
                            "views": meta.get("views"),
                        })
                    _tick(f"[instagram] {keyword!r}: refreshed existing post {i+1}/{len(links)}")
                    continue
                try:
                    post = await extract_post(page, link, healer, with_comments=with_comments)
                    if post:
                        result.posts.append(post)
                        _tick(f"[instagram] {keyword!r}: NEW post {len(result.posts)} "
                              f"(likes={post.likes}, {len(post.comments)} comments)")
                except Exception:
                    continue
                await human_delay("action")
    except Exception as e:  # noqa: BLE001
        if not result.error:
            result.error = f"hashtag_feed_failed: {e}"

    result.finished_at = now_iso()
    return result


# ── profile / hashtag entry points ───────────────────────────────────

async def scrape_profile(
    page: Page,
    username: str,
    healer: SelectorHealer,
    max_posts: int = 15,
    with_comments: bool = True,
) -> SearchResult:
    username = username.lstrip("@/ ")
    result = SearchResult(platform=Platform.instagram, keyword=f"@{username}", started_at=now_iso())
    try:
        await page.goto(f"{IG}/{username}/", wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
    except Exception as e:  # noqa: BLE001
        result.error = f"nav_failed: {e}"
        result.finished_at = now_iso()
        return result

    if not await check_login(page):
        result.error = "not_logged_in"
        result.finished_at = now_iso()
        return result

    account = await extract_profile(page, username)
    result.accounts.append(account)

    links = await collect_post_links(page, max_posts)
    for link in links:
        try:
            post = await extract_post(page, link, healer, author_fallback=username, with_comments=with_comments)
            if post:
                result.posts.append(post)
        except Exception:
            continue
        await human_delay("action")

    result.finished_at = now_iso()
    return result


async def scrape_hashtag(
    page: Page,
    tag: str,
    healer: SelectorHealer,
    max_posts: int = 15,
    with_comments: bool = True,
) -> SearchResult:
    tag = tag.lstrip("#").replace(" ", "")
    result = SearchResult(platform=Platform.instagram, keyword=f"#{tag}", started_at=now_iso())
    result.hashtags.append(Hashtag(tag=tag.lower(), url=f"{IG}/explore/tags/{tag}/"))
    try:
        await page.goto(f"{IG}/explore/tags/{tag}/", wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
    except Exception as e:  # noqa: BLE001
        result.error = f"nav_failed: {e}"
        result.finished_at = now_iso()
        return result

    if not await check_login(page):
        result.error = "not_logged_in"
        result.finished_at = now_iso()
        return result

    links = await collect_post_links(page, max_posts)
    for link in links:
        try:
            post = await extract_post(page, link, healer, with_comments=with_comments)
            if post:
                result.posts.append(post)
        except Exception:
            continue
        await human_delay("action")

    result.finished_at = now_iso()
    return result
