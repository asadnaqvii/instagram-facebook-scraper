"""End-to-end verification of the MySQL backend — run this on a machine that
can reach the MySQL server (your PC / the LAN box), since the connection is
made under YOUR identity from YOUR machine.

It proves the whole path works:
  1. connects to MySQL (creds from env / .env)
  2. creates the schema (idempotent) if missing
  3. runs the SQLite→MySQL migration (unless --skip-migrate)
  4. exercises every read method PostDatabase exposes
  5. prints MySQL vs SQLite row counts side by side so you can see parity

Setup — put these in a .env file next to where you run it (or export them):

    META_OSINT_DB_BACKEND=mysql
    MYSQL_HOST=192.168.1.100
    MYSQL_PORT=3306
    MYSQL_USER=your_user
    MYSQL_PASSWORD=...
    MYSQL_DB=your_db

Run:
    python -m meta_osint.database.verify_mysql                 # full run
    python -m meta_osint.database.verify_mysql --skip-migrate  # if already migrated

Read-mostly: it only writes via the standard migration (INSERT IGNORE,
re-runnable). Nothing is deleted.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from .. import config
from .db import PostDatabase


def _sqlite_counts() -> dict:
    src = str(config.DB_PATH)
    if not os.path.exists(src):
        return {}
    c = sqlite3.connect(src)
    out = {}
    for t in ("accounts", "posts", "media", "comments", "hashtags",
              "keywords", "strategic_keywords", "result_links"):
        try:
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = "-"
    c.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify the MySQL backend end to end")
    ap.add_argument("--skip-migrate", action="store_true",
                    help="don't run the data migration (assume already loaded)")
    args = ap.parse_args()

    if config.DB_BACKEND != "mysql":
        sys.exit("META_OSINT_DB_BACKEND is not 'mysql'. Set it (and MYSQL_*) "
                 "in your .env, then re-run.")

    print(f"Target: MySQL @ {config.MYSQL_HOST}:{config.MYSQL_PORT}/{config.MYSQL_DB}\n")

    # 1 + 2: connect + create schema (PostDatabase.connect does both).
    print("[1/4] Connecting + ensuring schema…")
    db = PostDatabase()
    db.connect()
    print("      OK — connected and schema present.\n")

    # 3: migrate data.
    if not args.skip_migrate:
        print("[2/4] Migrating data from SQLite…")
        from .migrate_sqlite_to_mysql import migrate
        migrate(dry_run=False, truncate=False)
        print()
    else:
        print("[2/4] Skipping migration (--skip-migrate).\n")

    # 4: exercise every read method.
    print("[3/4] Exercising read methods…")
    stats = db.get_stats()
    print("      get_stats:", stats)
    print("      get_keywords:", len(db.get_keywords()), "keywords")
    print("      get_posts(limit=5):", len(db.get_posts(limit=5)), "posts")
    sample = db.get_posts(limit=1)
    if sample:
        p = sample[0]
        print("      hydrate check — post", p["id"],
              "| media:", len(p.get("media", [])),
              "| hashtags:", len(p.get("hashtags", [])),
              "| keywords:", len(p.get("keywords", [])))
    print("      get_accounts:", len(db.get_accounts(limit=5)))
    print("      get_hashtags:", len(db.get_hashtags(limit=5)))
    print("      strategic_keywords:", db.get_strategic_keywords())
    print("      needing analysis:", db.count_posts_needing_analysis())
    print("      strategic summary:", db.get_strategic_summary())
    print("      strategic scores:", len(db.get_strategic_scores()), "enriched")
    print("      leaderboard rows:", len(db.get_strategic_leaderboard()))
    print("      timeline points:", len(db.get_strategic_timeline()))
    print("      sentiment rows:", len(db.get_strategic_sentiment_breakdown()))
    print("      top strategic posts:", len(db.get_top_strategic_posts(5)))
    print()

    # 5: parity table.
    print("[4/4] Row-count parity (SQLite source vs MySQL target):")
    sq = _sqlite_counts()
    my = {
        "accounts": stats["accounts"], "posts": stats["posts"],
        "media": stats["media"], "comments": stats["comments"],
        "hashtags": stats["hashtags"], "keywords": stats["keywords"],
    }
    print(f"      {'table':<20}{'sqlite':>10}{'mysql':>10}")
    for t in ("accounts", "posts", "media", "comments", "hashtags", "keywords"):
        s = sq.get(t, "-")
        m = my.get(t, "-")
        flag = "" if s == m else "   <-- differ"
        print(f"      {t:<20}{str(s):>10}{str(m):>10}{flag}")

    db.close()
    print("\nDone. If parity matches and hydrate check is non-zero, the MySQL "
          "backend is live — point the app at it (same .env) and run:\n"
          "    python -m meta_osint.main serve --port 5002")


if __name__ == "__main__":
    main()
