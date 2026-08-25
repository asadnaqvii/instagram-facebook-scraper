# Complete Linux setup — from a fresh PC to a running system

Everything needed to stand up this backend on a new Linux machine: the scraper,
the AI enrichment (Ollama), the database (with your existing data), the media,
and the JSON API. Follow top to bottom. Commands assume Ubuntu/Debian; adjust the
package manager for other distros.

**What you get at the end:** a local backend on `http://localhost:5002` exposing a
JSON API (26 endpoints), able to scrape Instagram/Facebook, run AI enrichment, and
serve the already-scraped data.

> Deeper dives live in companion docs, referenced where relevant:
> `MIGRATION.md` (data handoff detail), `LINUX_SCRAPING.md` (headless scraping),
> `meta_osint/web/API.md` (every endpoint).

---

## 0. Decide what you need

| You want to… | Then you need |
|---|---|
| Read/serve the already-scraped data via the API | Python + database + media |
| Also run **AI enrichment** (strategic scoring) | + **Ollama** (§4) |
| Also **scrape new** Instagram/Facebook data | + **Chrome + virtual display** (§6) |

Skip the sections you don't need. §1–3 + §7 are the minimum.

---

## 1. System dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl wget gnupg lsof \
                    mysql-server
```

Check Python is 3.10+:

```bash
python3 --version        # need >= 3.10
```

---

## 2. Get the code

```bash
git clone https://github.com/asadnaqvii/instagram-facebook-scraper.git
cd instagram-facebook-scraper
git checkout feature/strategic-intelligence

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you'll scrape, also install the browser engine (safe to run regardless):

```bash
python -m playwright install chromium
python -m playwright install-deps        # system libs (uses sudo)
```

---

## 3. Database (MySQL) + load your data

The repo ships a full data dump — one command creates every table **and** loads
the 358 posts, accounts, comments, and strategic scores.

```bash
# Secure MySQL once (set a root password, remove test DBs):
sudo mysql_secure_installation      # answer the prompts

# Create the app database + a user:
sudo mysql -e "CREATE DATABASE meta_osint CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
sudo mysql -e "CREATE USER 'meta_app'@'localhost' IDENTIFIED BY 'CHOOSE_A_PASSWORD'; \
               GRANT ALL PRIVILEGES ON meta_osint.* TO 'meta_app'@'localhost'; FLUSH PRIVILEGES;"

# Load the data:
mysql -u meta_app -p meta_osint < meta_osint/database/meta_osint_mysql_dump.sql

# Verify (expect 358):
mysql -u meta_app -p meta_osint -e "SELECT COUNT(*) AS posts FROM posts;"
```

> **Fresh start instead of the shipped data?** Load `schema_mysql.sql` (empty
> schema) instead of the dump.
>
> **Prefer no MySQL?** The default SQLite backend needs nothing — the repo already
> includes `meta_osint/data/meta_osint.db`. Skip this whole section and leave
> `META_OSINT_DB_BACKEND=sqlite` in step 5.

---

## 4. Ollama (for AI enrichment) — install if not already present

The enrichment feature (strategic scoring, sentiment, entities) runs a **local
LLM via Ollama**. Skip this section if you only need scraping + reading data.

**Check if it's already installed:**

```bash
ollama --version && curl -s http://localhost:11434/api/tags >/dev/null && echo "Ollama is up" || echo "Ollama not running/installed"
```

**Install it (official one-liner):**

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

The installer sets up a `systemd` service, so Ollama starts on boot and stays
running. Confirm:

```bash
systemctl status ollama          # should be active (running)
# if not: sudo systemctl enable --now ollama
```

**Pull the two models the code uses** (from `config.py`):

```bash
ollama pull llama3.2      # content analysis + strategic scoring (OLLAMA_MODEL)
ollama pull llama3.1      # selector-healing fallback (OLLAMA_HEAL_MODEL)
```

> `llama3.2` (~2 GB) is the main one and is enough for enrichment. `llama3.1`
> (~4.7 GB) is only used to self-heal scraper selectors when the site changes; pull
> it too if you'll scrape, skip it if you only enrich. These need a few GB of RAM;
> a GPU helps but isn't required.

**Verify from the app's side:**

```bash
curl -s http://localhost:11434/api/tags | grep -o '"name":"[^"]*"'
```

You should see `llama3.2` (and `llama3.1`) listed.

---

## 5. Configure (`.env`)

```bash
cp .env.example .env
nano .env
```

A typical full config:

```ini
# Database — MySQL you loaded in §3 (or leave as sqlite)
META_OSINT_DB_BACKEND=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=meta_app
MYSQL_PASSWORD=CHOOSE_A_PASSWORD
MYSQL_DB=meta_osint

# Ollama — local, from §4
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:latest
OLLAMA_HEAL_MODEL=llama3.1:latest
LLM_ENABLED=true

# Chrome CDP ports (scraping) — facebook 9222, instagram 9223
CDP_PORT_FACEBOOK=9222
CDP_PORT_INSTAGRAM=9223

# API access control (recommended so only your platform can call it)
META_OSINT_API_KEY=generate_a_long_random_string
META_OSINT_API_CORS=true          # only if a browser app on another origin calls it
```

Paths default to inside the code folder, so leave them unset for portability.

---

## 6. Media + (optional) scraping setup

**Media** (images/video the posts reference): the DB stores bare filenames, and
the app serves them from `meta_osint/data/media/`. To show existing images, copy
that folder from the origin machine into the same path here (~1.4 GB) — WinSCP,
`scp`, or rsync. If you don't need images, skip it; posts still return, image URLs
just won't resolve. New scrapes populate this folder automatically.

**Scraping** needs a real, human-logged-in Chrome with a display. On a headless
server that means a **virtual display (Xvfb)** + a **one-time VNC login** — the
full flow is in **`LINUX_SCRAPING.md`**. The short version:

```bash
# system deps for a headless display:
sudo apt install -y xvfb x11vnc fluxbox
# Chrome:
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb

# virtual display + the two CDP Chrome windows:
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 & fluxbox &
bash meta_osint/scripts/chrome_cdp.sh start      # facebook:9222, instagram:9223

# then VNC in ONCE to log into each (see LINUX_SCRAPING.md), then:
python -m meta_osint.main diagnose               # checks CDP + Ollama + yt-dlp
```

---

## 7. Run it

**The backend / API + dashboard:**

```bash
source .venv/bin/activate
python -m meta_osint.main serve --port 5002
```

It prints the active backend, e.g. `Database: MySQL @ 127.0.0.1/meta_osint`.
Confirm the API:

```bash
curl http://localhost:5002/api/v1/health
```

**Scraping (once §6 is done and you're logged in):**

```bash
export DISPLAY=:99
python -m meta_osint.main search -k "nuclear" -p instagram -n 8
python -m meta_osint.main search -f topics.txt          # multiple keywords, both platforms
```

**AI enrichment (once §4 is done)** — via the API or CLI:

```bash
# add strategic keywords, then enrich (API):
curl -X POST http://localhost:5002/api/v1/strategic/keywords \
     -H "Content-Type: application/json" -d '{"keywords":["nuclear","missile defense"]}'
curl -X POST http://localhost:5002/api/v1/enrich -d '{}'
```

---

## 8. Keep it running (systemd service)

So the API survives logout/reboot, create `/etc/systemd/system/meta-osint.service`:

```ini
[Unit]
Description=meta_osint backend API
After=network.target mysql.service ollama.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/instagram-facebook-scraper
Environment=PATH=/home/YOUR_USER/instagram-facebook-scraper/.venv/bin
ExecStart=/home/YOUR_USER/instagram-facebook-scraper/.venv/bin/python -m meta_osint.main serve --port 5002
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now meta-osint
sudo systemctl status meta-osint
```

> For higher load, run under a WSGI server instead of the dev server:
> `.venv/bin/pip install waitress` then
> `ExecStart=…/.venv/bin/waitress-serve --port=5002 "meta_osint.web.app:create_app"`.
>
> Scraping needs the Xvfb display + logged-in Chrome, so keep those in their own
> `tmux`/service (see `LINUX_SCRAPING.md`) — the API service above is separate.

---

## 9. Integrate the new platform

Your platform calls `http://localhost:5002/api/v1/...`. Full endpoint reference in
`meta_osint/web/API.md`; a ready Python client in `meta_osint/web/example_client.py`.

```bash
curl http://localhost:5002/api/v1/strategic/overview -H "X-API-Key: YOUR_KEY"
```

---

## Checklist

- [ ] Python 3.10+, git, MySQL installed (§1)
- [ ] Repo cloned, venv created, `pip install -r requirements.txt` (§2)
- [ ] `playwright install chromium` (if scraping)
- [ ] MySQL DB + user created, dump loaded — `posts` = 358 (§3)
- [ ] Ollama installed + running, `llama3.2` (and `llama3.1` if scraping) pulled (§4)
- [ ] `.env` filled: DB creds, Ollama, API key (§5)
- [ ] media copied to `meta_osint/data/media/` (if images needed) (§6)
- [ ] Chrome + Xvfb + one-time login done (if scraping) (§6)
- [ ] `serve` runs, `/api/v1/health` returns ok (§7)
- [ ] systemd service enabled (§8)
- [ ] new platform reaches the API (§9)

### What runs where (all local to this machine)

```
   Your new platform ──HTTP──▶ meta_osint API (:5002) ──▶ MySQL (local)
                                        │                ──▶ media/ folder
                                        ├──▶ Ollama    (localhost:11434)
                                        └──▶ Chrome/CDP (9222/9223 via Xvfb)
```
