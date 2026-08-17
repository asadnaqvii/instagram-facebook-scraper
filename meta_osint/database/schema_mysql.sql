-- ============================================================================
--  meta_osint — MySQL schema
--  Port of the SQLite schema (meta_osint/database/db.py) for hosting the
--  scraper + Strategic Intelligence dashboard inside a third-party Python web
--  app running locally on the user's own machine.
--
--  Faithful 1:1 port of the live SQLite tables, plus:
--    * strategic_relevance / strategic_rationale on posts and the
--      strategic_keywords table (the AI enrichment feature)
--    * a `jobs` table that replaces the in-memory JOBS dict + Python threads
--      used by the local Flask app, so scrape/enrich progress survives across
--      requests and worker restarts.
--
--  Conventions vs SQLite:
--    * utf8mb4 everywhere — Instagram/Facebook captions are full of emoji;
--      utf8mb3 silently truncates them.
--    * Columns that participate in a UNIQUE key or index are VARCHAR (MySQL
--      cannot index an unbounded TEXT without a prefix). Free-form bodies
--      (bio, text, rationale, raw_meta) stay TEXT/LONGTEXT.
--    * SQLite JSON-in-TEXT columns (entities_*, topics) become native JSON.
--    * SQLite INTEGER booleans become TINYINT.
--    * AUTOINCREMENT -> AUTO_INCREMENT; DEFAULT CURRENT_TIMESTAMP -> DATETIME.
--    * INSERT OR IGNORE (app code) -> INSERT IGNORE.
--
--  Apply:  mysql -u <user> -p <db_name> < schema_mysql.sql
--  Requires MySQL 5.7+ / MariaDB 10.2+ (native JSON, utf8mb4).
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ── accounts ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
    id                     BIGINT AUTO_INCREMENT PRIMARY KEY,
    platform               VARCHAR(32)  NOT NULL,
    username               VARCHAR(255) NOT NULL,
    display_name           VARCHAR(512),
    profile_url            VARCHAR(1024),
    bio                    TEXT,
    profile_picture_url    VARCHAR(2048),
    profile_picture_local  VARCHAR(1024),
    is_verified            TINYINT,
    is_private             TINYINT,
    category               VARCHAR(255),
    external_url           VARCHAR(2048),
    follower_count         BIGINT,
    following_count        BIGINT,
    post_count             BIGINT,
    likes_count            BIGINT,
    first_seen             DATETIME DEFAULT CURRENT_TIMESTAMP,
    scraped_at             DATETIME NULL,
    UNIQUE KEY uq_accounts_platform_username (platform, username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── hashtags ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hashtags (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    platform    VARCHAR(32)  NOT NULL,
    tag         VARCHAR(255) NOT NULL,
    url         VARCHAR(1024),
    post_count  BIGINT,
    first_seen  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_hashtags_platform_tag (platform, tag)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── locations ───────────────────────────────────────────────────────────────
--  SQLite's UNIQUE(platform, name, ext_id) treats NULL ext_id as distinct.
--  MySQL does too (multiple NULLs allowed in a UNIQUE key), so behaviour
--  matches. ext_id is bounded so it can join the unique key.
CREATE TABLE IF NOT EXISTS locations (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    platform   VARCHAR(32)  NOT NULL,
    name       VARCHAR(400) NOT NULL,  -- UNIQUE(platform,name,ext_id): (32+400+255)*4 = 2748B < 3072
    ext_id     VARCHAR(255),
    url        VARCHAR(1024),
    latitude   DOUBLE,
    longitude  DOUBLE,
    UNIQUE KEY uq_locations (platform, name, ext_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── posts ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS posts (
    id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
    platform              VARCHAR(32)  NOT NULL,
    post_url              VARCHAR(700),          -- composite UNIQUE(platform,post_url) must fit
                                                 -- InnoDB's 3072-byte limit: (32+700)*4 = 2928B
    post_id               VARCHAR(255),
    kind                  VARCHAR(32),
    account_id            BIGINT,
    author_username       VARCHAR(255),
    author_display_name   VARCHAR(512),
    author_is_verified    TINYINT,
    text                  TEXT,
    location_id           BIGINT,
    timestamp             DATETIME NULL,
    likes                 BIGINT,
    comments_count        BIGINT,
    shares                BIGINT,
    views                 BIGINT,

    -- LLM analysis (scraper-time or on-demand enrichment)
    sentiment             VARCHAR(32),
    sentiment_score       DOUBLE,
    entities_people       JSON,
    entities_orgs         JSON,
    entities_locations    JSON,
    topics                JSON,
    language              VARCHAR(16),
    analysis_model        VARCHAR(128),

    -- Strategic Intelligence (on-demand AI enrichment)
    strategic_relevance   INT,                   -- 0-100 semantic score, NULL = not yet scored
    strategic_rationale   TEXT,

    raw_meta              JSON,
    scraped_at            DATETIME NULL,
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_posts_platform_url (platform, post_url),
    KEY idx_posts_platform  (platform),
    KEY idx_posts_author    (author_username),
    KEY idx_posts_timestamp (timestamp),
    KEY idx_posts_strategic (strategic_relevance),
    CONSTRAINT fk_posts_account  FOREIGN KEY (account_id)  REFERENCES accounts(id)  ON DELETE SET NULL,
    CONSTRAINT fk_posts_location FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── media ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS media (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    post_id          BIGINT NOT NULL,
    url              VARCHAR(700),          -- UNIQUE(post_id,url): 8 + 700*4 = 2808B < 3072
    type             VARCHAR(32),
    alt_text         TEXT,
    thumbnail_url    VARCHAR(2048),
    width            INT,
    height           INT,
    duration_s       DOUBLE,
    local_path       VARCHAR(1024),
    local_thumbnail  VARCHAR(1024),
    UNIQUE KEY uq_media_post_url (post_id, url),
    KEY idx_media_post (post_id),
    CONSTRAINT fk_media_post FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── comments ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS comments (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    post_id             BIGINT NOT NULL,
    platform            VARCHAR(32),
    comment_id          VARCHAR(255),
    author_username     VARCHAR(255),
    author_display_name VARCHAR(512),
    author_avatar_url   VARCHAR(2048),
    text                TEXT,
    timestamp           DATETIME NULL,
    likes               BIGINT,
    reply_count         BIGINT,
    is_reply            TINYINT DEFAULT 0,
    parent_comment_id   VARCHAR(255),
    scraped_at          DATETIME NULL,
    UNIQUE KEY uq_comments_post_cid (post_id, comment_id),
    KEY idx_comments_post (post_id),
    CONSTRAINT fk_comments_post FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── post_hashtags (junction) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS post_hashtags (
    post_id     BIGINT NOT NULL,
    hashtag_id  BIGINT NOT NULL,
    PRIMARY KEY (post_id, hashtag_id),
    CONSTRAINT fk_ph_post    FOREIGN KEY (post_id)    REFERENCES posts(id)    ON DELETE CASCADE,
    CONSTRAINT fk_ph_hashtag FOREIGN KEY (hashtag_id) REFERENCES hashtags(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── keywords (search terms that drove scraping) ─────────────────────────────
CREATE TABLE IF NOT EXISTS keywords (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    keyword     VARCHAR(255) NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_keywords_keyword (keyword)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── strategic_keywords (the AI analysis lens) ───────────────────────────────
--  Distinct from search `keywords`: these define what "strategically relevant"
--  means when the LLM scores each post.
CREATE TABLE IF NOT EXISTS strategic_keywords (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    keyword     VARCHAR(255) NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_strategic_keywords_keyword (keyword)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── search_runs (per-keyword scrape bookkeeping) ────────────────────────────
CREATE TABLE IF NOT EXISTS search_runs (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    keyword_id      BIGINT,
    platform        VARCHAR(32),
    started_at      DATETIME NULL,
    finished_at     DATETIME NULL,
    posts_found     INT DEFAULT 0,
    accounts_found  INT DEFAULT 0,
    hashtags_found  INT DEFAULT 0,
    places_found    INT DEFAULT 0,
    error           TEXT,
    KEY idx_search_runs_keyword (keyword_id),
    CONSTRAINT fk_search_runs_keyword FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── result_links (which keyword surfaced which entity) ──────────────────────
CREATE TABLE IF NOT EXISTS result_links (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    keyword_id   BIGINT NOT NULL,
    entity_type  VARCHAR(16) NOT NULL,   -- 'post' | 'account' | 'hashtag' | 'place'
    entity_id    BIGINT NOT NULL,
    run_id       BIGINT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_result_links (keyword_id, entity_type, entity_id),
    KEY idx_result_links_kw (keyword_id),
    KEY idx_result_links_entity (entity_type, entity_id),
    CONSTRAINT fk_result_links_keyword FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── jobs (replaces the in-memory JOBS dict) ─────────────────────────────────
--  The local Flask app tracks scrape/enrich jobs in a Python dict + threads.
--  In a multi-worker web app that state must be shared, so it lives here: the
--  web layer inserts a queued row, a background worker updates status/progress,
--  and the UI polls GET /api/jobs/:id.  `log` is a JSON array of progress lines;
--  `result` is a JSON blob (e.g. {"enriched": 350, "skipped": 0}).
CREATE TABLE IF NOT EXISTS jobs (
    id           VARCHAR(64) PRIMARY KEY,          -- app-generated (uuid4 hex)
    kind         VARCHAR(32) NOT NULL,             -- 'scrape' | 'enrich'
    status       VARCHAR(16) NOT NULL DEFAULT 'queued', -- queued|running|done|stopped|error
    cancel       TINYINT NOT NULL DEFAULT 0,       -- cooperative stop flag
    params       JSON,                             -- keywords, platforms, rescore, etc.
    log          JSON,                             -- array of progress strings
    result       JSON,
    error        TEXT,
    started_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_jobs_status (status),
    KEY idx_jobs_kind_status (kind, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================================
--  Notes for the app / DB layer (db.py rewrite):
--
--  1. Parameter style: swap sqlite3 `?` placeholders for PyMySQL/mysql-connector
--     `%s`. Keep every query otherwise; the SQL is standard.
--
--  2. JSON columns: the app currently json.dumps() into TEXT and json.loads()
--     on read. With native JSON you can either keep doing that (MySQL accepts a
--     JSON string on insert and returns text on select — zero code change) OR
--     let the driver adapt dicts. Simplest port: keep the existing
--     _json()/json.loads() helpers unchanged.
--
--  3. Booleans: _bool() already yields 1/0 — lands correctly in TINYINT.
--
--  4. Guarded migration: the SQLite path uses PRAGMA table_info(posts) to add
--     strategic_* columns idempotently. On MySQL this DDL already includes them,
--     so for an EXISTING db instead run, guarded via information_schema:
--        SELECT COUNT(*) FROM information_schema.columns
--          WHERE table_schema = DATABASE() AND table_name='posts'
--            AND column_name='strategic_relevance';
--     and ALTER TABLE only when 0.
--
--  5. INSERT OR IGNORE -> INSERT IGNORE (get_or_create_keyword,
--     add_strategic_keyword, _link_result, post_hashtags insert).
--
--  6. Connection: replace the single reused sqlite3 connection with a pooled
--     engine (SQLAlchemy create_engine, or a mysql-connector pool). Drop the
--     PRAGMA foreign_keys / journal_mode lines (InnoDB enforces FKs; no WAL).
--
--  7. Aggregations used by the strategic page (leaderboard, timeline, sentiment,
--     summary) are portable as-is; only substr()->SUBSTRING() and confirming
--     ROUND()/AVG() behaviour (identical) is needed.
-- ============================================================================
