"""One-time migration: copy the local SQLite DB into MySQL.

Moves every row from meta_osint.db into a MySQL database that already has the
schema (run schema_mysql.sql first). As it copies the `media` table it strips
`local_path` / `local_thumbnail` down to BARE FILENAMES, so the data is
portable: copy the code folder + the media/ folder to another PC, point
MEDIA_DIR at the copied folder, and every image/video still resolves.

Credentials come from the environment / a .env file — never hard-coded, never
on the command line:

    MYSQL_HOST      (default 127.0.0.1)
    MYSQL_PORT      (default 3306)
    MYSQL_USER
    MYSQL_PASSWORD
    MYSQL_DB
    META_OSINT_DB   (optional) path to the source SQLite file;
                    defaults to meta_osint/data/meta_osint.db

Run:
    python -m meta_osint.database.migrate_sqlite_to_mysql            # migrate
    python -m meta_osint.database.migrate_sqlite_to_mysql --dry-run  # counts only
    python -m meta_osint.database.migrate_sqlite_to_mysql --truncate # wipe MySQL tables first

Safe to re-run: rows are inserted with INSERT IGNORE against the same natural
keys the app uses, so a second run won't duplicate. Read-only on the SQLite
side. Preserves primary-key ids so result_links / FKs stay consistent.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Make stdout UTF-8 so progress output never crashes on a legacy Windows
# console (cp1252). Harmless elsewhere.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from .. import config

# Tables in FK-safe insert order (parents before children).
_TABLE_ORDER = [
    "accounts",
    "hashtags",
    "locations",
    "keywords",
    "strategic_keywords",
    "posts",
    "media",
    "comments",
    "post_hashtags",
    "search_runs",
    "result_links",
]

# Columns holding a filesystem path we must reduce to a bare filename so the
# data is portable across machines.
_PATH_COLUMNS = {
    "media": ("local_path", "local_thumbnail"),
    "accounts": ("profile_picture_local",),
}


def _basename(value):
    """Reduce an absolute/any path to just its filename (handles both \\ and /)."""
    if not value:
        return value
    # Normalise Windows and POSIX separators, then take the last segment.
    return value.replace("\\", "/").rstrip("/").split("/")[-1]


def _connect_mysql():
    dsn = config.mysql_dsn() if hasattr(config, "mysql_dsn") else {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", ""),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": os.getenv("MYSQL_DB", ""),
    }
    missing = [k for k in ("user", "password", "database") if not dsn.get(k)]
    if missing:
        sys.exit(f"Missing MySQL env var(s): {', '.join('MYSQL_' + m.upper() for m in missing)}")
    try:
        import pymysql
        return pymysql.connect(charset="utf8mb4", autocommit=False, **dsn)
    except ImportError:
        pass
    try:
        import mysql.connector
        return mysql.connector.connect(charset="utf8mb4", **dsn)
    except ImportError:
        sys.exit("No MySQL driver. Run:  pip install pymysql")


def _sqlite_tables(scur) -> set[str]:
    return {r[0] for r in scur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def migrate(dry_run: bool = False, truncate: bool = False) -> None:
    src_path = Path(os.getenv("META_OSINT_DB", str(config.DB_PATH)))
    if not src_path.exists():
        sys.exit(f"Source SQLite DB not found: {src_path}")
    print(f"Source SQLite : {src_path}")
    print(f"Target MySQL  : {os.getenv('MYSQL_HOST','127.0.0.1')}/{os.getenv('MYSQL_DB')}")
    print(f"Mode          : {'DRY RUN' if dry_run else 'MIGRATE'}"
          f"{' + TRUNCATE' if truncate else ''}\n")

    sconn = sqlite3.connect(str(src_path))
    sconn.row_factory = sqlite3.Row
    scur = sconn.cursor()
    present = _sqlite_tables(scur)

    if dry_run:
        for t in _TABLE_ORDER:
            if t in present:
                n = scur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  {t:<22} {n} rows")
        sconn.close()
        print("\n(dry run — nothing written)")
        return

    mconn = _connect_mysql()
    mcur = mconn.cursor()
    mcur.execute("SET FOREIGN_KEY_CHECKS = 0")

    if truncate:
        for t in reversed(_TABLE_ORDER):
            mcur.execute(f"TRUNCATE TABLE `{t}`")
        print("Truncated all target tables.\n")

    total = 0
    for table in _TABLE_ORDER:
        if table not in present:
            continue
        rows = scur.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"  {table:<22} 0")
            continue
        cols = rows[0].keys()
        path_cols = _PATH_COLUMNS.get(table, ())
        placeholders = ",".join(["%s"] * len(cols))
        collist = ",".join(f"`{c}`" for c in cols)
        sql = f"INSERT IGNORE INTO `{table}` ({collist}) VALUES ({placeholders})"

        batch = []
        for r in rows:
            vals = []
            for c in cols:
                v = r[c]
                if c in path_cols:
                    v = _basename(v)
                vals.append(v)
            batch.append(tuple(vals))

        mcur.executemany(sql, batch)
        total += len(batch)
        note = f"  (paths -> filenames: {', '.join(path_cols)})" if path_cols else ""
        print(f"  {table:<22} {len(batch)}{note}")

    mcur.execute("SET FOREIGN_KEY_CHECKS = 1")
    mconn.commit()
    sconn.close()
    mconn.close()
    print(f"\nDone — {total} rows migrated. Media paths stored as filenames.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate meta_osint SQLite → MySQL")
    ap.add_argument("--dry-run", action="store_true", help="show row counts, write nothing")
    ap.add_argument("--truncate", action="store_true", help="empty target tables before insert")
    args = ap.parse_args()
    migrate(dry_run=args.dry_run, truncate=args.truncate)


if __name__ == "__main__":
    main()
