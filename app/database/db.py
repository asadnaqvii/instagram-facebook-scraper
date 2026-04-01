"""SQLite database for storing scraped posts with normalized schema."""
import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timedelta


class PostDatabase:
    """SQLite database for Meta scraper.

    Tables:
        profiles  – scraped profile metadata
        posts     – individual posts (text, timestamp, engagement)
        media     – media items linked to posts (images, videos)
        hashtags  – unique hashtags
        post_hashtags – many-to-many link
        locations – location check-ins
        scrape_log – tracks scrape runs
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = None

    # -- context manager --
    def __enter__(self):
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_tables()
        return self

    def __exit__(self, *exc):
        if self.conn:
            self.conn.close()
            self.conn = None

    # -- schema --
    def _create_tables(self):
        c = self.conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                username TEXT NOT NULL,
                profile_url TEXT,
                display_name TEXT,
                bio TEXT,
                follower_count INTEGER,
                following_count INTEGER,
                post_count INTEGER,
                profile_picture_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, username)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                post_url TEXT UNIQUE,
                profile_id INTEGER,
                author TEXT,
                text TEXT,
                timestamp TEXT,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                views INTEGER DEFAULT 0,
                location_name TEXT,
                location_lat REAL,
                location_lng REAL,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                type TEXT DEFAULT 'image',
                alt_text TEXT,
                thumbnail_url TEXT,
                local_path TEXT,
                local_thumbnail TEXT,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS hashtags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL UNIQUE
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS post_hashtags (
                post_id INTEGER NOT NULL,
                hashtag_id INTEGER NOT NULL,
                PRIMARY KEY (post_id, hashtag_id),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (hashtag_id) REFERENCES hashtags(id) ON DELETE CASCADE
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS topic_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                value TEXT NOT NULL,
                platform TEXT DEFAULT 'instagram',
                FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE,
                UNIQUE(topic_id, item_type, value)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS post_topics (
                post_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                PRIMARY KEY (post_id, topic_id),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS scrape_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                target TEXT NOT NULL,
                topic_id INTEGER,
                posts_count INTEGER DEFAULT 0,
                started_at TIMESTAMP,
                finished_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE SET NULL
            )
        """)

        self.conn.commit()

    # -- profiles --
    def upsert_profile(self, data: dict) -> int:
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO profiles (platform, username, profile_url, display_name, bio,
                                  follower_count, following_count, post_count, profile_picture_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, username) DO UPDATE SET
                display_name = excluded.display_name,
                bio = excluded.bio,
                follower_count = excluded.follower_count,
                following_count = excluded.following_count,
                post_count = excluded.post_count,
                profile_picture_url = excluded.profile_picture_url,
                updated_at = CURRENT_TIMESTAMP
        """, (
            data["platform"], data["username"], data.get("profile_url"),
            data.get("display_name"), data.get("bio"),
            data.get("follower_count"), data.get("following_count"),
            data.get("post_count"), data.get("profile_picture_url"),
        ))
        self.conn.commit()
        # Return the id
        c.execute("SELECT id FROM profiles WHERE platform=? AND username=?",
                  (data["platform"], data["username"]))
        return c.fetchone()[0]

    # -- posts --
    def insert_posts(self, posts: List[Dict], profile_id: int = None) -> int:
        """Insert posts, returning number of new rows inserted."""
        inserted = 0
        c = self.conn.cursor()
        for p in posts:
            post_url = p.get("post_url")
            if not post_url and not p.get("text"):
                continue

            # Dedup by post_url
            if post_url:
                c.execute("SELECT id FROM posts WHERE post_url=?", (post_url,))
                if c.fetchone():
                    continue

            c.execute("""
                INSERT INTO posts (platform, post_url, profile_id, author, text, timestamp,
                                   likes, comments, shares, views,
                                   location_name, location_lat, location_lng)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p.get("platform"), post_url, profile_id,
                p.get("author"), p.get("text"), p.get("timestamp"),
                p.get("likes", 0), p.get("comments", 0),
                p.get("shares", 0), p.get("views", 0),
                p.get("location_name"), p.get("location_lat"), p.get("location_lng"),
            ))
            post_id = c.lastrowid
            inserted += 1

            # Insert media
            for m in p.get("media", []):
                c.execute("""
                    INSERT INTO media (post_id, url, type, alt_text, thumbnail_url, local_path, local_thumbnail)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (post_id, m["url"], m.get("type", "image"),
                      m.get("alt_text"), m.get("thumbnail_url"),
                      m.get("local_path"), m.get("local_thumbnail")))

            # Insert hashtags
            for tag in p.get("hashtags", []):
                c.execute("INSERT OR IGNORE INTO hashtags (tag) VALUES (?)", (tag,))
                c.execute("SELECT id FROM hashtags WHERE tag=?", (tag,))
                h_id = c.fetchone()[0]
                c.execute("INSERT OR IGNORE INTO post_hashtags (post_id, hashtag_id) VALUES (?, ?)",
                          (post_id, h_id))

        self.conn.commit()
        return inserted

    # -- queries --
    def get_posts(self, platform: str = None, author: str = None,
                  search: str = None, hashtag: str = None,
                  limit: int = 50, offset: int = 0,
                  within_hours: int = None) -> List[Dict]:
        query = """
            SELECT p.*, pr.display_name as profile_display_name,
                   pr.profile_picture_url as profile_avatar
            FROM posts p
            LEFT JOIN profiles pr ON p.profile_id = pr.id
            WHERE 1=1
        """
        params = []

        if platform:
            query += " AND p.platform = ?"
            params.append(platform)
        if author:
            query += " AND p.author = ?"
            params.append(author)
        if search:
            query += " AND p.text LIKE ?"
            params.append(f"%{search}%")
        if hashtag:
            query += """ AND p.id IN (
                SELECT ph.post_id FROM post_hashtags ph
                JOIN hashtags h ON ph.hashtag_id = h.id
                WHERE h.tag = ?
            )"""
            params.append(hashtag)
        if within_hours:
            query += " AND p.scraped_at >= datetime('now', ?)"
            params.append(f"-{within_hours} hours")

        query += " ORDER BY p.scraped_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["media"] = self._get_post_media(d["id"])
            d["hashtags"] = self._get_post_hashtags(d["id"])
            result.append(d)
        return result

    def get_post_count(self, platform: str = None, author: str = None,
                       within_hours: int = None) -> int:
        query = "SELECT COUNT(*) FROM posts WHERE 1=1"
        params = []
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        if author:
            query += " AND author = ?"
            params.append(author)
        if within_hours:
            query += " AND scraped_at >= datetime('now', ?)"
            params.append(f"-{within_hours} hours")
        return self.conn.execute(query, params).fetchone()[0]

    def _get_post_media(self, post_id: int) -> List[Dict]:
        rows = self.conn.execute("SELECT * FROM media WHERE post_id=?", (post_id,)).fetchall()
        return [dict(r) for r in rows]

    def _get_post_hashtags(self, post_id: int) -> List[str]:
        rows = self.conn.execute("""
            SELECT h.tag FROM hashtags h
            JOIN post_hashtags ph ON h.id = ph.hashtag_id
            WHERE ph.post_id = ?
        """, (post_id,)).fetchall()
        return [r[0] for r in rows]

    def get_profiles(self, platform: str = None) -> List[Dict]:
        query = "SELECT * FROM profiles WHERE 1=1"
        params = []
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        query += " ORDER BY updated_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["scraped_posts"] = self.get_post_count(author=d["username"])
            result.append(d)
        return result

    def get_profile(self, platform: str, username: str) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM profiles WHERE platform=? AND username=?",
            (platform, username)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["scraped_posts"] = self.get_post_count(author=username)
        return d

    def get_stats(self, within_hours: int = None) -> Dict:
        total = self.get_post_count(within_hours=within_hours)
        ig = self.get_post_count(platform="instagram", within_hours=within_hours)
        fb = self.get_post_count(platform="facebook", within_hours=within_hours)
        profiles = self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        media_count = self.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        hashtag_count = self.conn.execute("SELECT COUNT(*) FROM hashtags").fetchone()[0]

        return {
            "total_posts": total,
            "instagram_posts": ig,
            "facebook_posts": fb,
            "total_profiles": profiles,
            "total_media": media_count,
            "total_hashtags": hashtag_count,
        }

    def get_top_hashtags(self, limit: int = 20) -> List[Dict]:
        rows = self.conn.execute("""
            SELECT h.tag, COUNT(ph.post_id) as count
            FROM hashtags h
            JOIN post_hashtags ph ON h.id = ph.hashtag_id
            GROUP BY h.tag ORDER BY count DESC LIMIT ?
        """, (limit,)).fetchall()
        return [{"tag": r[0], "count": r[1]} for r in rows]

    def get_recent_scrapes(self, limit: int = 20) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM scrape_log ORDER BY finished_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def log_scrape(self, platform: str, target: str, posts_count: int, started_at: str):
        self.conn.execute("""
            INSERT INTO scrape_log (platform, target, posts_count, started_at)
            VALUES (?, ?, ?, ?)
        """, (platform, target, posts_count, started_at))
        self.conn.commit()

    def search_posts(self, query: str, limit: int = 50) -> List[Dict]:
        return self.get_posts(search=query, limit=limit)

    # -- topics --
    def sync_topics(self, topics: list):
        """Sync topics from parsed topics.txt into the database."""
        c = self.conn.cursor()
        for t in topics:
            c.execute("INSERT OR IGNORE INTO topics (name) VALUES (?)", (t["name"],))
            c.execute("SELECT id FROM topics WHERE name=?", (t["name"],))
            tid = c.fetchone()[0]
            # Clear old items, re-insert
            c.execute("DELETE FROM topic_items WHERE topic_id=?", (tid,))
            for kw in t.get("keywords", []):
                for plat in t.get("platforms", ["instagram", "facebook"]):
                    c.execute("INSERT OR IGNORE INTO topic_items (topic_id,item_type,value,platform) VALUES (?,?,?,?)",
                              (tid, "keyword", kw, plat))
            for handle in t.get("handles", []):
                h = handle.lstrip("@")
                for plat in t.get("platforms", ["instagram", "facebook"]):
                    c.execute("INSERT OR IGNORE INTO topic_items (topic_id,item_type,value,platform) VALUES (?,?,?,?)",
                              (tid, "handle", h, plat))
        self.conn.commit()

    def get_topics(self) -> List[Dict]:
        rows = self.conn.execute("SELECT * FROM topics ORDER BY name").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            items = self.conn.execute("SELECT * FROM topic_items WHERE topic_id=?", (d["id"],)).fetchall()
            d["keywords"] = [i["value"] for i in items if i["item_type"] == "keyword"]
            d["handles"] = [i["value"] for i in items if i["item_type"] == "handle"]
            d["platforms"] = list(set(i["platform"] for i in items))
            # Count posts linked to this topic
            d["post_count"] = self.conn.execute(
                "SELECT COUNT(*) FROM post_topics WHERE topic_id=?", (d["id"],)
            ).fetchone()[0]
            result.append(d)
        return result

    def get_topic_by_name(self, name: str) -> Optional[Dict]:
        row = self.conn.execute("SELECT * FROM topics WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        d = dict(row)
        items = self.conn.execute("SELECT * FROM topic_items WHERE topic_id=?", (d["id"],)).fetchall()
        d["keywords"] = [i["value"] for i in items if i["item_type"] == "keyword"]
        d["handles"] = [i["value"] for i in items if i["item_type"] == "handle"]
        d["platforms"] = list(set(i["platform"] for i in items))
        return d

    def link_post_to_topic(self, post_id: int, topic_id: int):
        self.conn.execute("INSERT OR IGNORE INTO post_topics (post_id, topic_id) VALUES (?,?)",
                          (post_id, topic_id))
        self.conn.commit()

    def get_posts_by_topic(self, topic_id: int, limit: int = 50, offset: int = 0) -> List[Dict]:
        rows = self.conn.execute("""
            SELECT p.*, pr.display_name as profile_display_name,
                   pr.profile_picture_url as profile_avatar
            FROM posts p
            LEFT JOIN profiles pr ON p.profile_id = pr.id
            JOIN post_topics pt ON p.id = pt.post_id
            WHERE pt.topic_id = ?
            ORDER BY p.scraped_at DESC LIMIT ? OFFSET ?
        """, (topic_id, limit, offset)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["media"] = self._get_post_media(d["id"])
            d["hashtags"] = self._get_post_hashtags(d["id"])
            result.append(d)
        return result

    def get_topic_post_count(self, topic_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM post_topics WHERE topic_id=?", (topic_id,)
        ).fetchone()[0]
