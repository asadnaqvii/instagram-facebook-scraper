"""Store extracted posts into the SQLite database."""
from typing import List, Dict
from app.config import DB_PATH
from app.database.db import PostDatabase


def save_posts(posts: List[Dict], profile_data: Dict = None) -> int:
    """Save posts (and optionally profile) to the database. Returns count of new inserts."""
    with PostDatabase(DB_PATH) as db:
        profile_id = None
        if profile_data:
            profile_id = db.upsert_profile(profile_data)

        inserted = db.insert_posts(posts, profile_id=profile_id)

        stats = db.get_stats()
        print(f"  Saved {inserted} new posts (total in DB: {stats['total_posts']})")
        return inserted
