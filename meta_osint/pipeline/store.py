"""Persist a normalised SearchResult to the database, with optional LLM
content analysis applied to posts on the way in."""
from __future__ import annotations

from typing import Optional

from ..database.db import PostDatabase
from ..llm.healer import SelectorHealer
from ..models import ContentAnalysis, SearchResult
from .normalize import normalize_result


def store_search_result(
    db: PostDatabase,
    result: SearchResult,
    healer: Optional[SelectorHealer] = None,
    run_id: Optional[int] = None,
    analyze: bool = True,
) -> dict:
    """Normalise, optionally analyse each post, then upsert everything."""
    result = normalize_result(result)

    if analyze and healer is not None:
        for post in result.posts:
            if post.analysis is not None or not post.text:
                continue
            data = healer.analyze(
                post.text,
                {"platform": result.platform.value, "author": post.author_username or ""},
            )
            if data:
                post.analysis = ContentAnalysis(**data)

    counts = db.store_search_result(result, run_id=run_id)
    return counts
