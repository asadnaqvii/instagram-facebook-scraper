"""Pydantic data models describing everything we extract from IG / FB.

These are the canonical in-memory shapes. The database layer persists them
into a normalised SQLite schema; the pipeline normalises raw scraper dicts
into these before storage. Keeping one field set here means the scraper,
DB, and dashboard never drift apart.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Platform(str, Enum):
    instagram = "instagram"
    facebook = "facebook"


class ResultKind(str, Enum):
    """What a search surfaced. A keyword search on Meta returns a mix."""

    post = "post"
    account = "account"
    hashtag = "hashtag"
    place = "place"


class MediaType(str, Enum):
    image = "image"
    video = "video"
    reel = "reel"
    carousel = "carousel"
    story = "story"


# ── Leaf models ──────────────────────────────────────────────────────


class Media(BaseModel):
    url: str = ""
    type: MediaType = MediaType.image
    alt_text: Optional[str] = None
    thumbnail_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_s: Optional[float] = None
    local_path: Optional[str] = None            # downloaded media file
    local_thumbnail: Optional[str] = None


class Location(BaseModel):
    name: str
    id: Optional[str] = None
    url: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class Hashtag(BaseModel):
    tag: str                                     # without the leading '#'
    url: Optional[str] = None
    post_count: Optional[int] = None             # from hashtag search results


class Comment(BaseModel):
    platform: Platform
    post_url: Optional[str] = None
    comment_id: Optional[str] = None
    author_username: Optional[str] = None
    author_display_name: Optional[str] = None
    author_avatar_url: Optional[str] = None
    text: str = ""
    timestamp: Optional[datetime] = None
    likes: Optional[int] = None
    reply_count: Optional[int] = None
    is_reply: bool = False
    parent_comment_id: Optional[str] = None
    scraped_at: datetime = Field(default_factory=_now)


class Account(BaseModel):
    """A profile (IG) or page/profile (FB)."""

    platform: Platform
    username: str                                # handle, without @
    display_name: Optional[str] = None
    profile_url: Optional[str] = None
    bio: Optional[str] = None
    profile_picture_url: Optional[str] = None
    profile_picture_local: Optional[str] = None
    is_verified: Optional[bool] = None
    is_private: Optional[bool] = None
    category: Optional[str] = None               # e.g. "Public figure"
    external_url: Optional[str] = None           # link-in-bio
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    post_count: Optional[int] = None
    likes_count: Optional[int] = None            # FB page likes
    scraped_at: datetime = Field(default_factory=_now)


class ContentAnalysis(BaseModel):
    """Optional LLM enrichment attached to a post."""

    sentiment: Optional[str] = None              # very_negative..very_positive
    sentiment_score: Optional[float] = None      # -1..1
    entities_people: list[str] = Field(default_factory=list)
    entities_orgs: list[str] = Field(default_factory=list)
    entities_locations: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    language: Optional[str] = None
    model_used: Optional[str] = None


class Post(BaseModel):
    platform: Platform
    post_url: Optional[str] = None
    post_id: Optional[str] = None
    kind: MediaType = MediaType.image
    author_username: Optional[str] = None
    author_display_name: Optional[str] = None
    author_is_verified: Optional[bool] = None
    text: str = ""
    hashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    media: list[Media] = Field(default_factory=list)
    location: Optional[Location] = None
    timestamp: Optional[datetime] = None
    # Engagement
    likes: Optional[int] = None
    comments_count: Optional[int] = None
    shares: Optional[int] = None
    views: Optional[int] = None
    # Sampled comments (not necessarily all of comments_count)
    comments: list[Comment] = Field(default_factory=list)
    # Provenance / enrichment
    analysis: Optional[ContentAnalysis] = None
    raw_meta: Optional[dict] = None              # raw yt-dlp / debug payload
    scraped_at: datetime = Field(default_factory=_now)


class SearchResult(BaseModel):
    """Everything one keyword surfaced on one platform in one run."""

    platform: Platform
    keyword: str
    posts: list[Post] = Field(default_factory=list)
    accounts: list[Account] = Field(default_factory=list)
    hashtags: list[Hashtag] = Field(default_factory=list)
    places: list[Location] = Field(default_factory=list)
    # Already-known posts re-encountered this run, with refreshed engagement
    # only (URL + updated counts). Not re-stored as new — the DB just updates
    # the existing row. Each item: {post_url, likes?, comments_count?, views?}.
    refreshed: list[dict] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_now)
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

    def summary(self) -> dict:
        return {
            "platform": self.platform.value,
            "keyword": self.keyword,
            "posts": len(self.posts),
            "accounts": len(self.accounts),
            "hashtags": len(self.hashtags),
            "places": len(self.places),
            "error": self.error,
        }
