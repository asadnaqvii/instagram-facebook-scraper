"""SQLite persistence for meta_osint.

Normalised schema — one row per real-world entity, linked by association
tables so a post found under three keywords is stored once and referenced
three times. Everything the scraper captures lands here: accounts, posts,
media, comments, hashtags, locations, plus the keyword/search bookkeeping.

Design notes:
  * Natural keys (post_url, account username, hashtag tag, comment_id) carry
    UNIQUE constraints so re-scraping is idempotent — inserts upsert.
  * JSON columns are avoided for anything you'd want to query; media and
    comments get their own tables.
  * A single connection is reused; callers may use the class as a context
    manager.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from .. import config
from ..models import (
    Account,
    Comment,
    Hashtag,
    Location,
    Media,
    Post,
    SearchResult,
)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else dt


def _dt_to_str(v):
    """MySQL DATETIME/DATE -> ISO string (SQLite stores these as text already).
    Uses a space separator to match SQLite's stored format, so `timestamp[:10]`
    still yields the date. Leaves everything else untouched."""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return v


# ── dialect adapter ──────────────────────────────────────────────────
#
# The whole point: the ~30 query methods below are written once, in SQLite
# style (`?` placeholders, `INSERT OR IGNORE`, `dict(row)` access). This thin
# adapter lets those exact method bodies also run on MySQL, so switching
# backends is pure config — no forked query code to drift out of sync.
#
# For MySQL it wraps PyMySQL's DictCursor and, on every execute():
#   * rewrites `?` placeholders to `%s`
#   * translates `INSERT OR IGNORE` -> `INSERT IGNORE`
#   * returns rows that support r["col"], r[0], and dict(r) like sqlite3.Row
# The connection object exposes `.execute(sql, params)` (like sqlite3) and
# `.commit()` / `.cursor()`, so callers can't tell the difference.


class _Row(dict):
    """dict that also allows positional access (r[0]) and dict(r), matching the
    subset of sqlite3.Row behaviour this codebase relies on.

    Also normalizes MySQL DATETIME/DATE columns (which PyMySQL returns as Python
    datetime/date objects) to ISO strings, so they behave exactly like SQLite's
    text timestamps. Without this, template/JSON code that treats a timestamp as
    a string (e.g. `timestamp[:10]`) crashes only on the MySQL backend."""

    __slots__ = ("_order",)

    def __init__(self, mapping):
        norm = {k: _dt_to_str(v) for k, v in mapping.items()}
        super().__init__(norm)
        self._order = list(norm.keys())

    def __getitem__(self, key):
        if isinstance(key, int):
            return super().__getitem__(self._order[key])
        return super().__getitem__(key)

    def keys(self):  # dict(_Row) preserves column order
        return self._order


_PLACEHOLDER_RE = re.compile(r"\?")


_IS_PARAM_RE = re.compile(r"\bIS\s+\?", re.IGNORECASE)


def _to_mysql_sql(sql: str) -> str:
    """Rewrite SQLite-flavoured SQL to MySQL for the shared method bodies."""
    sql = sql.replace("INSERT OR IGNORE", "INSERT IGNORE")
    sql = sql.replace("INSERT OR REPLACE", "REPLACE")
    # SQLite allows `col IS ?` as a null-safe compare; MySQL rejects it
    # (IS only takes NULL/TRUE/FALSE). `<=>` is MySQL's null-safe equality.
    sql = _IS_PARAM_RE.sub("<=> ?", sql)
    # `?` -> `%s`. No string literals in this codebase contain a bare `?`, so a
    # blanket replace is safe and far simpler than a tokenizer.
    sql = _PLACEHOLDER_RE.sub("%s", sql)
    return sql


class _MySQLCursor:
    """Wraps a PyMySQL DictCursor to mimic the sqlite3 cursor surface used here:
    execute() returns self, rows come back as _Row, iteration/fetch* work, and
    lastrowid is exposed."""

    def __init__(self, raw):
        self._c = raw

    def execute(self, sql, params=()):
        if params:
            params = tuple(_normalize_dt_param(p) for p in params)
        self._c.execute(_to_mysql_sql(sql), params)
        return self

    def executemany(self, sql, seq):
        seq = [tuple(_normalize_dt_param(p) for p in row) for row in seq]
        self._c.executemany(_to_mysql_sql(sql), seq)
        return self

    def fetchone(self):
        row = self._c.fetchone()
        return _Row(row) if row is not None else None

    def fetchall(self):
        return [_Row(r) for r in self._c.fetchall()]

    def __iter__(self):
        for r in self._c.fetchall():
            yield _Row(r)

    @property
    def lastrowid(self):
        return self._c.lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount


class _MySQLConn:
    """sqlite3.Connection-compatible facade over a PyMySQL connection."""

    def __init__(self, raw):
        self._conn = raw

    def execute(self, sql, params=()):
        cur = _MySQLCursor(self._conn.cursor())
        return cur.execute(sql, params)

    def cursor(self):
        return _MySQLCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


class PostDatabase:
    def __init__(self, db_path: str | Path | None = None):
        # backend chosen by config; 'mysql' ignores db_path (connects to the
        # server) so every existing `PostDatabase(config.DB_PATH)` call site
        # works unchanged on both backends.
        self.backend = getattr(config, "DB_BACKEND", "sqlite")
        self.db_path = Path(db_path) if db_path is not None else Path(config.DB_PATH)
        if self.backend == "sqlite":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = None

    # ── connection / schema ──────────────────────────────────────────

    def connect(self):
        if self.conn is not None:
            return self.conn
        if self.backend == "mysql":
            self.conn = self._connect_mysql()
        else:
            self.conn = sqlite3.connect(str(self.db_path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        return self.conn

    def _connect_mysql(self) -> "_MySQLConn":
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "MySQL backend selected but PyMySQL is not installed. "
                "Run: pip install pymysql"
            ) from e
        raw = pymysql.connect(
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
            **config.mysql_dsn(),
        )
        return _MySQLConn(raw)

    def _create_schema(self) -> None:
        if self.backend == "mysql":
            self._create_schema_mysql()
            return
        c = self.conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                username TEXT NOT NULL,
                display_name TEXT,
                profile_url TEXT,
                bio TEXT,
                profile_picture_url TEXT,
                profile_picture_local TEXT,
                is_verified INTEGER,
                is_private INTEGER,
                category TEXT,
                external_url TEXT,
                follower_count INTEGER,
                following_count INTEGER,
                post_count INTEGER,
                likes_count INTEGER,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                scraped_at TEXT,
                UNIQUE(platform, username)
            );

            CREATE TABLE IF NOT EXISTS hashtags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                tag TEXT NOT NULL,
                url TEXT,
                post_count INTEGER,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, tag)
            );

            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                name TEXT NOT NULL,
                ext_id TEXT,
                url TEXT,
                latitude REAL,
                longitude REAL,
                UNIQUE(platform, name, ext_id)
            );

            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                post_url TEXT,
                post_id TEXT,
                kind TEXT,
                account_id INTEGER,
                author_username TEXT,
                author_display_name TEXT,
                author_is_verified INTEGER,
                text TEXT,
                location_id INTEGER,
                timestamp TEXT,
                likes INTEGER,
                comments_count INTEGER,
                shares INTEGER,
                views INTEGER,
                -- LLM analysis
                sentiment TEXT,
                sentiment_score REAL,
                entities_people TEXT,
                entities_orgs TEXT,
                entities_locations TEXT,
                topics TEXT,
                language TEXT,
                analysis_model TEXT,
                -- Strategic Intelligence (on-demand AI enrichment)
                strategic_relevance INTEGER,
                strategic_rationale TEXT,
                raw_meta TEXT,
                scraped_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL,
                FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL,
                UNIQUE(platform, post_url)
            );

            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                url TEXT,
                type TEXT,
                alt_text TEXT,
                thumbnail_url TEXT,
                width INTEGER,
                height INTEGER,
                duration_s REAL,
                local_path TEXT,
                local_thumbnail TEXT,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                UNIQUE(post_id, url)
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                platform TEXT,
                comment_id TEXT,
                author_username TEXT,
                author_display_name TEXT,
                author_avatar_url TEXT,
                text TEXT,
                timestamp TEXT,
                likes INTEGER,
                reply_count INTEGER,
                is_reply INTEGER DEFAULT 0,
                parent_comment_id TEXT,
                scraped_at TEXT,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                UNIQUE(post_id, comment_id)
            );

            CREATE TABLE IF NOT EXISTS post_hashtags (
                post_id INTEGER NOT NULL,
                hashtag_id INTEGER NOT NULL,
                PRIMARY KEY (post_id, hashtag_id),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (hashtag_id) REFERENCES hashtags(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Strategic keywords: the analysis lens for AI enrichment. Distinct
            -- from search `keywords` (which drove scraping) — these define what
            -- "strategically relevant" means when scoring posts.
            CREATE TABLE IF NOT EXISTS strategic_keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword_id INTEGER,
                platform TEXT,
                started_at TEXT,
                finished_at TEXT,
                posts_found INTEGER DEFAULT 0,
                accounts_found INTEGER DEFAULT 0,
                hashtags_found INTEGER DEFAULT 0,
                places_found INTEGER DEFAULT 0,
                error TEXT,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
            );

            -- Which keyword/run surfaced which entity (posts primarily).
            CREATE TABLE IF NOT EXISTS result_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL,   -- 'post' | 'account' | 'hashtag' | 'place'
                entity_id INTEGER NOT NULL,
                run_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                UNIQUE(keyword_id, entity_type, entity_id)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform);
            CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_username);
            CREATE INDEX IF NOT EXISTS idx_posts_timestamp ON posts(timestamp);
            CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
            CREATE INDEX IF NOT EXISTS idx_media_post ON media(post_id);
            CREATE INDEX IF NOT EXISTS idx_result_links_kw ON result_links(keyword_id);
            """
        )
        self._migrate()
        self.conn.commit()

    def _create_schema_mysql(self) -> None:
        """Apply schema_mysql.sql (idempotent — all CREATE TABLE IF NOT EXISTS)
        so a fresh MySQL database gets the full 11-table schema. Harmless to run
        every connect on an already-populated DB."""
        sql_file = Path(__file__).resolve().parent / "schema_mysql.sql"
        ddl = sql_file.read_text(encoding="utf-8")
        raw = self.conn._conn  # underlying PyMySQL connection
        with raw.cursor() as cur:
            # Split on ';' at statement boundaries; skip blanks/comment-only.
            for stmt in _split_sql_statements(ddl):
                cur.execute(stmt)
        raw.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent schema upgrades for DBs created before a column existed.

        SQLite path: guarded ALTERs (PRAGMA table_info). MySQL path: the
        schema file already defines the strategic_* columns, so we only add
        them if an older MySQL DB predates them (checked via information_schema).
        Safe to run every connect."""
        c = self.conn.cursor()
        if self.backend == "mysql":
            existing = {
                r["COLUMN_NAME"] if "COLUMN_NAME" in r else r["column_name"]
                for r in c.execute(
                    "SELECT COLUMN_NAME FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = 'posts'"
                )
            }
            if "strategic_relevance" not in existing:
                c.execute("ALTER TABLE posts ADD COLUMN strategic_relevance INT")
            if "strategic_rationale" not in existing:
                c.execute("ALTER TABLE posts ADD COLUMN strategic_rationale TEXT")
            self.conn.commit()
            return
        existing = {r["name"] for r in c.execute("PRAGMA table_info(posts)")}
        if "strategic_relevance" not in existing:
            c.execute("ALTER TABLE posts ADD COLUMN strategic_relevance INTEGER")
        if "strategic_rationale" not in existing:
            c.execute("ALTER TABLE posts ADD COLUMN strategic_rationale TEXT")
        # Index created here (not in the executescript) because the column may
        # not have existed when that ran on an older DB.
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_strategic "
            "ON posts(strategic_relevance)"
        )
        # strategic_keywords table is CREATE IF NOT EXISTS above.

    # ── keyword / run bookkeeping ────────────────────────────────────

    def get_or_create_keyword(self, keyword: str) -> int:
        conn = self.connect()
        cur = conn.execute("SELECT id FROM keywords WHERE keyword = ?", (keyword,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur = conn.execute("INSERT INTO keywords (keyword) VALUES (?)", (keyword,))
        conn.commit()
        return cur.lastrowid

    def start_run(self, keyword: str, platform: str, started_at: str) -> int:
        conn = self.connect()
        kw_id = self.get_or_create_keyword(keyword)
        cur = conn.execute(
            "INSERT INTO search_runs (keyword_id, platform, started_at) VALUES (?, ?, ?)",
            (kw_id, platform, started_at),
        )
        conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, result: SearchResult) -> None:
        conn = self.connect()
        conn.execute(
            """UPDATE search_runs SET finished_at=?, posts_found=?, accounts_found=?,
                   hashtags_found=?, places_found=?, error=? WHERE id=?""",
            (
                _iso(result.finished_at),
                len(result.posts),
                len(result.accounts),
                len(result.hashtags),
                len(result.places),
                result.error,
                run_id,
            ),
        )
        conn.commit()

    def _link_result(self, keyword_id: int, entity_type: str, entity_id: int, run_id: Optional[int]) -> None:
        self.connect().execute(
            """INSERT OR IGNORE INTO result_links (keyword_id, entity_type, entity_id, run_id)
                   VALUES (?, ?, ?, ?)""",
            (keyword_id, entity_type, entity_id, run_id),
        )

    # ── entity upserts ───────────────────────────────────────────────

    def upsert_account(self, account: Account) -> int:
        conn = self.connect()
        cols = {
            "platform": account.platform.value,
            "username": account.username,
            "display_name": account.display_name,
            "profile_url": account.profile_url,
            "bio": account.bio,
            "profile_picture_url": account.profile_picture_url,
            "profile_picture_local": account.profile_picture_local,
            "is_verified": _bool(account.is_verified),
            "is_private": _bool(account.is_private),
            "category": account.category,
            "external_url": account.external_url,
            "follower_count": account.follower_count,
            "following_count": account.following_count,
            "post_count": account.post_count,
            "likes_count": account.likes_count,
            "scraped_at": _iso(account.scraped_at),
        }
        row = conn.execute(
            "SELECT id FROM accounts WHERE platform=? AND username=?",
            (account.platform.value, account.username),
        ).fetchone()
        if row:
            aid = row["id"]
            _update_non_null(conn, "accounts", aid, cols)
        else:
            aid = _insert(conn, "accounts", cols)
        conn.commit()
        return aid

    def upsert_hashtag(self, hashtag: Hashtag, platform: str) -> int:
        conn = self.connect()
        row = conn.execute(
            "SELECT id FROM hashtags WHERE platform=? AND tag=?",
            (platform, hashtag.tag),
        ).fetchone()
        cols = {
            "platform": platform,
            "tag": hashtag.tag,
            "url": hashtag.url,
            "post_count": hashtag.post_count,
        }
        if row:
            hid = row["id"]
            _update_non_null(conn, "hashtags", hid, cols)
        else:
            hid = _insert(conn, "hashtags", cols)
        conn.commit()
        return hid

    def upsert_location(self, loc: Location, platform: str) -> int:
        conn = self.connect()
        row = conn.execute(
            "SELECT id FROM locations WHERE platform=? AND name=? AND IFNULL(ext_id,'')=IFNULL(?,'')",
            (platform, loc.name, loc.id),
        ).fetchone()
        cols = {
            "platform": platform,
            "name": loc.name,
            "ext_id": loc.id,
            "url": loc.url,
            "latitude": loc.latitude,
            "longitude": loc.longitude,
        }
        if row:
            lid = row["id"]
            _update_non_null(conn, "locations", lid, cols)
        else:
            lid = _insert(conn, "locations", cols)
        conn.commit()
        return lid

    def upsert_post(self, post: Post) -> int:
        conn = self.connect()

        account_id = None
        if post.author_username:
            acc_row = conn.execute(
                "SELECT id FROM accounts WHERE platform=? AND username=?",
                (post.platform.value, post.author_username),
            ).fetchone()
            if acc_row:
                account_id = acc_row["id"]

        location_id = None
        if post.location:
            location_id = self.upsert_location(post.location, post.platform.value)

        analysis = post.analysis
        cols = {
            "platform": post.platform.value,
            "post_url": post.post_url,
            "post_id": post.post_id,
            "kind": post.kind.value if hasattr(post.kind, "value") else post.kind,
            "account_id": account_id,
            "author_username": post.author_username,
            "author_display_name": post.author_display_name,
            "author_is_verified": _bool(post.author_is_verified),
            "text": post.text,
            "location_id": location_id,
            "timestamp": _iso(post.timestamp),
            "likes": post.likes,
            "comments_count": post.comments_count,
            "shares": post.shares,
            "views": post.views,
            "sentiment": analysis.sentiment if analysis else None,
            "sentiment_score": analysis.sentiment_score if analysis else None,
            "entities_people": _json(analysis.entities_people) if analysis else None,
            "entities_orgs": _json(analysis.entities_orgs) if analysis else None,
            "entities_locations": _json(analysis.entities_locations) if analysis else None,
            "topics": _json(analysis.topics) if analysis else None,
            "language": analysis.language if analysis else None,
            "analysis_model": analysis.model_used if analysis else None,
            "raw_meta": _json(post.raw_meta) if post.raw_meta else None,
            "scraped_at": _iso(post.scraped_at),
        }

        # Natural key: (platform, post_url). Posts without a URL fall back to
        # (platform, post_id) then a text+author heuristic so we still dedup.
        row = None
        if post.post_url:
            row = conn.execute(
                "SELECT id FROM posts WHERE platform=? AND post_url=?",
                (post.platform.value, post.post_url),
            ).fetchone()
        if row is None and post.post_id:
            row = conn.execute(
                "SELECT id FROM posts WHERE platform=? AND post_id=?",
                (post.platform.value, post.post_id),
            ).fetchone()
        if row is None and not post.post_url and post.text:
            row = conn.execute(
                "SELECT id FROM posts WHERE platform=? AND post_url IS NULL AND author_username IS ? AND substr(text,1,120)=substr(?,1,120)",
                (post.platform.value, post.author_username, post.text),
            ).fetchone()

        if row:
            pid = row["id"]
            _update_non_null(conn, "posts", pid, cols)
            conn.execute("UPDATE posts SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (pid,))
        else:
            pid = _insert(conn, "posts", cols)

        # Children
        for m in post.media:
            self._upsert_media(pid, m)
        for cm in post.comments:
            self._upsert_comment(pid, cm)
        for tag in post.hashtags:
            hid = self.upsert_hashtag(Hashtag(tag=tag.lstrip("#").lower()), post.platform.value)
            conn.execute(
                "INSERT OR IGNORE INTO post_hashtags (post_id, hashtag_id) VALUES (?, ?)",
                (pid, hid),
            )

        conn.commit()
        return pid

    def _upsert_media(self, post_id: int, m: Media) -> None:
        conn = self.connect()
        cols = {
            "post_id": post_id,
            "url": m.url,
            "type": m.type.value if hasattr(m.type, "value") else m.type,
            "alt_text": m.alt_text,
            "thumbnail_url": m.thumbnail_url,
            "width": m.width,
            "height": m.height,
            "duration_s": m.duration_s,
            "local_path": m.local_path,
            "local_thumbnail": m.local_thumbnail,
        }
        row = conn.execute(
            "SELECT id FROM media WHERE post_id=? AND IFNULL(url,'')=IFNULL(?,'')",
            (post_id, m.url),
        ).fetchone()
        if row:
            _update_non_null(conn, "media", row["id"], cols)
        else:
            _insert(conn, "media", cols)

    def _upsert_comment(self, post_id: int, cm: Comment) -> None:
        conn = self.connect()
        cols = {
            "post_id": post_id,
            "platform": cm.platform.value if hasattr(cm.platform, "value") else cm.platform,
            "comment_id": cm.comment_id,
            "author_username": cm.author_username,
            "author_display_name": cm.author_display_name,
            "author_avatar_url": cm.author_avatar_url,
            "text": cm.text,
            "timestamp": _iso(cm.timestamp),
            "likes": cm.likes,
            "reply_count": cm.reply_count,
            "is_reply": 1 if cm.is_reply else 0,
            "parent_comment_id": cm.parent_comment_id,
            "scraped_at": _iso(cm.scraped_at),
        }
        # comment_id may be absent; fall back to (post_id, author, text) dedup.
        row = None
        if cm.comment_id:
            row = conn.execute(
                "SELECT id FROM comments WHERE post_id=? AND comment_id=?",
                (post_id, cm.comment_id),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM comments WHERE post_id=? AND IFNULL(author_username,'')=IFNULL(?,'') AND substr(text,1,120)=substr(?,1,120)",
                (post_id, cm.author_username, cm.text),
            ).fetchone()
        if row:
            _update_non_null(conn, "comments", row["id"], cols)
        else:
            _insert(conn, "comments", cols)

    # ── high-level: persist a whole SearchResult ─────────────────────

    def store_search_result(self, result: SearchResult, run_id: Optional[int] = None) -> dict:
        """Persist every entity in a SearchResult and link it to the keyword."""
        conn = self.connect()
        kw_id = self.get_or_create_keyword(result.keyword)
        counts = {"posts": 0, "accounts": 0, "hashtags": 0, "places": 0}

        for account in result.accounts:
            aid = self.upsert_account(account)
            self._link_result(kw_id, "account", aid, run_id)
            counts["accounts"] += 1

        for hashtag in result.hashtags:
            hid = self.upsert_hashtag(hashtag, result.platform.value)
            self._link_result(kw_id, "hashtag", hid, run_id)
            counts["hashtags"] += 1

        for place in result.places:
            lid = self.upsert_location(place, result.platform.value)
            self._link_result(kw_id, "place", lid, run_id)
            counts["places"] += 1

        for post in result.posts:
            pid = self.upsert_post(post)
            self._link_result(kw_id, "post", pid, run_id)
            counts["posts"] += 1

        conn.commit()
        return counts

    # ── incremental scraping (dedup + engagement refresh) ────────────

    def known_post_urls(self, platform: Optional[str] = None) -> set[str]:
        """All post URLs already stored — the extractor skips these to only
        pull genuinely new content (URL-based delta scraping)."""
        conn = self.connect()
        if platform:
            rows = conn.execute(
                "SELECT post_url FROM posts WHERE platform=? AND post_url IS NOT NULL",
                (platform,),
            )
        else:
            rows = conn.execute("SELECT post_url FROM posts WHERE post_url IS NOT NULL")
        return {r["post_url"] for r in rows}

    def refresh_engagement(self, platform: str, post_url: str, *, likes=None,
                           comments_count=None, shares=None, views=None) -> bool:
        """Cheap update of a known post's engagement counts without touching
        its media/comments/text. Used when a re-run re-encounters a post we
        already have. Returns True if a row was updated."""
        updates = {}
        if likes is not None:
            updates["likes"] = likes
        if comments_count is not None:
            updates["comments_count"] = comments_count
        if shares is not None:
            updates["shares"] = shares
        if views is not None:
            updates["views"] = views
        if not updates:
            return False
        conn = self.connect()
        row = conn.execute(
            "SELECT id FROM posts WHERE platform=? AND post_url=?", (platform, post_url)
        ).fetchone()
        if not row:
            return False
        assignments = ",".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE posts SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [*updates.values(), row["id"]],
        )
        # Still link this post to the (possibly new) keyword that re-found it.
        conn.commit()
        return True

    def link_existing_post(self, platform: str, post_url: str, keyword: str,
                           run_id: Optional[int] = None) -> None:
        """Attach a keyword to an already-stored post (so re-finding a post
        under a new keyword still records that classification)."""
        conn = self.connect()
        row = conn.execute(
            "SELECT id FROM posts WHERE platform=? AND post_url=?", (platform, post_url)
        ).fetchone()
        if not row:
            return
        kw_id = self.get_or_create_keyword(keyword)
        self._link_result(kw_id, "post", row["id"], run_id)
        conn.commit()

    # ── read helpers (dashboard / CLI) ───────────────────────────────

    def get_stats(self) -> dict:
        conn = self.connect()
        q = lambda sql: conn.execute(sql).fetchone()[0]
        return {
            "accounts": q("SELECT COUNT(*) FROM accounts"),
            "posts": q("SELECT COUNT(*) FROM posts"),
            "comments": q("SELECT COUNT(*) FROM comments"),
            "media": q("SELECT COUNT(*) FROM media"),
            "hashtags": q("SELECT COUNT(*) FROM hashtags"),
            "locations": q("SELECT COUNT(*) FROM locations"),
            "keywords": q("SELECT COUNT(*) FROM keywords"),
            "search_runs": q("SELECT COUNT(*) FROM search_runs"),
        }

    def get_posts(
        self,
        platform: Optional[str] = None,
        keyword: Optional[str] = None,
        author: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        sort: str = "latest",
    ) -> list[dict]:
        """Fetch posts.

        sort:
          'latest'  — newest by best-available time: the post's own timestamp
                      when we have one, otherwise when we scraped it. This is
                      the 'classify by latest data' ordering.
          'posted'  — strictly by the post's published timestamp (nulls last).
          'scraped' — by when we captured it (most recent run first).
        """
        conn = self.connect()
        sql = ["SELECT DISTINCT p.* FROM posts p"]
        params: list[Any] = []
        if keyword:
            sql.append(
                "JOIN result_links rl ON rl.entity_type='post' AND rl.entity_id=p.id "
                "JOIN keywords k ON k.id=rl.keyword_id"
            )
        where = []
        if platform:
            where.append("p.platform=?")
            params.append(platform)
        if author:
            where.append("p.author_username=?")
            params.append(author)
        if keyword:
            where.append("k.keyword=?")
            params.append(keyword)
        if where:
            sql.append("WHERE " + " AND ".join(where))

        if sort == "posted":
            order = "p.timestamp IS NULL, p.timestamp DESC"
        elif sort == "scraped":
            order = "p.created_at DESC"
        else:  # latest — coalesce posted time with scrape time
            order = "COALESCE(p.timestamp, p.scraped_at, p.created_at) DESC"
        sql.append(f"ORDER BY {order} LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        rows = conn.execute(" ".join(sql), params).fetchall()
        return [self._hydrate_post(dict(r)) for r in rows]

    def get_post(self, post_id: int) -> Optional[dict]:
        """One hydrated post by primary key, or None. Used by the API's
        /posts/<id> endpoint (avoids scanning all rows)."""
        conn = self.connect()
        row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return self._hydrate_post(dict(row)) if row else None

    def _hydrate_post(self, row: dict) -> dict:
        conn = self.connect()
        pid = row["id"]
        row["media"] = [dict(r) for r in conn.execute("SELECT * FROM media WHERE post_id=?", (pid,))]
        row["comments"] = [dict(r) for r in conn.execute("SELECT * FROM comments WHERE post_id=? ORDER BY timestamp", (pid,))]
        row["hashtags"] = [
            r["tag"]
            for r in conn.execute(
                "SELECT h.tag FROM hashtags h JOIN post_hashtags ph ON ph.hashtag_id=h.id WHERE ph.post_id=?",
                (pid,),
            )
        ]
        # Which keyword(s) surfaced this post — this is the classification.
        row["keywords"] = [
            r["keyword"]
            for r in conn.execute(
                "SELECT k.keyword FROM keywords k JOIN result_links rl ON rl.keyword_id=k.id "
                "WHERE rl.entity_type='post' AND rl.entity_id=?",
                (pid,),
            )
        ]
        # Location name, if any.
        row["location_name"] = None
        if row.get("location_id"):
            loc = conn.execute("SELECT name FROM locations WHERE id=?", (row["location_id"],)).fetchone()
            if loc:
                row["location_name"] = loc["name"]
        # Convenience media split for the UI.
        row["images"] = [m for m in row["media"] if m.get("type") == "image"]
        row["videos"] = [m for m in row["media"] if m.get("type") in ("video", "reel")]
        for jf in ("entities_people", "entities_orgs", "entities_locations", "topics"):
            if row.get(jf):
                row[jf] = _load_json_field(row[jf])
        return row

    def get_accounts(self, platform: Optional[str] = None, keyword: Optional[str] = None,
                     limit: int = 200) -> list[dict]:
        conn = self.connect()
        sql = ["SELECT DISTINCT a.* FROM accounts a"]
        params: list[Any] = []
        if keyword:
            sql.append("JOIN result_links rl ON rl.entity_type='account' AND rl.entity_id=a.id "
                       "JOIN keywords k ON k.id=rl.keyword_id")
        where = []
        if platform:
            where.append("a.platform=?"); params.append(platform)
        if keyword:
            where.append("k.keyword=?"); params.append(keyword)
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY a.follower_count IS NULL, a.follower_count DESC LIMIT ?")
        params.append(limit)
        return [dict(r) for r in conn.execute(" ".join(sql), params)]

    def get_hashtags(self, platform: Optional[str] = None, keyword: Optional[str] = None,
                     limit: int = 200) -> list[dict]:
        conn = self.connect()
        sql = ["SELECT DISTINCT h.* FROM hashtags h"]
        params: list[Any] = []
        if keyword:
            sql.append("JOIN result_links rl ON rl.entity_type='hashtag' AND rl.entity_id=h.id "
                       "JOIN keywords k ON k.id=rl.keyword_id")
        where = []
        if platform:
            where.append("h.platform=?"); params.append(platform)
        if keyword:
            where.append("k.keyword=?"); params.append(keyword)
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY h.post_count IS NULL, h.post_count DESC LIMIT ?")
        params.append(limit)
        return [dict(r) for r in conn.execute(" ".join(sql), params)]

    def get_places(self, keyword: Optional[str] = None, limit: int = 200) -> list[dict]:
        conn = self.connect()
        if keyword:
            rows = conn.execute(
                "SELECT DISTINCT l.* FROM locations l "
                "JOIN result_links rl ON rl.entity_type='place' AND rl.entity_id=l.id "
                "JOIN keywords k ON k.id=rl.keyword_id WHERE k.keyword=? LIMIT ?",
                (keyword, limit),
            )
        else:
            rows = conn.execute("SELECT * FROM locations LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def get_keywords(self) -> list[dict]:
        conn = self.connect()
        rows = conn.execute(
            """SELECT k.keyword,
                      SUM(CASE WHEN rl.entity_type='post' THEN 1 ELSE 0 END) AS posts,
                      SUM(CASE WHEN rl.entity_type='account' THEN 1 ELSE 0 END) AS accounts,
                      SUM(CASE WHEN rl.entity_type='hashtag' THEN 1 ELSE 0 END) AS hashtags,
                      SUM(CASE WHEN rl.entity_type='place' THEN 1 ELSE 0 END) AS places
               FROM keywords k
               LEFT JOIN result_links rl ON rl.keyword_id=k.id
               GROUP BY k.id ORDER BY k.keyword"""
        )
        out = []
        for r in rows:
            d = dict(r)
            for k in ("posts", "accounts", "hashtags", "places"):
                d[k] = _num(d.get(k)) or 0
            out.append(d)
        return out

    def get_keyword_detail(self, keyword: str, sort: str = "latest") -> dict:
        """Everything one keyword surfaced: its posts, accounts, hashtags,
        places — the full drill-down for a single search term."""
        return {
            "keyword": keyword,
            "posts": self.get_posts(keyword=keyword, sort=sort, limit=200),
            "accounts": self.get_accounts(keyword=keyword, limit=200),
            "hashtags": self.get_hashtags(keyword=keyword, limit=200),
            "places": self.get_places(keyword=keyword, limit=200),
        }

    # ── strategic keywords (AI analysis lens) ────────────────────────

    def get_strategic_keywords(self) -> list[str]:
        conn = self.connect()
        rows = conn.execute(
            "SELECT keyword FROM strategic_keywords ORDER BY keyword"
        )
        return [r["keyword"] for r in rows]

    def add_strategic_keyword(self, keyword: str) -> bool:
        keyword = (keyword or "").strip()
        if not keyword:
            return False
        conn = self.connect()
        cur = conn.execute(
            "INSERT OR IGNORE INTO strategic_keywords (keyword) VALUES (?)",
            (keyword,),
        )
        conn.commit()
        return cur.rowcount > 0

    def remove_strategic_keyword(self, keyword: str) -> None:
        conn = self.connect()
        conn.execute("DELETE FROM strategic_keywords WHERE keyword=?", (keyword,))
        conn.commit()

    # ── AI enrichment I/O ─────────────────────────────────────────────

    def count_posts_needing_analysis(self) -> int:
        """Posts with usable text but no strategic score yet.

        Keyed on strategic_relevance IS NULL (NOT analysis_model) — posts that
        already got scraper-time sentiment analysis still need a strategic
        score, so they must be picked up by the enrich pass."""
        conn = self.connect()
        return conn.execute(
            "SELECT COUNT(*) FROM posts "
            "WHERE strategic_relevance IS NULL "
            "AND text IS NOT NULL AND TRIM(text) <> ''"
        ).fetchone()[0]

    def get_posts_needing_analysis(
        self, limit: int = 50, exclude_ids: Optional[set[int]] = None
    ) -> list[dict]:
        """A batch of un-enriched posts (id, platform, author, text) for the
        LLM pass. `exclude_ids` lets the job skip posts the LLM couldn't score
        this run so the same batch isn't refetched into an infinite loop."""
        conn = self.connect()
        sql = (
            "SELECT id, platform, author_username, author_display_name, text "
            "FROM posts WHERE strategic_relevance IS NULL "
            "AND text IS NOT NULL AND TRIM(text) <> ''"
        )
        params: list[Any] = []
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            sql += f" AND id NOT IN ({placeholders})"
            params.extend(exclude_ids)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params)]

    def update_post_analysis(self, post_id: int, analysis: dict) -> None:
        """Write AI enrichment back to a post. Unlike _update_non_null this
        writes the strategic fields DIRECTLY so a genuine score of 0 persists
        (0 would be treated as 'missing' by the non-null updater)."""
        conn = self.connect()
        cols = {
            "sentiment": analysis.get("sentiment"),
            "sentiment_score": analysis.get("sentiment_score"),
            "entities_people": _json(analysis.get("entities_people")),
            "entities_orgs": _json(analysis.get("entities_orgs")),
            "entities_locations": _json(analysis.get("entities_locations")),
            "topics": _json(analysis.get("topics")),
            "language": analysis.get("language"),
            "analysis_model": analysis.get("model_used"),
            "strategic_relevance": analysis.get("strategic_relevance"),
            "strategic_rationale": analysis.get("strategic_rationale"),
        }
        assignments = ",".join(f"{k}=?" for k in cols)
        conn.execute(
            f"UPDATE posts SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [*cols.values(), post_id],
        )
        conn.commit()

    def reset_strategic_scores(self) -> None:
        """Clear all strategic scores so the next enrich re-runs from scratch —
        used when the strategic keyword set changes (rescore=1)."""
        conn = self.connect()
        conn.execute(
            "UPDATE posts SET strategic_relevance=NULL, strategic_rationale=NULL"
        )
        conn.commit()

    def get_strategic_scores(self) -> dict[int, dict]:
        """Map post_id -> {relevance, rationale} for every enriched post.
        Used by the dashboard to overlay AI scores onto NLP-scored posts."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT id, strategic_relevance, strategic_rationale FROM posts "
            "WHERE strategic_relevance IS NOT NULL"
        )
        return {
            r["id"]: {
                "relevance": r["strategic_relevance"],
                "rationale": r["strategic_rationale"],
            }
            for r in rows
        }

    # ── strategic trends (read-only aggregation) ──────────────────────

    def get_strategic_leaderboard(self, limit: int = 15) -> list[dict]:
        """Accounts ranked by strategic footprint — avg & peak relevance over
        their enriched posts, plus post count. Groups by author_username so it
        works even for posts with no linked account row."""
        conn = self.connect()
        rows = conn.execute(
            """SELECT
                   COALESCE(author_username, author_display_name, '—') AS author,
                   MAX(platform) AS platform,
                   COUNT(*) AS posts,
                   ROUND(AVG(strategic_relevance)) AS avg_rel,
                   MAX(strategic_relevance) AS max_rel,
                   SUM(strategic_relevance) AS total_rel
               FROM posts
               WHERE strategic_relevance IS NOT NULL
                 AND COALESCE(author_username, author_display_name) IS NOT NULL
               GROUP BY author
               ORDER BY avg_rel DESC, posts DESC
               LIMIT ?""",
            (limit,),
        )
        out = []
        for r in rows:
            d = dict(r)
            for k in ("posts", "avg_rel", "max_rel", "total_rel"):
                d[k] = _num(d.get(k))
            out.append(d)
        return out

    def get_strategic_timeline(self, min_relevance: int = 1) -> list[dict]:
        """Strategic activity per day (posts with a real timestamp only).
        Returns day, post count, and summed relevance for a time-series chart.
        NULL-timestamp posts (a large share) are excluded by design — the UI
        notes the coverage."""
        conn = self.connect()
        rows = conn.execute(
            """SELECT substr(timestamp,1,10) AS day,
                      COUNT(*) AS posts,
                      SUM(strategic_relevance) AS total_rel,
                      ROUND(AVG(strategic_relevance)) AS avg_rel
               FROM posts
               WHERE strategic_relevance IS NOT NULL
                 AND strategic_relevance >= ?
                 AND timestamp IS NOT NULL AND TRIM(timestamp) <> ''
               GROUP BY day
               ORDER BY day""",
            (min_relevance,),
        )
        out = []
        for r in rows:
            d = dict(r)
            for k in ("posts", "total_rel", "avg_rel"):
                d[k] = _num(d.get(k))
            out.append(d)
        return out

    def get_strategic_sentiment_breakdown(self) -> list[dict]:
        """Sentiment distribution across strategically-relevant posts
        (relevance >= 46, i.e. clearly on-topic)."""
        conn = self.connect()
        rows = conn.execute(
            """SELECT COALESCE(sentiment,'unknown') AS sentiment, COUNT(*) AS n
               FROM posts
               WHERE strategic_relevance IS NOT NULL AND strategic_relevance >= 46
               GROUP BY sentiment
               ORDER BY n DESC"""
        )
        out = []
        for r in rows:
            d = dict(r)
            d["n"] = _num(d.get("n"))
            out.append(d)
        return out

    def get_top_strategic_posts(self, limit: int = 12) -> list[dict]:
        """The highest-scoring strategic posts, hydrated for the feed."""
        conn = self.connect()
        rows = conn.execute(
            """SELECT * FROM posts
               WHERE strategic_relevance IS NOT NULL
               ORDER BY strategic_relevance DESC,
                        COALESCE(timestamp, scraped_at, created_at) DESC
               LIMIT ?""",
            (limit,),
        )
        return [self._hydrate_post(dict(r)) for r in rows]

    def get_strategic_summary(self) -> dict:
        """Headline numbers for the strategic page hero."""
        conn = self.connect()
        row = conn.execute(
            """SELECT
                   COUNT(*) AS enriched,
                   SUM(CASE WHEN strategic_relevance >= 71 THEN 1 ELSE 0 END) AS high,
                   SUM(CASE WHEN strategic_relevance >= 46 THEN 1 ELSE 0 END) AS relevant,
                   ROUND(AVG(strategic_relevance)) AS avg_rel
               FROM posts WHERE strategic_relevance IS NOT NULL"""
        ).fetchone()
        if not row:
            return {}
        d = dict(row)
        # MySQL returns SUM()/ROUND() as Decimal/str; normalize to plain ints so
        # the JSON API emits numbers, matching SQLite's behaviour.
        for k in ("enriched", "high", "relevant", "avg_rel"):
            d[k] = _num(d.get(k))
        return d

    # ── lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "PostDatabase":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── module-level sql helpers ─────────────────────────────────────────


def _insert(conn: sqlite3.Connection, table: str, cols: dict) -> int:
    keys = list(cols.keys())
    placeholders = ",".join("?" for _ in keys)
    cur = conn.execute(
        f"INSERT INTO {table} ({','.join(keys)}) VALUES ({placeholders})",
        [cols[k] for k in keys],
    )
    return cur.lastrowid


def _update_non_null(conn: sqlite3.Connection, table: str, row_id: int, cols: dict) -> None:
    """Update only the columns whose new value is not None — a re-scrape that
    missed a field must never wipe a previously-captured value."""
    updates = {k: v for k, v in cols.items() if v is not None}
    if not updates:
        return
    assignments = ",".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id=?",
        [*updates.values(), row_id],
    )


def _split_sql_statements(ddl: str) -> list[str]:
    """Split a multi-statement SQL script into individual statements for a
    driver that executes one at a time. Strips line comments and blank
    statements. The schema file has no ';' inside string literals, so a simple
    split on ';' is safe here."""
    lines = []
    for line in ddl.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        lines.append(line)
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


_ISO_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$"
)


def _normalize_dt_param(v):
    """Convert an ISO-8601 datetime string (T-separated, possibly with tz) to
    MySQL's `YYYY-MM-DD HH:MM:SS` so it lands cleanly in a DATETIME column.
    Non-datetime values pass through untouched. Applied only on the MySQL
    binding path — SQLite keeps ISO text as-is."""
    if isinstance(v, str) and _ISO_DT_RE.match(v):
        s = v.replace("T", " ")
        # Drop timezone suffix and fractional seconds — DATETIME has neither.
        s = re.sub(r"(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$", "", s).strip()
        return s
    return v


def _load_json_field(value):
    """Decode a JSON column into a Python list. Handles both backends: SQLite
    (and older PyMySQL) return the raw JSON string; some MySQL drivers return an
    already-parsed list/dict. Returns [] on anything unusable."""
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _bool(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return 1 if value else 0


def _num(value):
    """Coerce a DB aggregate to a plain int (or None). MySQL returns
    SUM()/ROUND()/AVG() as Decimal or str; SQLite returns int/float. This
    normalizes both so the JSON API always emits numbers, not strings."""
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return value
