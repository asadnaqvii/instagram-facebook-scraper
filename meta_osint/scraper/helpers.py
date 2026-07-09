"""Small parsing helpers shared across the extraction layer."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


_COUNT_RE = re.compile(r"([\d][\d,.]*)\s*([KkMmBb]?)")


def parse_count(text: Optional[str]) -> Optional[int]:
    """Parse a human-formatted count ('1,234', '12.5K', '3M') into an int.

    Returns None when there is no parseable number, so callers can tell
    'absent' apart from a genuine zero.
    """
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    m = _COUNT_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    suffix = m.group(2).upper()
    if suffix == "K":
        n *= 1_000
    elif suffix == "M":
        n *= 1_000_000
    elif suffix == "B":
        n *= 1_000_000_000
    return int(n)


def extract_hashtags(text: Optional[str]) -> list[str]:
    """Return hashtags (without the #), lowercased, de-duplicated in order."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for tag in re.findall(r"#(\w+)", text):
        seen.setdefault(tag.lower(), None)
    return list(seen.keys())


def extract_mentions(text: Optional[str]) -> list[str]:
    """Return @-mentions (without the @), lowercased, de-duplicated in order."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in re.findall(r"@([\w.]+)", text):
        seen.setdefault(m.lower(), None)
    return list(seen.keys())


def parse_relative_time(text: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """Turn a relative age like '10 weeks ago', '3d', 'Yesterday', 'March 5,
    2026' into an ISO-8601 timestamp (approximate for relative forms).

    Meta rarely gives a machine-readable post date; when it does it's usually
    this human phrasing. An approximate date is still enough for recency
    sorting and 'is this newer than last run' checks. Returns None if nothing
    parseable is found.
    """
    if not text:
        return None
    from datetime import timedelta

    t = str(text).strip().lower()
    ref = now or datetime.now(timezone.utc)

    # "just now" / "yesterday"
    if "just now" in t or t in ("now", "a moment ago"):
        return ref.isoformat()
    if "yesterday" in t:
        return (ref - timedelta(days=1)).isoformat()

    # Compact forms: 5m, 3h, 2d, 10w, 6mo, 1y  (Meta uses these in feeds)
    m = re.match(r"^(\d+)\s*(mo|[smhdwy])\b", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
            "mo": timedelta(days=30 * n),
            "y": timedelta(days=365 * n),
        }.get(unit)
        if delta is not None:
            return (ref - delta).isoformat()

    # Worded forms: "10 weeks ago", "3 days ago", "an hour ago"
    m = re.match(r"^(\d+|a|an)\s+(second|minute|hour|day|week|month|year)s?\s+ago", t)
    if m:
        n = 1 if m.group(1) in ("a", "an") else int(m.group(1))
        unit = m.group(2)
        delta = {
            "second": timedelta(seconds=n),
            "minute": timedelta(minutes=n),
            "hour": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
            "year": timedelta(days=365 * n),
        }.get(unit)
        if delta is not None:
            return (ref - delta).isoformat()

    # Absolute forms: "March 5, 2026", "March 5", "5 March 2026"
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%d %B %Y", "%B %d", "%b %d"):
        try:
            dt = datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
            if dt.year == 1900:  # format without a year -> assume current year
                dt = dt.replace(year=ref.year)
                if dt > ref:  # that date hasn't happened this year -> last year
                    dt = dt.replace(year=ref.year - 1)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def ytdlp_date_to_iso(date_str: Optional[str]) -> Optional[str]:
    """Convert yt-dlp's YYYYMMDD upload_date to an ISO-8601 string."""
    if not date_str or len(str(date_str)) != 8:
        return None
    d = str(date_str)
    try:
        dt = datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


def epoch_to_iso(seconds: Optional[int]) -> Optional[str]:
    """Convert a unix timestamp to an ISO-8601 UTC string."""
    if not seconds:
        return None
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


def now_iso() -> str:
    """Current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def clean_text(text: Optional[str], limit: int = 5000) -> str:
    """Collapse whitespace and trim to a sane length."""
    if not text:
        return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def absolute_url(href: str, platform: str) -> str:
    """Turn a possibly-relative href into an absolute URL for the platform."""
    if not href:
        return ""
    if href.startswith("http"):
        return href.split("#")[0]
    base = "https://www.instagram.com" if platform == "instagram" else "https://www.facebook.com"
    return base + href


def _norm(s: str) -> str:
    """Lowercase, collapse to alphanumerics+spaces, single-space — for matching."""
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def keyword_relevancy(
    text: Optional[str],
    hashtags: Optional[list[str]],
    author: Optional[str],
    keywords: Optional[list[str]],
) -> Optional[int]:
    """Score 0-100 how relevant a post is to the keyword(s) that surfaced it.

    A keyword search often returns coincidental matches — e.g. searching
    'defence' surfaces a volleyball post tagged #Defence that is not about
    military defence. This scores each post against its keyword(s) by WHERE the
    keyword actually appears, so on-topic hits rank above keyword-coincidence
    noise. Pure/text-based (no LLM), computed at display time.

    Signals (best keyword wins):
      100  exact hashtag match (keyword == a tag, spaces ignored)
       85  full keyword phrase present in the caption text
       65  every word of a multi-word keyword present in text (not adjacent)
       55  keyword present in the author / display name
       35  a single word of the keyword overlaps text/tags
       15  found via search but keyword not present in the content

    Returns None when there are no keywords to score against.
    """
    if not keywords:
        return None

    # Match against the caption WITHOUT its hashtags/mentions — otherwise a lone
    # coincidental tag (e.g. a volleyball post whose caption ends "#Defence")
    # reads as a genuine caption mention. Hashtags are scored separately, weaker.
    caption_only = re.sub(r"[#@]\w+", " ", text or "")
    text_n = _norm(caption_only)
    author_n = _norm(author)
    tags_n = {_norm(t) for t in (hashtags or []) if t}

    text_words = set(text_n.split())
    tags_nospace = {t.replace(" ", "") for t in tags_n}

    best = 0
    for kw in keywords:
        kw_n = _norm(kw)
        if not kw_n:
            continue
        kw_words = kw_n.split()
        kw_nospace = kw_n.replace(" ", "")

        in_caption = bool(kw_n) and kw_n in text_n
        all_words_in_text = len(kw_words) > 1 and all(w in text_words for w in kw_words)
        in_tags = kw_n in tags_n or kw_nospace in tags_nospace
        in_author = bool(kw_n) and (kw_n in author_n or kw_nospace in author_n.replace(" ", ""))
        partial = any(w in text_words for w in kw_words) or (
            tags_n and any(w in " ".join(tags_n).split() for w in kw_words)
        )

        # Base signal by where the keyword appears (strongest first).
        if in_caption:
            score = 85                       # keyword phrase actually in the caption
        elif all_words_in_text:
            score = 65
        elif in_tags:
            # A hashtag match ALONE is weak — a coincidental tag (e.g. a
            # volleyball post tagged #Defence) shouldn't beat real on-topic
            # content. Only trust it fully if the caption/author corroborates.
            score = 45
        elif in_author:
            score = 55
        elif partial:
            score = 35
        else:
            score = 15                       # linked by search, not in content

        # Corroboration bonus: if the keyword appears in MULTIPLE places
        # (caption + tag, or tag + author), it's very likely genuinely on-topic.
        places = sum([in_caption or all_words_in_text, in_tags, in_author])
        if places >= 2:
            score = min(100, score + 25)

        best = max(best, score)
    return best
