"""Inspect a MySQL database and print its structure + a sample of one table.

Credentials are read from the environment or a local `.env` file — never
hard-coded and never passed on the command line (where they'd land in shell
history). Populate these:

    MYSQL_HOST         e.g. 192.168.3.161   (default 127.0.0.1)
    MYSQL_PORT         default 3306
    MYSQL_USER
    MYSQL_PASSWORD
    MYSQL_DB           database name to inspect
    MYSQL_FOCUS_TABLE  table to dump in detail (default: meta_scrapper)

Option A — a `.env` file next to where you run it:

    MYSQL_HOST=192.168.3.161
    MYSQL_PORT=3306
    MYSQL_USER=AIUser01
    MYSQL_PASSWORD=CHANGEME
    MYSQL_DB=your_db
    MYSQL_FOCUS_TABLE=meta_scrapper

Then:  python -m meta_osint.database.inspect_db

Option B — export in your shell, then run the same command.

Read-only: it runs SHOW TABLES / DESCRIBE / COUNT / SELECT ... LIMIT 5 only.
It never writes. Paste its output back for interpretation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency). Looks in CWD then this file's dir.
    Does not overwrite vars already set in the real environment."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)
        break


def _connect():
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = int(os.getenv("MYSQL_PORT", "3306"))
    user = os.getenv("MYSQL_USER")
    password = os.getenv("MYSQL_PASSWORD")
    db = os.getenv("MYSQL_DB")

    missing = [k for k, v in {"MYSQL_USER": user, "MYSQL_PASSWORD": password,
                              "MYSQL_DB": db}.items() if not v]
    if missing:
        sys.exit(f"Missing env var(s): {', '.join(missing)}.\n"
                 f"Set them in a .env file or your shell — see the docstring.")

    # Prefer PyMySQL; fall back to mysql-connector — whichever is installed.
    try:
        import pymysql
        from pymysql.cursors import DictCursor
        return (pymysql.connect(host=host, port=port, user=user, password=password,
                                database=db, cursorclass=DictCursor,
                                connect_timeout=8),
                "pymysql")
    except ImportError:
        pass
    try:
        import mysql.connector
        return (mysql.connector.connect(host=host, port=port, user=user,
                                        password=password, database=db,
                                        connection_timeout=8),
                "mysql.connector")
    except ImportError:
        sys.exit("No MySQL driver installed. Run:  pip install pymysql")


def _rows(cur) -> list[dict]:
    """Normalise DictCursor vs tuple cursor to a list of dicts."""
    data = cur.fetchall()
    if data and isinstance(data[0], dict):
        return list(data)
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in data]


def main() -> None:
    _load_dotenv()
    conn, driver = _connect()
    host, db = os.getenv("MYSQL_HOST", "127.0.0.1"), os.getenv("MYSQL_DB")
    focus = os.getenv("MYSQL_FOCUS_TABLE", "meta_scrapper")
    print(f"Connected via {driver} to {host}/{db}\n")
    cur = conn.cursor()

    cur.execute("SHOW TABLES")
    tables = [next(iter(r.values())) for r in _rows(cur)]
    print(f"Tables ({len(tables)}): {', '.join(tables) or '(none)'}\n")

    print("Row counts:")
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{t}`")
            n = next(iter(_rows(cur)[0].values()))
        except Exception as e:  # noqa: BLE001
            n = f"? ({e})"
        print(f"  {t:<30} {n}")
    print()

    if focus in tables:
        print(f"=== structure of `{focus}` ===")
        cur.execute(f"DESCRIBE `{focus}`")
        for col in _rows(cur):
            null = "NULL" if col.get("Null") == "YES" else "NOT NULL"
            print(f"  {col.get('Field',''):<28} {col.get('Type',''):<22} "
                  f"{null:<9} {col.get('Key','')}")
        print()

        print(f"=== up to 5 sample rows from `{focus}` ===")
        cur.execute(f"SELECT * FROM `{focus}` LIMIT 5")
        sample = _rows(cur)
        for i, row in enumerate(sample, 1):
            print(f"--- row {i} ---")
            for k, v in row.items():
                s = str(v)
                print(f"  {k}: {s[:200]}{'…' if len(s) > 200 else ''}")
        if not sample:
            print("  (table is empty)")
    else:
        print(f"(No `{focus}` table found. Set MYSQL_FOCUS_TABLE to one of the "
              f"tables listed above.)")

    conn.close()


if __name__ == "__main__":
    main()
