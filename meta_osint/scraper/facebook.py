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
import base64
import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import Page

from .. import config
from ..browser.manager import (human_delay, human_mouse, scroll_page,
                               detect_and_handle_rate_limit)
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
    keyword_relevancy,
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
                // FB renders the post age as <abbr> ("54m", aria-label
                // "54 minutes ago"). The FIRST one on the page is the post's
                // own; comment ages come later and sit under "Comment by".
                // Anything inside the notifications flyout (a role=dialog that
                // starts with "Notifications") is NOT the post — its 1h/3w
                // abbrs are what an unscoped scan picks up first.
                const isNoise = (el) => {
                    const d = el.closest('[role="dialog"]');
                    if (d && /^\s*notifications/i.test(d.innerText || '')) return true;
                    return !!el.closest('[aria-label^="Comment by"], ul');
                };
                const root = document.querySelector('[role="main"]') || document;
                for (const ab of root.querySelectorAll('abbr')) {
                    if (isNoise(ab)) continue;
                    const lab = (ab.getAttribute('aria-label') || ab.getAttribute('title') || '').trim();
                    const txt = (ab.innerText || ab.textContent || '').trim();
                    if (lab && /\d|an hour|a minute|a day/i.test(lab)) return lab;
                    if (/^\d+\s?[smhdwy]$/.test(txt)) return txt;
                }
                // Timestamp links usually sit near the author, pointing at the
                // permalink, with a title carrying the absolute date.
                const links = [...root.querySelectorAll('a[role="link"]')].filter(a => !isNoise(a));
                for (const a of links) {
                    const t = (a.innerText || a.textContent || '').trim();
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
        if not cand:
            return None
        return (_absolute_to_iso(cand) or _relative_to_iso(cand)
                or parse_relative_time(cand))
    except Exception:
        return None

FB = "https://www.facebook.com"


_REL_SHORT = re.compile(r"^(\d+)\s?([smhdwy])$")            # 5h, 2 d  (lowercase only:
                                                                 # "5M" is a view count)
_REL_LONG = re.compile(
    r"^(\d+)\s*(mins?|minutes?|hrs?|hours?|days?|wks?|weeks?|mos?|months?|yrs?|years?)"
    r"(\s+ago)?$", re.I,
)


def _relative_to_iso(text: Optional[str]) -> Optional[str]:
    """'5h' / '2 d' / '13 mins' / 'yesterday' -> approximate ISO timestamp (UTC).

    FB search cards show the post age as relative text and often carry no
    permalink, so this is the only way those posts get a date at all."""
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    now = datetime.now(timezone.utc)
    if low.startswith("just now") or low == "now":
        return now.isoformat()
    if low.startswith("yesterday"):
        return (now - timedelta(days=1)).isoformat()
    m_a = re.match(r"^(?:about\s+)?an?\s+(second|minute|hour|day|week|month|year)\s+ago$", low)
    if m_a:
        unit = m_a.group(1)
        delta = {"second": timedelta(seconds=1), "minute": timedelta(minutes=1),
                 "hour": timedelta(hours=1), "day": timedelta(days=1),
                 "week": timedelta(weeks=1), "month": timedelta(days=30),
                 "year": timedelta(days=365)}[unit]
        return (now - delta).isoformat()
    m = _REL_SHORT.match(t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n),
                 "w": timedelta(weeks=n), "y": timedelta(days=365 * n)}[u]
        return (now - delta).isoformat()
    m = _REL_LONG.match(low)
    if m:
        n, u = int(m.group(1)), m.group(2)
        if u.startswith("mi"):
            delta = timedelta(minutes=n)
        elif u.startswith("h"):
            delta = timedelta(hours=n)
        elif u.startswith("d"):
            delta = timedelta(days=n)
        elif u.startswith("w"):
            delta = timedelta(weeks=n)
        elif u.startswith("mo"):
            delta = timedelta(days=30 * n)
        else:
            delta = timedelta(days=365 * n)
        return (now - delta).isoformat()
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_ABS_DATE = re.compile(
    r"(?:(?P<d1>\d{1,2})\s+(?P<m1>[a-z]{3,9})\.?(?:\s+(?P<y1>\d{4}))?"      # 8 September 2026
    r"|(?P<m2>[a-z]{3,9})\.?\s+(?P<d2>\d{1,2})(?:,?\s+(?P<y2>\d{4}))?)"     # September 8, 2026
    r"(?:\s+at\s+(?P<hh>\d{1,2}):(?P<mm>\d{2})\s*(?P<ap>am|pm)?)?", re.I,
)


def _absolute_to_iso(text: Optional[str]) -> Optional[str]:
    """'Monday, 8 September 2026 at 10:12' / 'September 8 at 10:12 AM' -> ISO.
    FB puts this on the post-age <abbr>/aria-label; it is exact, unlike the
    '5h' relative text. Year defaults to the current one (FB omits it for
    recent posts)."""
    if not text:
        return None
    t = text.strip()
    now = datetime.now(timezone.utc)
    # "Today at 14:00" / "Yesterday at 9:05 PM" — FB's label for recent posts.
    m_rel = re.match(r"^(today|yesterday)(?:\s+at\s+(\d{1,2}):(\d{2})\s*(am|pm)?)?", t, re.I)
    if m_rel:
        base = now - (timedelta(days=1) if m_rel.group(1).lower() == "yesterday" else timedelta())
        if m_rel.group(2):
            hh, mm, ap = int(m_rel.group(2)), int(m_rel.group(3)), (m_rel.group(4) or "").lower()
            if ap == "pm" and hh < 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
            base = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return base.isoformat()
    m = _ABS_DATE.search(t)
    if not m:
        return None
    mon = (m.group("m1") or m.group("m2") or "").lower()[:3]
    if mon not in _MONTHS:
        return None
    day = int(m.group("d1") or m.group("d2"))
    year = int(m.group("y1") or m.group("y2") or now.year)
    hh = int(m.group("hh") or 0)
    mm = int(m.group("mm") or 0)
    ap = (m.group("ap") or "").lower()
    if ap == "pm" and hh < 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    try:
        dt = datetime(year, _MONTHS[mon], day, hh, mm, tzinfo=timezone.utc)
    except ValueError:
        return None
    # No year given and the date is in the future -> it was last year.
    if not (m.group("y1") or m.group("y2")) and dt > now + timedelta(days=1):
        dt = dt.replace(year=year - 1)
    return dt.isoformat()


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

# FB's "Recent posts" sort is selected via an opaque ?filters= parameter — a
# base64-encoded JSON blob. Build it rather than hardcode it.
_RECENT_FILTER = quote_plus(base64.b64encode(json.dumps(
    {"rp_chrono_sort:0": json.dumps({"name": "chronosort", "args": ""},
                                     separators=(",", ":"))},
    separators=(",", ":"),
).encode("utf-8")).decode("ascii"))


def _dedup_batch(batch: list[dict], seen_texts: set[str]) -> list[dict]:
    """Identity-dedup a batch of raw card dicts (shared by both paths)."""
    fresh = []
    _seen_before = len(seen_texts)
    for p in batch:
        # Identity, best-available. The permalink is the only truly unique key;
        # text is a fallback for posts whose link we couldn't read. Keying on
        # text alone silently DROPPED every image/video post with no caption
        # (very common on FB) because an empty key failed the `if key` test,
        # and collapsed distinct posts that share a boilerplate opener.
        url = (p.get("post_url") or "").strip()
        if url:
            key = "u:" + url
        else:
            text = (p.get("text") or "").strip()
            if text:
                key = "t:" + text[:200]
            else:
                # No link and no text — fall back to author + engagement so a
                # media-only post still counts instead of vanishing.
                key = "a:{}|{}|{}".format(
                    (p.get("author") or "")[:60],
                    p.get("likes"), p.get("comments_count"),
                )
                if key == "a:||None|None":
                    continue  # genuinely empty node, skip
        if key not in seen_texts:
            seen_texts.add(key)
            fresh.append(p)
    # Expose the funnel: how many article nodes the DOM gave us vs how many
    # were new. A large gap means the feed is repeating (scrolled past the end)
    # rather than the extractor failing.
    _last_batch_stats["offered"] = len(batch)
    _last_batch_stats["fresh"] = len(fresh)
    return fresh


async def _extract_one_card(page: Page, posinset: int, seen_texts: set[str]) -> list[dict]:
    """Extract the single card with this aria-posinset, right now.

    FB renders only a handful of cards around the viewport and blanks the rest,
    so a card has to be read while it is in view — scanning the whole feed
    afterwards only ever returns the current window."""
    try:
        batch = await page.evaluate(
            _FEED_CARD_JS, f'div[aria-posinset="{posinset}"]'
        )
    except Exception:
        return []
    return _dedup_batch(batch, seen_texts)


# Last feed-batch funnel numbers (offered by the DOM vs new after dedup).
_last_batch_stats: dict[str, int] = {"offered": 0, "fresh": 0}


_FEED_CARD_JS = r"""(containerSel) => {
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
                        // Candidate permalink shapes, widest first. FB mixes many
                        // forms in one feed and adds new ones (e.g. /share/p/),
                        // so a narrow list silently drops ~half the results.
                        const LINK_SEL = [
                            'a[href*="/posts/"]', 'a[href*="/permalink/"]',
                            'a[href*="/reel/"]', 'a[href*="/videos/"]', 'a[href*="/video.php"]',
                            'a[href*="/watch"]', 'a[href*="story_fbid"]', 'a[href*="/story.php"]',
                            'a[href*="/photo/"]', 'a[href*="/photo.php"]', 'a[href*="fbid="]',
                            'a[href*="/groups/"][href*="/posts/"]',
                            'a[href*="/share/p/"]', 'a[href*="/share/v/"]', 'a[href*="/share/r/"]',
                            'a[href*="/stories/"]',
                            'a[href*="pfbid"]', 'a[href*="/events/"]', 'a[href*="/notes/"]'
                        ].join(', ');

                        const normalize = (raw) => {
                            let href = raw || '';
                            if (!href) return '';
                            if (href.startsWith('//')) href = 'https:' + href;
                            else if (href.startsWith('/')) href = 'https://www.facebook.com' + href;
                            if (!href.startsWith('http')) return '';
                            // Ignore obvious non-permalinks.
                            if (/\/(login|privacy|policies|help|settings|hashtag)\//.test(href)) return '';
                            try {
                                const u = new URL(href);
                                if (!/facebook\.com$|fb\.watch$/.test(u.hostname.replace(/^m\.|^web\./, ''))
                                    && !u.hostname.endsWith('facebook.com')) return '';
                                const keep = new URLSearchParams();
                                for (const k of ['fbid','story_fbid','id','v','set']) {
                                    if (u.searchParams.has(k)) keep.set(k, u.searchParams.get(k));
                                }
                                const qs = keep.toString();
                                return u.origin + u.pathname + (qs ? '?' + qs : '');
                            } catch (e) {
                                return href.split('?')[0];
                            }
                        };

                        // 1) Any matching anchor inside the article.
                        for (const a of art.querySelectorAll(LINK_SEL)) {
                            const cand = normalize(a.href || a.getAttribute('href'));
                            if (cand) { postUrl = cand; break; }
                        }
                        // 2) Fallback: the timestamp link. FB renders the post date
                        // as an <a> whose text is a relative time ("5h", "2 d") and
                        // which points at the canonical permalink — often the only
                        // link present on text-only posts.
                        if (!postUrl) {
                            for (const a of art.querySelectorAll('a[role="link"], a[href]')) {
                                const t = (a.textContent || '').trim();
                                if (!t || t.length > 24) continue;
                                if (!/^(just now|yesterday|\d+\s*(s|m|h|d|w|y|min|hr|hrs|mins|hours?|days?|weeks?|years?)\b)/i.test(t)
                                    && !/^\d{1,2}\s+\w+/.test(t)) continue;
                                const cand = normalize(a.href || a.getAttribute('href'));
                                if (cand) { postUrl = cand; break; }
                            }
                        }
                        // 3) Last resort: aria-label="..." permalink anchors.
                        if (!postUrl) {
                            const al = art.querySelector('a[aria-label*="ermalink"], a[aria-label*="Full story"]');
                            if (al) postUrl = normalize(al.href || al.getAttribute('href'));
                        }

                        // Post age as FB renders it ("5h", "2 d", "13 mins",
                        // "yesterday"). Lowercase single-letter units only —
                        // "5M"/"12K" are view/like counts, not times.
                        let timeAgo = '';
                        let timeAbs = '';
                        const inComment = (el) => {
                            const d = el.closest('[role="dialog"]');
                            if (d && /^\s*notifications/i.test(d.innerText || '')) return true;
                            return !!el.closest('[aria-label^="Comment by"], ul');
                        };
                        const dateLike = /\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b/i;
                        const relLike = /^\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago$/i;
                        // 1) The post-age <abbr>: FB puts "54 minutes ago" (or an
                        //    absolute date) in its aria-label and "54m" as text.
                        //    First non-comment <abbr> in the card is the post's own.
                        for (const ab of art.querySelectorAll('abbr')) {
                            if (inComment(ab)) continue;
                            const lab = (ab.getAttribute('aria-label') || ab.getAttribute('title') || '').trim();
                            const txt = (ab.textContent || '').trim();
                            if (lab && lab.length < 80 && (relLike.test(lab) || (dateLike.test(lab) && /\d/.test(lab)) || /^(today|yesterday)/i.test(lab))) { timeAbs = lab; break; }
                            if (/^\d+\s?[smhdwy]$/.test(txt)) { timeAgo = txt; break; }
                        }
                        // 1b) Absolute/relative label on a link or span.
                        if (!timeAbs && !timeAgo) for (const el of art.querySelectorAll('a[aria-label], span[aria-label]')) {
                            if (inComment(el)) continue;
                            const lab = el.getAttribute('aria-label') || '';
                            if (lab && lab.length < 80 && (relLike.test(lab) || (dateLike.test(lab) && /\d/.test(lab)) || /^(today|yesterday)/i.test(lab))) { timeAbs = lab; break; }
                        }
                        // 2) Relative text ("5h", "2 d") — lowercase single-letter
                        //    units only; "5M"/"12K" are counts. Skip comment blocks.
                        if (!timeAbs && !timeAgo) {
                            for (const el of art.querySelectorAll('a[role="link"], a[href], abbr, span')) {
                                if (inComment(el)) continue;
                                const tt = (el.textContent || '').trim();
                                if (!tt || tt.length > 14) continue;
                                if (/^\d+\s?[smhdwy]$/.test(tt) ||
                                    /^(just now|yesterday|\d+\s*(mins?|minutes?|hrs?|hours?|days?|weeks?|months?|years?)(\s+ago)?)$/i.test(tt)) {
                                    timeAgo = tt; break;
                                }
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
                            time_ago: timeAgo,
                            time_abs: timeAbs,
                            images: images.slice(0, 8),
                            likes: reactions,
                            comments: comments,
                            shares: shares,
                        });
                    } catch (e) { /* skip */ }
                });
                return posts;
            }"""


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
            _FEED_CARD_JS,
            container.selector,
        )
    except Exception:
        return []

    return _dedup_batch(batch, seen_texts)


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
    if raw.get("time_abs"):
        post.timestamp = (_absolute_to_iso(raw.get("time_abs"))
                          or _relative_to_iso(raw.get("time_abs")))
    if not post.timestamp and raw.get("time_ago"):
        post.timestamp = _relative_to_iso(raw.get("time_ago"))
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
    seen: set[str] = set()
    filtered_out = 0

    async def _harvest_feed(url: str, label: str, want: int) -> int:
        """Navigate to a feed URL and pull article posts into raw_posts.

        Returns the number of NEW raw posts gained. Crucially, it detects FB
        bouncing us to the home feed / a login wall — the extractor would
        happily read that as "results" and store random feed posts under the
        keyword (the DRDO 0-of-4 symptom)."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await human_delay("action")
            # Close any open flyout (e.g. the notifications panel) so its
            # timestamps and text don't get read as post content.
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            _tick(f"[facebook] {keyword!r}: {label}: navigation failed — {type(e).__name__}")
            return 0
        final = page.url or ""
        expect = "/search/" if "/search/" in url else ("/hashtag/" if "/hashtag/" in url else None)
        if expect and expect not in final:
            _tick(f"[facebook] {keyword!r}: {label}: FB redirected to {final[:70]} — "
                  f"not serving results for this query/account; surface skipped")
            return 0
        low = final.lower()
        if "/login" in low or "checkpoint" in low:
            _tick(f"[facebook] {keyword!r}: {label}: login/checkpoint wall — skipped")
            return 0
        if await detect_and_handle_rate_limit(page, "facebook", progress):
            return 0
        before = len(raw_posts)
        # FB VIRTUALISES the results list: it keeps many cards in the DOM but
        # renders content only for those near the viewport, blanking the rest.
        # (Measured live: a card reported textLen=0, then 756 after
        # scrollIntoView.) So scrolling and scanning whatever is painted always
        # caps out at ~4 posts. Instead walk each card index into view, let it
        # render, and extract it.
        idx = 0                      # next aria-posinset to visit
        misses = 0                   # consecutive indices that yielded nothing
        steps = 0
        max_steps = min(max(want * 3, 30), config.FB_MAX_FEED_CARDS)
        while len(raw_posts) - before < want and steps < max_steps:
            steps += 1
            idx += 1
            # Bring card `idx` into view; if it doesn't exist yet, scroll to the
            # bottom so FB appends the next burst, then retry the same index.
            present = await page.evaluate(
                """(n) => {
                    const el = document.querySelector(`div[aria-posinset='${n}']`);
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    return true;
                }""",
                idx,
            )
            if not present:
                idx -= 1                      # retry this index after loading more
                await scroll_page(page, 1)
                try:
                    await page.wait_for_timeout(
                        int(config.FB_FEED_SETTLE_MS * random.uniform(0.8, 1.4)))
                except Exception:
                    pass
                misses += 1
                if misses >= config.FB_FEED_STALL_SCROLLS:
                    break
                continue
            # Let the card paint, then read THAT CARD immediately — FB blanks
            # it again as soon as it leaves the ~5-card render window.
            try:
                await page.wait_for_timeout(
                    int(config.FB_FEED_SETTLE_MS * random.uniform(0.5, 0.9)))
            except Exception:
                pass
            b = len(raw_posts)
            raw_posts.extend(await _extract_one_card(page, idx, seen))
            misses = 0 if len(raw_posts) > b else misses + 1
            if misses >= config.FB_FEED_STALL_SCROLLS:
                break
            await human_mouse(page)
        gained = len(raw_posts) - before
        _tick(f"[facebook] {keyword!r}: {label}: +{gained} post(s) "
              f"from {idx} card(s) visited")
        return gained

    async def _harvest_reels(url: str, want: int) -> int:
        """Reels search is a link grid, not article cards: gather /reel/ links
        and let _dict_to_post pull caption/likes/views/date via yt-dlp."""
        label = "reels search"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await human_delay("action")
        except Exception as e:  # noqa: BLE001
            _tick(f"[facebook] {keyword!r}: {label}: navigation failed — {type(e).__name__}")
            return 0
        final = page.url or ""
        if "/search/" not in final:
            _tick(f"[facebook] {keyword!r}: {label}: FB redirected to {final[:70]} — skipped")
            return 0
        low = final.lower()
        if "/login" in low or "checkpoint" in low:
            _tick(f"[facebook] {keyword!r}: {label}: login/checkpoint wall — skipped")
            return 0
        if await detect_and_handle_rate_limit(page, "facebook", progress):
            return 0
        links = await _collect_links(
            page, 'a[href*="/reel/"]',
            max_scrolls=min(max(want, 8), config.FB_MAX_FEED_SCROLLS),
        )
        have = {r.get("post_url") for r in raw_posts if r.get("post_url")}
        gained = 0
        for link in links:
            if gained >= want:
                break
            if not link or link in have or "/reel/" not in link:
                continue
            have.add(link)
            seen.add("u:" + link)
            # Minimal raw dict; _dict_to_post enriches it through yt-dlp.
            raw_posts.append({"post_url": link, "text": "", "author": "",
                              "images": [], "is_reel": True})
            gained += 1
        _tick(f"[facebook] {keyword!r}: {label}: +{gained} reel link(s) "
              f"({len(links)} on page)")
        return gained

    # 1. Posts from several search surfaces. Collect everything from the feeds
    # FIRST (author, text, media, engagement are inline), then only visit
    # permalinks later for comments/dates.
    q = quote_plus(keyword)
    tag = keyword.replace(" ", "").lstrip("#")
    surface_urls = {
        "posts":   (f"{FB}/search/posts/?q={q}", "post search"),
        "recent":  (f"{FB}/search/posts/?q={q}&filters={_RECENT_FILTER}", "recent posts"),
        "videos":  (f"{FB}/search/videos/?q={q}", "video search"),
        "reels":   (f"{FB}/search/reels/?q={q}", "reels search"),
        "hashtag": (f"{FB}/hashtag/{tag}", f"#{tag} page"),
    }
    surfaces = [x.strip() for x in config.FB_SEARCH_SURFACES.split(",") if x.strip()]
    # Recency mode: FB's chronological filter goes first, so a periodic run
    # collects the newest posts rather than FB's "top posts" ranking.
    if config.SORT_MODE == "recent" and "recent" in surfaces:
        surfaces = ["recent"] + [x for x in surfaces if x != "recent"]
    try:
        for sname in surfaces:
            if len(raw_posts) >= max_posts:
                break
            if sname not in surface_urls:
                _tick(f"[facebook] {keyword!r}: unknown surface {sname!r} ignored")
                continue
            url, label = surface_urls[sname]
            if sname == "reels":
                await _harvest_reels(url, max_posts - len(raw_posts))
            else:
                await _harvest_feed(url, label, max_posts - len(raw_posts))
        _tick(f"[facebook] {keyword!r}: search surfaces yielded {len(raw_posts)} raw post(s) "
              f"(target {max_posts})")

        # Multi-word keywords carry context ("DRDO missile"): require every
        # word / the phrase / an author match, not just one stray word.
        min_rel = (config.FB_SEARCH_MIN_RELEVANCE_MULTI if len(keyword.split()) > 1
                   else config.FB_SEARCH_MIN_RELEVANCE)
        fresh_days = config.FRESHNESS_DAYS
        fresh_cutoff = (datetime.now(timezone.utc) - timedelta(days=fresh_days)) if fresh_days > 0 else None
        stale = 0
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
            except Exception:
                continue
            # Relevance gate: FB search results are personalised and frequently
            # off-topic. Don't store posts that show no keyword signal at all.
            if min_rel > 0:
                score = keyword_relevancy(
                    post.text, post.hashtags,
                    post.author_display_name or post.author_username, [keyword],
                )
                if score is not None and score < min_rel:
                    filtered_out += 1
                    continue
            # Freshness: drop posts we KNOW are older than the window.
            if fresh_cutoff and post.timestamp:
                try:
                    _ts = datetime.fromisoformat(post.timestamp.replace("Z", "+00:00"))
                    if _ts.tzinfo is None:
                        _ts = _ts.replace(tzinfo=timezone.utc)
                    if _ts < fresh_cutoff:
                        stale += 1
                        continue
                except ValueError:
                    pass
            post.raw_meta = {**(post.raw_meta or {}), "source": "search"}
            result.posts.append(post)
            _tick(f"[facebook] {keyword!r}: post {len(result.posts)} "
                  f"(likes={post.likes}, comments={post.comments_count})")
        if filtered_out:
            _tick(f"[facebook] {keyword!r}: dropped {filtered_out} off-topic search "
                  f"result(s) (relevance < {min_rel})")
        if stale:
            _tick(f"[facebook] {keyword!r}: dropped {stale} post(s) older than "
                  f"{fresh_days} day(s) (FRESHNESS_DAYS)")
    except Exception as e:  # noqa: BLE001
        result.error = f"post_search_failed: {e}"

    # 2. Pages from FB page-search (navigates away — safe now, feed is done).
    try:
        result.accounts = await search_pages(page, keyword, healer)
        _tick(f"[facebook] {keyword!r}: {len(result.accounts)} pages found")
    except Exception as e:  # noqa: BLE001
        if not result.error:
            result.error = f"page_search_failed: {e}"

    # 2b. Feeds of the discovered pages — the richest on-topic source by far.
    # A page that matched the keyword posts about that topic constantly,
    # whereas search hands back a thin, personalised slice. Independent
    # sources, so they can run in parallel tabs (SOURCE_CONCURRENCY).
    n_pages = config.FB_PAGE_FEEDS
    if n_pages > 0 and result.accounts:
        pass
        # Only pages whose NAME matches the keyword. FB's page search also
        # returns unrelated pages; scraping those feeds is where off-topic
        # posts came from (run 111: an "Autozone scam" post under DRDO).
        _cands = [a for a in result.accounts if a.username]
        targets, _skipped_pages = [], []
        for a in _cands:
            # Score each keyword WORD against the page name so a multi-word
            # keyword ("DRDO missile") still recognises a page called DPIDRDO.
            _gate_words = [w for w in keyword.split() if len(w) >= 3] or keyword.split()
            _name_score = max(
                (keyword_relevancy("", [], f"{a.display_name or ''} {a.username}", [w]) or 0)
                for w in _gate_words)
            (targets if _name_score >= config.PAGE_MIN_RELEVANCE else _skipped_pages).append(a)
        if _skipped_pages:
            _tick(f"[facebook] {keyword!r}: skipping {len(_skipped_pages)} discovered "
                  f"page(s) whose name doesn't match the keyword "
                  f"(e.g. @{_skipped_pages[0].username})")
        targets = targets[:n_pages]
        conc = max(1, min(config.SOURCE_CONCURRENCY, max(1, len(targets))))
        _tick(f"[facebook] {keyword!r}: scraping feeds of {len(targets)} discovered "
              f"page(s), {config.FB_POSTS_PER_PAGE} posts each, concurrency {conc}")
        have_urls = {p.post_url for p in result.posts if p.post_url} | set(known_urls)
        queue: list = list(targets)

        async def _page_feed(acct, tab) -> list:
            got: list = []
            try:
                r = await scrape_page(tab, acct.username, healer,
                                      config.FB_POSTS_PER_PAGE, with_comments)
                _pg_drop = 0
                _pg_stale = 0
                _pg_min = (config.FB_SEARCH_MIN_RELEVANCE_MULTI if len(keyword.split()) > 1
                           else config.FB_SEARCH_MIN_RELEVANCE)
                _pg_cutoff = ((datetime.now(timezone.utc) - timedelta(days=config.FRESHNESS_DAYS))
                              if config.FRESHNESS_DAYS > 0 else None)
                for p in r.posts:
                    if p.post_url and p.post_url in have_urls:
                        continue
                    # Same gate as search results: a matching page can still
                    # post off-topic content ("World Peace Now", 2016).
                    if _pg_min > 0:
                        _sc = keyword_relevancy(p.text, p.hashtags,
                                                p.author_display_name or p.author_username or acct.username,
                                                [keyword])
                        if _sc is not None and _sc < _pg_min:
                            _pg_drop += 1
                            continue
                    if _pg_cutoff and p.timestamp:
                        try:
                            _pts = datetime.fromisoformat(p.timestamp.replace("Z", "+00:00"))
                            if _pts.tzinfo is None:
                                _pts = _pts.replace(tzinfo=timezone.utc)
                            if _pts < _pg_cutoff:
                                _pg_stale += 1
                                continue
                        except ValueError:
                            pass
                    if p.post_url:
                        have_urls.add(p.post_url)
                    p.raw_meta = {**(p.raw_meta or {}), "source": f"page:{acct.username}",
                                  "keyword": keyword}
                    got.append(p)
                _tick(f"[facebook] {keyword!r}: page @{acct.username}: +{len(got)} post(s)"
                      + (f", {_pg_drop} off-topic dropped" if _pg_drop else "")
                      + (f", {_pg_stale} stale dropped" if _pg_stale else "")
                      + (f" ({r.error})" if r.error else ""))
            except Exception as e:  # noqa: BLE001
                _tick(f"[facebook] {keyword!r}: page @{acct.username} failed — "
                      f"{type(e).__name__}: {str(e)[:60]}")
            return got

        async def _worker(tab) -> list:
            out: list = []
            while queue:
                acct = queue.pop(0)
                out.extend(await _page_feed(acct, tab))
                if queue:
                    await human_delay("action")
            return out

        if conc == 1:
            result.posts.extend(await _worker(page))
        else:
            tabs = []
            try:
                for _ in range(conc):
                    tabs.append(await page.context.new_page())
                gathered = await asyncio.gather(*(_worker(tb) for tb in tabs),
                                                return_exceptions=True)
                for g in gathered:
                    if isinstance(g, list):
                        result.posts.extend(g)
            finally:
                for tb in tabs:
                    try:
                        await tb.close()
                    except Exception:
                        pass

    # 3. Comments + timestamp — visit each post permalink now that the feed is
    # fully collected. FB search feeds carry no post date, but the permalink
    # page does (relative like "10w" or an absolute date in a link title), so
    # we grab it here in the same visit we use for comments.
    # NOTE: the timestamp lives on the permalink page, so this visit is needed
    # even when comments are disabled — previously both were behind
    # `if with_comments:`, which is why --no-comments also produced posts with
    # no date at all (FB timestamps were 3% populated).
    need_visit = [pp for pp in result.posts if pp.post_url and
                  (with_comments or pp.timestamp is None) and
                  not str((pp.raw_meta or {}).get("source", "")).startswith("page:")]
    _enriched = 0
    _no_url = sum(1 for pp in result.posts if not pp.post_url)
    if _no_url:
        _tick(f"[facebook] {keyword!r}: {_no_url} post(s) have no permalink — "
              f"cannot fetch their comments/timestamp")
    for post in need_visit:
        try:
            if with_comments:
                post.comments = await extract_comments(page, post.post_url, healer)
                if post.comments_count is None:
                    post.comments_count = len(post.comments)
            else:
                # Still need the page for the date; go there directly.
                await page.goto(post.post_url, wait_until="domcontentloaded",
                                timeout=25000)
                await human_delay("action", "facebook")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            if post.timestamp is None:
                ts = await _extract_post_timestamp(page)
                if ts:
                    post.timestamp = ts
            _enriched += 1
        except Exception as e:  # noqa: BLE001
            _tick(f"[facebook] {keyword!r}: enrich failed for a post — "
                  f"{type(e).__name__}: {str(e)[:70]}")
    if need_visit:
        _dated = sum(1 for pp in result.posts if pp.timestamp)
        _tick(f"[facebook] {keyword!r}: enriched {_enriched}/{len(need_visit)} "
              f"post page(s); {_dated}/{len(result.posts)} now have a timestamp")

    # 3b. Date backfill. FB search cards carry no date element at all (verified
    # live: every card's age lookup returned None), so a post's timestamp can
    # only come from its permalink. Visit the ones still missing a date.
    if config.FB_DATE_BACKFILL:
        undated = [pp for pp in result.posts
                   if pp.post_url and not pp.timestamp
                   and not str((pp.raw_meta or {}).get("source", "")).startswith("page:")]
        undated = undated[:config.FB_DATE_BACKFILL_MAX]
        if undated:
            _got = 0
            for pp in undated:
                try:
                    await page.goto(pp.post_url, wait_until="domcontentloaded", timeout=25000)
                    await human_delay("action", "facebook")
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    ts = await _extract_post_timestamp(page)
                    if ts:
                        pp.timestamp = ts
                        _got += 1
                except Exception:
                    continue
            _tick(f"[facebook] {keyword!r}: date backfill: {_got}/{len(undated)} "
                  f"permalink(s) yielded a date")

    # 4. Record the hashtag form of the keyword.
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
