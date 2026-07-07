"""Facebook extraction.

Entry points mirror the Instagram module:
  * search_keyword() — posts from FB post-search + pages from FB page-search
    (the "entire search results" for a term) + hashtag links.
  * scrape_page()     — a page's info + recent posts/reels.
  * scrape_hashtag()  — a hashtag feed's posts.

FB search results already contain the post text/author/engagement inline, so
we extract those directly from the feed DOM (with self-healing selectors) and
only fall back to yt-dlp for reels/videos where it adds view counts + the
downloadable file.
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
    parse_relative_time,
    ytdlp_date_to_iso,
)


async def _extract_post_timestamp(page: Page) -> str | None:
    """Read a post's date from the permalink page.

    FB puts the date in the title/aria-label of the timestamp link (e.g.
    "Tuesday, 5 March 2026 at 14:30") and shows a relative form ("10w") as the
    link text. We try the absolute title first, then the relative text."""
    try:
        cand = await page.evaluate(r"""
            () => {
                // Timestamp links usually sit near the author, pointing at the
                // permalink, with a title carrying the absolute date.
                const links = [...document.querySelectorAll('a[role="link"]')];
                for (const a of links) {
                    const t = (a.textContent || '').trim();
                    const title = a.getAttribute('aria-label') || a.getAttribute('title') || '';
                    // Absolute date in title.
                    if (/\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},\s*\d{4}/.test(title)) return title;
                    // Relative text like 10w / 3d / 5h.
                    if (/^\d+[wdhms]$/.test(t)) return t;
                    if (/^\d+\s+(week|day|hour|minute|month|year)s?\s+ago$/i.test(t)) return t;
                }
                return '';
            }
        """)
        return parse_relative_time(cand) if cand else None
    except Exception:
        return None

FB = "https://www.facebook.com"


def _as_int(value) -> Optional[int]:
    """Coerce an engagement value (int from JS, or a '1.2K' string) to int.

    Returns None only for a genuinely absent value; a real 0 stays 0."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return parse_count(value)


# ── page / profile info ──────────────────────────────────────────────

async def extract_page_info(page: Page, name: str) -> Account:
    raw = await page.evaluate(
        r"""
        () => {
            const meta = (s) => { const e=document.querySelector(s); return e ? (e.getAttribute('content')||'') : ''; };
            const body = document.body ? document.body.textContent || '' : '';
            let followers = null, likes = null;
            const fm = body.match(/([\d,.]+[KkMmBb]?)\s+(?:followers|people follow)/i);
            if (fm) followers = fm[1];
            const lm = body.match(/([\d,.]+[KkMmBb]?)\s+(?:likes|people like this)/i);
            if (lm) likes = lm[1];
            return {
                title: meta('meta[property="og:title"]'),
                desc: meta('meta[property="og:description"]'),
                img: meta('meta[property="og:image"]'),
                followers, likes,
            };
        }
        """
    )
    return Account(
        platform=Platform.facebook,
        username=name,
        display_name=(raw.get("title") or None),
        profile_url=f"{FB}/{name}",
        bio=(raw.get("desc") or "")[:500] or None,
        profile_picture_url=(raw.get("img") or None),
        follower_count=parse_count(raw.get("followers")),
        likes_count=parse_count(raw.get("likes")),
        scraped_at=now_iso(),
    )


# ── search: posts + pages ────────────────────────────────────────────

async def _extract_feed_posts(page: Page, healer: SelectorHealer, seen_texts: set[str]) -> list[dict]:
    """Pull post dicts out of whatever feed is currently on screen."""
    container = await healer.resolve(
        page, "facebook.search.post_container",
        description="a container wrapping one post in the feed (author, text, media, engagement)",
        min_matches=1,
        expect="text",
    )
    if not container.found:
        return []
    try:
        batch = await page.evaluate(
            r"""(containerSel) => {
                const posts = [];
                document.querySelectorAll(containerSel).forEach(art => {
                    try {
                        const textEls = [...art.querySelectorAll('div[dir="auto"]')];
                        const texts = textEls.map(e => (e.textContent||'').trim()).filter(t => t && t.length > 10);
                        const text = texts.length ? texts.reduce((a,b)=> a.length>b.length?a:b) : '';
                        if (!text || text.length < 12) return;

                        let author = '';
                        const authorEl = art.querySelector('strong a, h3 a, h4 a, a[role="link"] strong');
                        if (authorEl) author = (authorEl.textContent||'').trim();

                        // Permalink. FB search results expose several forms:
                        // /posts/, /permalink/, /reel/, /videos/, /watch, plus
                        // /photo/?fbid=... and story.php?story_fbid=... which are
                        // ALL query-string — so we must NOT blindly strip '?'.
                        // We keep the meaningful id params and drop FB's tracking
                        // junk (__cft__, __tn__, set, etc.).
                        let postUrl = '';
                        const link = art.querySelector(
                            'a[href*="/posts/"], a[href*="/reel/"], a[href*="/videos/"], a[href*="/permalink/"], ' +
                            'a[href*="/watch"], a[href*="story_fbid"], a[href*="/photo/"], a[href*="fbid="]'
                        );
                        if (link) {
                            let href = link.href || link.getAttribute('href') || '';
                            if (href && !href.startsWith('http')) href = 'https://www.facebook.com' + href;
                            try {
                                const u = new URL(href);
                                // Keep only identity params.
                                const keep = new URLSearchParams();
                                for (const k of ['fbid','story_fbid','id','v','set']) {
                                    if (u.searchParams.has(k)) keep.set(k, u.searchParams.get(k));
                                }
                                const qs = keep.toString();
                                postUrl = u.origin + u.pathname + (qs ? '?' + qs : '');
                            } catch (e) {
                                postUrl = href.split('?')[0];
                            }
                        }

                        const images = [];
                        art.querySelectorAll('img[src*="scontent"]').forEach(img => {
                            if ((img.naturalWidth || img.width || 0) > 60) images.push(img.src);
                        });

                        // Engagement. FB renders reaction counts in aria-labels
                        // ("Like: 50 people", "Love: 8 people") and comment/share
                        // counts in button labels — NOT as plain visible text.
                        // Sum the reaction labels; fall back to textContent regex.
                        const num = (s) => {
                            if (!s) return 0;
                            const m = String(s).replace(/,/g,'').match(/([\d.]+)\s*([KkMm]?)/);
                            if (!m) return 0;
                            let v = parseFloat(m[1]);
                            const u = (m[2]||'').toUpperCase();
                            if (u === 'K') v *= 1000; else if (u === 'M') v *= 1000000;
                            return Math.round(v);
                        };
                        let reactions = 0;
                        const labels = [...art.querySelectorAll('[aria-label]')].map(e => e.getAttribute('aria-label') || '');
                        for (const lb of labels) {
                            const rm = lb.match(/^(?:Like|Love|Care|Haha|Wow|Sad|Angry|Support):\s*([\d,.]+)\s*(?:people|person)/i);
                            if (rm) reactions += num(rm[1]);
                        }
                        // Comments / shares from aria-labels or visible text.
                        const engText = art.textContent || '';
                        let comments = 0, shares = 0;
                        for (const lb of labels) {
                            const cm = lb.match(/([\d][\d,.]*[KkMm]?)\s*comments?/i);
                            if (cm && !comments) comments = num(cm[1]);
                            const sm = lb.match(/([\d][\d,.]*[KkMm]?)\s*shares?/i);
                            if (sm && !shares) shares = num(sm[1]);
                        }
                        if (!comments) { const m = engText.match(/([\d][\d,.]*[KkMm]?)\s*comments?/i); if (m) comments = num(m[1]); }
                        if (!shares)   { const m = engText.match(/([\d][\d,.]*[KkMm]?)\s*shares?/i);   if (m) shares = num(m[1]); }
                        if (!reactions){ const m = engText.match(/([\d][\d,.]*[KkMm]?)\s*(?:likes?|reactions?)/i); if (m) reactions = num(m[1]); }

                        posts.push({
                            text: text.slice(0, 2000),
                            author: author.slice(0, 120),
                            post_url: postUrl,
                            images: images.slice(0, 8),
                            likes: reactions,
                            comments: comments,
                            shares: shares,
                        });
                    } catch (e) { /* skip */ }
                });
                return posts;
            }""",
            container.selector,
        )
    except Exception:
        return []

    fresh = []
    for p in batch:
        key = (p.get("text") or "")[:80]
        if key and key not in seen_texts:
            seen_texts.add(key)
            fresh.append(p)
    return fresh


async def _dict_to_post(page: Page, raw: dict, keyword: str, with_video: bool = True) -> Post:
    text = clean_text(raw.get("text"))
    post = Post(
        platform=Platform.facebook,
        post_url=raw.get("post_url") or None,
        author_username=(raw.get("author") or None),
        text=text,
        hashtags=extract_hashtags(text),
        mentions=extract_mentions(text),
        # Engagement values arrive already parsed to ints from the JS layer
        # (aria-label reaction sums); keep ints as-is, coerce strings.
        likes=_as_int(raw.get("likes")),
        comments_count=_as_int(raw.get("comments")),
        shares=_as_int(raw.get("shares")),
        scraped_at=now_iso(),
    )
    # Images.
    for img in (raw.get("images") or [])[: config.MAX_MEDIA_PER_POST]:
        local = await media_mod.download_image(page, img)
        post.media.append(Media(url=img, type=MediaType.image, local_path=local))

    # Video / reel via yt-dlp (adds views + downloadable file).
    url = post.post_url or ""
    if with_video and url.startswith("http") and any(k in url for k in ("/reel/", "/videos/", "/watch")):
        meta = await media_mod.ytdlp_metadata_async(url)
        if meta:
            post.views = meta.get("views")
            post.likes = post.likes or meta.get("likes")
            post.comments_count = post.comments_count or meta.get("comments")
            post.shares = post.shares or meta.get("shares")
            post.timestamp = post.timestamp or ytdlp_date_to_iso(meta.get("upload_date"))
            post.kind = MediaType.video
            local = await media_mod.ytdlp_download_async(url)
            thumb = meta.get("thumbnail_url")
            post.media.insert(
                0,
                Media(
                    url=url, type=MediaType.video, thumbnail_url=thumb,
                    duration_s=meta.get("duration_s"), local_path=local,
                    local_thumbnail=(await media_mod.download_image(page, thumb) if thumb else None),
                ),
            )
    return post


async def search_pages(page: Page, keyword: str, healer: SelectorHealer) -> list[Account]:
    """FB page/profile search results for a keyword."""
    try:
        await page.goto(f"{FB}/search/pages/?q={keyword}", wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
        await scroll_page(page, 3)
    except Exception:
        return []

    res = await healer.resolve(
        page, "facebook.search.account_link",
        description="a link to a Facebook page in search results (page name + url)",
        min_matches=1,
        expect="link",
    )
    selector = res.selector or 'a[role="presentation"][href*="facebook.com/"]'
    try:
        rows = await page.evaluate(
            r"""(sel) => {
                const out = []; const seen = new Set();
                document.querySelectorAll(sel).forEach(a => {
                    let href = (a.href || '').split('?')[0];
                    const m = href.match(/facebook\.com\/([^\/]+)\/?$/);
                    if (!m || m[1].length < 2) return;
                    if (['search','hashtag','groups','watch','marketplace'].includes(m[1])) return;
                    if (seen.has(m[1])) return; seen.add(m[1]);
                    const name = (a.textContent||'').trim() || m[1];
                    if (name.length > 1 && name.length < 100) out.push({ handle: m[1], name, url: href });
                });
                return out;
            }""",
            selector,
        )
    except Exception:
        rows = []

    accounts = []
    for r in rows[: config.MAX_ACCOUNTS_PER_SEARCH]:
        accounts.append(
            Account(
                platform=Platform.facebook,
                username=r["handle"],
                display_name=r.get("name"),
                profile_url=r.get("url"),
                scraped_at=now_iso(),
            )
        )
    return accounts


async def search_keyword(
    page: Page,
    keyword: str,
    healer: SelectorHealer,
    max_posts: int = 15,
    with_comments: bool = True,
    progress=None,
    known_urls: set | None = None,
) -> SearchResult:
    result = SearchResult(platform=Platform.facebook, keyword=keyword, started_at=now_iso())
    known_urls = known_urls or set()

    def _tick(msg: str) -> None:
        if progress:
            progress(msg)

    # 1. Posts from FB post-search. Collect everything from the feed FIRST
    # (author, text, media, engagement are all inline), then only visit
    # permalinks for comments — navigating away mid-collection would blow up
    # the search results page and force a costly re-scroll each time.
    raw_posts: list[dict] = []
    try:
        await page.goto(f"{FB}/search/posts/?q={keyword}", wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
        seen: set[str] = set()
        scrolls = max(max_posts // 2, 8)
        for i in range(scrolls):
            raw_posts.extend(await _extract_feed_posts(page, healer, seen))
            if len(raw_posts) >= max_posts:
                break
            await scroll_page(page, 1)
        for raw in raw_posts[:max_posts]:
            url = raw.get("post_url") or ""
            # Delta scraping: refresh engagement for a post we already have,
            # rather than re-storing it as new.
            if url and url in known_urls:
                result.refreshed.append({
                    "post_url": url,
                    "likes": _as_int(raw.get("likes")),
                    "comments_count": _as_int(raw.get("comments")),
                    "shares": _as_int(raw.get("shares")),
                })
                continue
            try:
                post = await _dict_to_post(page, raw, keyword)
                result.posts.append(post)
                _tick(f"[facebook] {keyword!r}: post {len(result.posts)} (likes={post.likes}, comments={post.comments_count})")
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        result.error = f"post_search_failed: {e}"

    # 2. Pages from FB page-search (navigates away — safe now, feed is done).
    try:
        result.accounts = await search_pages(page, keyword, healer)
        _tick(f"[facebook] {keyword!r}: {len(result.accounts)} pages found")
    except Exception as e:  # noqa: BLE001
        if not result.error:
            result.error = f"page_search_failed: {e}"

    # 3. Comments + timestamp — visit each post permalink now that the feed is
    # fully collected. FB search feeds carry no post date, but the permalink
    # page does (relative like "10w" or an absolute date in a link title), so
    # we grab it here in the same visit we use for comments.
    if with_comments:
        for post in result.posts:
            if not post.post_url:
                continue
            try:
                post.comments = await extract_comments(page, post.post_url, healer)
                if post.comments_count is None:
                    post.comments_count = len(post.comments)
                if post.timestamp is None:
                    ts = await _extract_post_timestamp(page)
                    if ts:
                        post.timestamp = ts
            except Exception:
                pass

    # 4. Record the hashtag form of the keyword.
    tag = keyword.replace(" ", "").lstrip("#")
    if tag:
        result.hashtags.append(Hashtag(tag=tag.lower(), url=f"{FB}/hashtag/{tag}"))

    result.finished_at = now_iso()
    return result


# ── comments ─────────────────────────────────────────────────────────

async def extract_comments(page: Page, url: str, healer: SelectorHealer) -> list[Comment]:
    """Open a post permalink and sample its comments.

    FB marks each comment container with aria-label 'Comment by <name>' — a
    stable anchor we target directly. We also try to switch the comment
    ordering to 'All comments' and click 'view more' so more than the default
    handful load. The healer selector is a fallback if the aria-label anchor
    ever stops matching.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
    except Exception:
        return []

    # Nudge more comments into the DOM: click "View more comments" a few times.
    for _ in range(3):
        try:
            btn = page.locator(
                'div[role="button"]:has-text("View more comments"), '
                'div[role="button"]:has-text("View more"), '
                'span:has-text("View more comments")'
            ).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(1200)
            else:
                break
        except Exception:
            break
    await scroll_page(page, 2)

    # Primary: aria-label="Comment by <name>" containers.
    raw = await page.evaluate(
        r"""() => {
            const out = [];
            const nodes = document.querySelectorAll('div[aria-label^="Comment by"], div[role="article"][aria-label*="Comment"]');
            nodes.forEach(node => {
                let label = node.getAttribute('aria-label') || '';
                // "Comment by John Doe 3 weeks ago" -> strip the trailing time phrase.
                let m = label.match(/Comment by\s+(.+?)$/i);
                let name = m ? m[1].trim() : '';
                name = name.replace(/\s+\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago$/i, '')
                           .replace(/\s+(yesterday|just now)$/i, '').trim();
                // Comment text is the longest dir=auto block that isn't the name
                // or a bare timestamp/UI label.
                const texts = [...node.querySelectorAll('div[dir="auto"], span[dir="auto"]')]
                    .map(e => (e.textContent || '').trim())
                    .filter(t => t && t !== name && t.length > 1 &&
                                 !/^(Like|Reply|Share|Author|Edited)$/i.test(t) &&
                                 !/^\d+\s*(second|minute|hour|day|week|month|year)s?(\s+ago)?$/i.test(t) &&
                                 !/^\d+[wdhms]$/.test(t));
                const text = texts.sort((a, b) => b.length - a.length)[0] || '';
                // profile handle if present
                let username = '';
                const a = node.querySelector('a[href*="facebook.com/"], a[href^="/"]');
                if (a) {
                    const href = a.getAttribute('href') || '';
                    const um = href.match(/facebook\.com\/([^\/?]+)/) || href.match(/^\/([^\/?]+)/);
                    if (um && !['profile.php','photo','groups'].includes(um[1])) username = um[1];
                }
                if (text) out.push({ name, text, username });
            });
            return out.slice(0, 300);
        }"""
    )

    # Fallback: healed selector.
    if not raw:
        res = await healer.resolve(
            page, "facebook.comment.list",
            description="a comment block: commenter name + comment text (aria-label often starts with 'Comment by')",
            min_matches=1, expect="text",
        )
        if res.found:
            try:
                raw = await page.evaluate(
                    r"""(sel) => {
                        const out = [];
                        document.querySelectorAll(sel).forEach(node => {
                            const nameEl = node.querySelector('a[role="link"] strong, strong a, a[href*="facebook.com/"]');
                            const name = nameEl ? (nameEl.textContent||'').trim() : '';
                            const textEls = [...node.querySelectorAll('div[dir="auto"]')].map(e => (e.textContent||'').trim())
                                .filter(t => t && t !== name && t.length > 0);
                            const text = textEls.sort((a,b)=>b.length-a.length)[0] || '';
                            if (text) out.push({ name, text, username: '' });
                        });
                        return out.slice(0, 200);
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
        key = f"{r.get('name','')}::{text[:80]}"
        if key in seen:
            continue
        seen.add(key)
        comments.append(
            Comment(
                platform=Platform.facebook,
                post_url=url,
                author_username=(r.get("username") or None),
                author_display_name=(r.get("name") or None),
                text=text,
                scraped_at=now_iso(),
            )
        )
        if len(comments) >= config.MAX_COMMENTS_PER_POST:
            break
    return comments


# ── page / hashtag entry points ──────────────────────────────────────

async def _collect_links(page: Page, patterns: str, max_scrolls: int) -> list[str]:
    await scroll_page(page, max_scrolls)
    try:
        return await page.evaluate(
            r"""(patterns) => {
                const links = new Set();
                document.querySelectorAll(patterns).forEach(a => {
                    let href = a.href || a.getAttribute('href') || '';
                    if (!href.startsWith('http')) href = 'https://www.facebook.com' + href;
                    href = href.split('?')[0].split('#')[0];
                    links.add(href);
                });
                return [...links];
            }""",
            patterns,
        )
    except Exception:
        return []


async def scrape_page(
    page: Page,
    name: str,
    healer: SelectorHealer,
    max_posts: int = 15,
    with_comments: bool = True,
) -> SearchResult:
    name = name.lstrip("@/ ")
    result = SearchResult(platform=Platform.facebook, keyword=f"@{name}", started_at=now_iso())
    try:
        await page.goto(f"{FB}/{name}", wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
    except Exception as e:  # noqa: BLE001
        result.error = f"nav_failed: {e}"
        result.finished_at = now_iso()
        return result

    if "/search/" in (page.url or "") or "login" in (page.url or "").lower():
        result.error = "page_not_found_or_login"
        result.finished_at = now_iso()
        return result

    result.accounts.append(await extract_page_info(page, name))

    links = await _collect_links(
        page,
        'a[href*="/posts/"], a[href*="/photos/"], a[href*="/videos/"], a[href*="/reel/"]',
        max_scrolls=max(max_posts // 3, 5),
    )
    links = list(dict.fromkeys(links))[:max_posts]
    for link in links:
        try:
            meta = await media_mod.ytdlp_metadata_async(link)
            post = Post(platform=Platform.facebook, post_url=link, author_username=name, scraped_at=now_iso())
            if meta:
                post.text = clean_text(meta.get("text"))
                post.hashtags = meta.get("hashtags") or []
                post.likes = meta.get("likes")
                post.comments_count = meta.get("comments")
                post.shares = meta.get("shares")
                post.views = meta.get("views")
                post.timestamp = ytdlp_date_to_iso(meta.get("upload_date"))
                post.kind = MediaType.video if meta.get("is_video") else MediaType.image
                local = await media_mod.ytdlp_download_async(link)
                thumb = meta.get("thumbnail_url")
                if local or thumb:
                    post.media.append(Media(
                        url=link, type=post.kind, thumbnail_url=thumb, local_path=local,
                        local_thumbnail=(await media_mod.download_image(page, thumb) if thumb else None),
                    ))
            if with_comments:
                try:
                    post.comments = await extract_comments(page, link, healer)
                except Exception:
                    pass
            if post.text or post.media:
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
    result = SearchResult(platform=Platform.facebook, keyword=f"#{tag}", started_at=now_iso())
    result.hashtags.append(Hashtag(tag=tag.lower(), url=f"{FB}/hashtag/{tag}"))
    try:
        await page.goto(f"{FB}/hashtag/{tag}", wait_until="domcontentloaded", timeout=25000)
        await human_delay("action")
    except Exception as e:  # noqa: BLE001
        result.error = f"nav_failed: {e}"
        result.finished_at = now_iso()
        return result

    seen: set[str] = set()
    raw_posts: list[dict] = []
    for _ in range(max(max_posts // 2, 8)):
        raw_posts.extend(await _extract_feed_posts(page, healer, seen))
        if len(raw_posts) >= max_posts:
            break
        await scroll_page(page, 1)

    for raw in raw_posts[:max_posts]:
        try:
            result.posts.append(await _dict_to_post(page, raw, f"#{tag}"))
        except Exception:
            continue

    result.finished_at = now_iso()
    return result
