"""Normalize and clean extracted post data."""
import re
from typing import List, Dict
from datetime import datetime


def normalize_posts(raw_posts: List[Dict], platform: str, author: str) -> List[Dict]:
    """Normalize a list of raw posts."""
    result = []
    for p in raw_posts:
        n = normalize_single_post(p, platform, author)
        if n:
            result.append(n)
    return result


def normalize_single_post(post: Dict, platform: str, author: str) -> Dict | None:
    text = clean_whitespace(post.get("text", ""))
    media = post.get("media", [])
    if not text and not media:
        return None

    hashtags = post.get("hashtags", [])
    if not hashtags and text:
        hashtags = re.findall(r"#(\w+)", text)

    return {
        "platform": platform,
        "post_url": post.get("post_url"),
        "author": author,
        "text": text,
        "hashtags": hashtags,
        "media": media,
        "timestamp": normalize_timestamp(post.get("timestamp")),
        "location_name": post.get("location_name"),
        "location_lat": post.get("location_lat"),
        "location_lng": post.get("location_lng"),
        "likes": parse_engagement(post.get("likes", 0)),
        "comments": parse_engagement(post.get("comments", 0)),
        "shares": parse_engagement(post.get("shares", 0)),
        "views": parse_engagement(post.get("views", 0)),
    }


def deduplicate_posts(posts: List[Dict]) -> List[Dict]:
    seen = set()
    unique = []
    for p in posts:
        key = p.get("post_url") or (p.get("text", "")[:100] if p.get("text") else None)
        if key and key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def clean_whitespace(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\u00A0", " ", text)
    text = re.sub(r" +", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def normalize_timestamp(ts) -> str | None:
    if not ts:
        return None
    ts = str(ts)
    if "T" in ts:
        return ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    return ts


def parse_engagement(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return 0
        m = re.match(r"^([\d.]+)([KMB]?)$", value, re.IGNORECASE)
        if m:
            num = float(m.group(1))
            suffix = m.group(2).upper()
            if suffix == "K":
                num *= 1000
            elif suffix == "M":
                num *= 1_000_000
            elif suffix == "B":
                num *= 1_000_000_000
            return int(num)
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0
