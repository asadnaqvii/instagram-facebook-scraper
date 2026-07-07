"""Normalisation + dedup for scraped entities.

The extractors already emit typed models, so normalisation here is light:
tidy hashtags/mentions, backfill hashtags from text, drop empty posts, and
de-duplicate within a single SearchResult before it hits the DB (the DB
dedups across runs; this dedups within one run to avoid redundant work).
"""
from __future__ import annotations

from ..models import Post, SearchResult
from ..scraper.helpers import extract_hashtags, extract_mentions


def normalize_post(post: Post) -> Post:
    if post.text:
        if not post.hashtags:
            post.hashtags = extract_hashtags(post.text)
        if not post.mentions:
            post.mentions = extract_mentions(post.text)
    # Normalise hashtag casing.
    post.hashtags = [h.lstrip("#").lower() for h in post.hashtags]
    return post


def deduplicate_posts(posts: list[Post]) -> list[Post]:
    """Collapse duplicate posts by URL, then by (author, text-prefix)."""
    seen_urls: set[str] = set()
    seen_text: set[str] = set()
    out: list[Post] = []
    for p in posts:
        if p.post_url:
            if p.post_url in seen_urls:
                continue
            seen_urls.add(p.post_url)
            out.append(p)
            continue
        key = f"{p.author_username or ''}::{(p.text or '')[:120]}"
        if key.strip(": ") and key in seen_text:
            continue
        seen_text.add(key)
        if p.text or p.media:
            out.append(p)
    return out


def _is_real_post(p: Post) -> bool:
    """Drop failed extractions: a post with no text, no media, and no
    engagement is page chrome that leaked through (e.g. yt-dlp returned
    nothing for a private/deleted post), not real content. Comments alone
    don't qualify — they attach to whatever page loaded."""
    if p.text and p.text.strip():
        return True
    if p.media:
        return True
    if any(v is not None for v in (p.likes, p.comments_count, p.shares, p.views)):
        return True
    return False


def normalize_result(result: SearchResult) -> SearchResult:
    posts = [normalize_post(p) for p in result.posts]
    posts = [p for p in posts if _is_real_post(p)]
    result.posts = deduplicate_posts(posts)
    # Dedup accounts by username, hashtags by tag.
    seen_acc: set[str] = set()
    result.accounts = [
        a for a in result.accounts
        if not (a.username in seen_acc or seen_acc.add(a.username))
    ]
    seen_tag: set[str] = set()
    result.hashtags = [
        h for h in result.hashtags
        if not (h.tag.lower() in seen_tag or seen_tag.add(h.tag.lower()))
    ]
    return result
