# meta_osint — Migration & Integration Guide

For a team taking this backend and standing it up on **their own machine** to
integrate its APIs into a new platform. This is a full migration: you run
everything locally on your infrastructure; nothing calls back to the origin
machine.

What you're deploying is a **local backend** that exposes a JSON API at
`http://localhost:5002/api/v1`. Your new platform (any language) calls that API.
Everything the backend needs — browser, LLM, database — runs on the same box.

---

## 1. What you receive (three things)

| Item | What it is | How it travels |
|---|---|---|
| **The code** | the `meta_osint` package + scripts + requirements | git clone / zip of the repo |
| **The database dump** | `meta_osint/database/meta_osint_mysql_dump.sql` — all scraped data (358 posts, accounts, comments, strategic scores) | in the repo |
| **The media files** | `meta_osint/data/media/` — ~1.4 GB of images/videos | **transferred separately** (too big for git; see step 5) |

> **Not transferred (by design):** `cookies_*.txt` (login-session secrets, tied to
> the origin machine) and any `.env` (contains passwords). You create fresh ones.

---

## 2. Prerequisites (install on the target machine)

| Dependency | Why | Install |
|---|---|---|
| **Python 3.10+** | runs the backend | python.org |
| **MySQL 8.0+** (or MariaDB 10.4+) | the database | dev.mysql.com |
| **Google Chrome** | scraping runs through a real logged-in Chrome over CDP | google.com/chrome |
| **Ollama** + a model | local LLM for AI enrichment | ollama.com, then `ollama pull llama3.2` |

Ollama and Chrome are only needed for **live scraping/enrichment**. If the new
platform will only *read* the already-scraped data over the API, you can skip
Chrome and Ollama and just run the DB + API.

---

## 3. Set up the code

```bash
# 1. Get the code onto the target machine (git or a copied folder)
cd meta-scraper-relevancy

# 2. Create a virtualenv and install dependencies
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/mac: source .venv/bin/activate
pip install -r requirements.txt

# 3. Install the Playwright browser binaries (only if scraping)
python -m playwright install chromium
```

---

## 4. Set up the database (MySQL)

```bash
# 1. Create an empty database + a user for the app
mysql -u root -p -e "CREATE DATABASE meta_osint CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -u root -p -e "CREATE USER 'meta_app'@'localhost' IDENTIFIED BY 'CHOOSE_A_PASSWORD'; \
                     GRANT ALL PRIVILEGES ON meta_osint.* TO 'meta_app'@'localhost'; FLUSH PRIVILEGES;"

# 2. Load the data dump into it
mysql -u meta_app -p meta_osint < meta_osint/database/meta_osint_mysql_dump.sql
```

That single dump creates every table **and** loads all the data. Verify:

```bash
mysql -u meta_app -p meta_osint -e "SELECT COUNT(*) AS posts FROM posts;"   # expect 358
```

> Starting fresh with no historical data instead? Skip the dump load and just
> create the schema: `mysql -u meta_app -p meta_osint < meta_osint/database/schema_mysql.sql`

---

## 5. Bring the media over

The DB stores media as **bare filenames** (e.g. `47ababc43aac.jpg`), and the API
serves them from a configurable folder. So you just need the files on disk.

1. Copy the origin's `meta_osint/data/media/` folder (~1.4 GB) to the target —
   USB, `scp`, rsync, cloud drive, whatever. Put it at the same relative path:
   `meta_osint/data/media/` inside the code folder.
2. That's it — because paths are filenames, no rewriting is needed. If you put
   media somewhere else, point `META_OSINT_DATA_DIR` at its parent (see step 6).

> Read-only-data integrations that don't need images can skip this; posts still
> return, image URLs just won't resolve.

---

## 6. Configure (`.env`)

Copy `.env.example` to `.env` and fill it in:

```ini
# Use the MySQL you just loaded
META_OSINT_DB_BACKEND=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=meta_app
MYSQL_PASSWORD=CHOOSE_A_PASSWORD
MYSQL_DB=meta_osint

# Local LLM (only for enrichment)
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:latest
LLM_ENABLED=true

# Chrome CDP ports (only for scraping)
CDP_PORT_FACEBOOK=9222
CDP_PORT_INSTAGRAM=9223

# ── API access control (recommended for integration) ──
# Require a key so only your platform can call the API:
META_OSINT_API_KEY=generate_a_long_random_string
# If your platform is a BROWSER app on a different origin, allow cross-origin:
META_OSINT_API_CORS=true
```

Paths default to inside the code folder (`meta_osint/data/…`), so leave them
unset unless your media lives elsewhere.

---

## 7. Run the backend

```bash
python -m meta_osint.main serve --port 5002
```

You'll see it print the active backend:

```
Dashboard: http://localhost:5002
Database:  MySQL @ 127.0.0.1/meta_osint
```

Confirm the API is up:

```bash
curl http://localhost:5002/api/v1/health
# {"data":{"status":"ok",...},"meta":{}}
```

If you set an API key, include it: `-H "X-API-Key: <key>"`.

> **Run it as a service** so it stays up: on Linux use a `systemd` unit that runs
> the `python -m meta_osint.main serve` command in the venv; on Windows use NSSM
> or Task Scheduler. For heavier use, front it with a WSGI server
> (`waitress-serve --port=5002 "meta_osint.web.app:create_app()"`).

---

## 8. Integrate from the new platform

The new platform calls `http://localhost:5002/api/v1/...`. See **`meta_osint/web/API.md`**
for all 26 endpoints, and **`meta_osint/web/example_client.py`** for a ready Python
client. Examples:

```bash
# Read data
curl http://localhost:5002/api/v1/stats               -H "X-API-Key: KEY"
curl "http://localhost:5002/api/v1/posts?sort=relevancy&limit=20" -H "X-API-Key: KEY"
curl http://localhost:5002/api/v1/strategic/overview  -H "X-API-Key: KEY"

# Drive the backend
curl -X POST http://localhost:5002/api/v1/strategic/keywords \
     -H "X-API-Key: KEY" -H "Content-Type: application/json" \
     -d '{"keywords":["nuclear","missile defense"]}'
curl -X POST http://localhost:5002/api/v1/enrich -H "X-API-Key: KEY" -d '{}'
```

From Python:

```python
from meta_osint.web.example_client import MetaOsintClient
api = MetaOsintClient("http://localhost:5002", api_key="KEY")
posts = api.posts(sort="relevancy", limit=20)
job = api.enrich(); api.wait_for_job(job["id"])
overview = api.strategic_overview()
```

Any language works — it's plain HTTP + JSON. Media URLs in responses are absolute
(`http://localhost:5002/media/<file>`) so the platform can load images directly.

---

## 9. Enabling live scraping/enrichment (optional)

Only needed if the new platform will scrape new data (not just read existing):

1. **Ollama**: `ollama serve` running + `ollama pull llama3.2`. Enrichment then
   works via `POST /api/v1/enrich`.
2. **Chrome for scraping**: launch each platform's Chrome with the helper scripts
   (`meta_osint/scripts/start_chrome_instagram.bat` / `_facebook.bat`, or the
   `.sh` on Linux/mac). **A human logs into Instagram/Facebook in those Chrome
   windows once.** The scraper attaches to them over CDP — it never handles the
   password. Check status via `GET /api/v1/login-status`, or open a login page
   with `POST /api/v1/login/<platform>`. Then `POST /api/v1/scrape`.

---

## 10. Checklist

- [ ] Python 3.10+, MySQL 8+ installed (+ Chrome & Ollama if scraping)
- [ ] `pip install -r requirements.txt` in a venv
- [ ] `python -m playwright install chromium` (if scraping)
- [ ] MySQL database + user created
- [ ] Dump loaded (`… < meta_osint_mysql_dump.sql`) — `posts` = 358
- [ ] `media/` folder copied into `meta_osint/data/media/` (if images needed)
- [ ] `.env` filled: MySQL creds, `META_OSINT_DB_BACKEND=mysql`, an API key
- [ ] `python -m meta_osint.main serve --port 5002` → `/api/v1/health` returns ok
- [ ] New platform calls the API and gets data back

---

### Architecture (what runs where, all on the target machine)

```
   Your new platform  ──HTTP──▶  meta_osint backend  ──▶  MySQL (local)
   (any language)               (:5002, /api/v1)      ──▶  media/ folder (local)
                                        │
                                        ├──▶ Ollama    (localhost:11434)  [enrichment]
                                        └──▶ Chrome/CDP (9222/9223)        [scraping]
```

Nothing depends on the origin machine after the code + dump + media are copied.
