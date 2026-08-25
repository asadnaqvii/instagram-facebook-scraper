# Running the scraper on a Linux server (SSH / PuTTY / WinSCP)

How to run the Instagram/Facebook scraper on a headless Linux box you reach over
SSH (PuTTY) and move files with (WinSCP).

## Read this first — the one hard requirement

The scraper does **not** open its own browser. It **attaches** over the Chrome
DevTools Protocol (CDP) to a real Chrome that **a human has logged into**
Instagram/Facebook. This is deliberate: a fresh automated/headless browser gets
login-walled by Meta almost immediately. Consequences on a server:

- **Chrome must run with a display.** A bare SSH session has none, so you give it
  a *virtual* display with **Xvfb**, and run Chrome **headful** (not `--headless`,
  which gets bot-detected).
- **A human logs in once**, by viewing that virtual display over **VNC**. After
  that the session persists in Chrome's profile folder.
- **Datacenter IPs get blocked harder.** A cloud VPS will hit rate limits (HTTP
  429) and checkpoints far more than a residential connection. If you can, run on
  a machine with a residential/clean IP, or expect to scrape slowly and in small
  batches.

PuTTY = your shell on the server. WinSCP = moving files (upload code, edit `.env`,
pull the downloaded media back). The scraper **runs in the shell**.

---

## Port map (matches `meta_osint/config.py`)

| Platform  | CDP port | Profile dir (`~/.meta-osint/`) |
|-----------|----------|--------------------------------|
| Facebook  | **9222** | `chrome-facebook`  |
| Instagram | **9223** | `chrome-instagram` |

Both overridable via `CDP_PORT_FACEBOOK` / `CDP_PORT_INSTAGRAM`.

---

## 1. System setup (once, in PuTTY)

```bash
sudo apt update
sudo apt install -y wget gnupg xvfb x11vnc fluxbox lsof curl python3-venv
# Google Chrome
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
```

## 2. Get the code + install (in PuTTY)

```bash
git clone https://github.com/asadnaqvii/instagram-facebook-scraper.git
cd instagram-facebook-scraper
git checkout feature/strategic-intelligence
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python -m playwright install-deps        # system libs Chromium needs (sudo)
```

> Or upload the folder with **WinSCP** instead of `git clone`. Either way, `cd`
> into it before the next steps.

## 3. Configure (in PuTTY or via WinSCP's editor)

```bash
cp .env.example .env
nano .env        # or edit with WinSCP
```

Set at least the database. To reuse the migrated data, point at MySQL; for a
quick standalone run, SQLite (the default) needs nothing:

```ini
META_OSINT_DB_BACKEND=mysql
MYSQL_HOST=127.0.0.1
MYSQL_USER=meta_app
MYSQL_PASSWORD=your_password
MYSQL_DB=meta_osint
```

## 4. Start a virtual display + the CDP Chrome windows (in PuTTY)

```bash
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 &
fluxbox &

bash meta_osint/scripts/chrome_cdp.sh start
```

`chrome_cdp.sh` launches Facebook Chrome on 9222 and Instagram Chrome on 9223,
each with a persistent profile. `status` / `stop` / `restart` also work.

## 5. Log in ONCE, over VNC (the step SSH alone can't do)

Chrome is running on display `:99`, which you can't see over plain SSH. Expose it
with VNC, tunnel it through SSH, and view it from Windows:

```bash
# on the server (PuTTY):
x11vnc -display :99 -localhost -nopw -forever &
```

On **Windows**:
1. Tunnel VNC through SSH. In PuTTY: *Connection → SSH → Tunnels* → Source port
   `5900`, Destination `localhost:5900`, **Add**, then open the session. (CLI
   equivalent: `plink -L 5900:localhost:5900 user@server`.)
2. Open a VNC viewer (TightVNC / RealVNC / UltraVNC) to `localhost:5900`.
3. You'll see the server's desktop with the two Chrome windows. **Log into
   Instagram in the IG window and Facebook in the FB window.** Solve any
   checkpoint/2FA here.
4. Close the VNC viewer. The logins persist — you only redo this if a session
   expires.

Verify the scraper can see everything:

```bash
python -m meta_osint.main diagnose      # checks Chrome CDP (9222/9223), Ollama, yt-dlp
```

## 6. Run scrapes (in PuTTY — the everyday commands)

```bash
source .venv/bin/activate
export DISPLAY=:99          # so it finds the logged-in Chrome

# One keyword on Instagram, 8 posts:
python -m meta_osint.main search -k "nuclear" -p instagram -n 8

# Facebook:
python -m meta_osint.main search -k "missile defense" -p facebook -n 15

# Both platforms, several keywords from a file:
python -m meta_osint.main search -f topics.txt

# Faster (skip comments); add --analyze for LLM sentiment/entities:
python -m meta_osint.main search -k "naval deployment" -p instagram -n 8 --no-comments
```

Scrape options: `-k <keywords...>`, `-f <file>`, `-p instagram|facebook` (omit =
both), `-n <max posts per keyword>`, `--no-comments`, `--analyze`.

Data lands in the DB; media downloads to `meta_osint/data/media/`.

## 7. Get the results off the server

- **Data**: already in your DB (MySQL or the SQLite file). Query via the app or
  `python -m meta_osint.main stats`.
- **Media**: `meta_osint/data/media/` fills with images/video. Pull it to Windows
  with **WinSCP** (drag the folder), or serve it via the dashboard/API.
- **Dashboard/API on the server** (optional): `python -m meta_osint.main serve
  --port 5002` then tunnel `5002` in PuTTY the same way as VNC to browse it, or
  bind it for your other platform to call (see `MIGRATION.md` / `web/API.md`).

---

## Keeping it running after you log out

Chrome + Xvfb die when your SSH session ends. To persist, run them in **tmux**:

```bash
sudo apt install -y tmux
tmux new -s scraper
# inside tmux: run steps 4–6. Detach with Ctrl-b then d. Reattach: tmux attach -t scraper
```

For a long-lived setup, make Xvfb + the Chrome windows a `systemd` service.

## Rate-limit tips (especially on a VPS)

- Instagram throttles hardest. Keep `-n` small (≤8), scrape a few keywords per
  session, and pause between runs. The scraper already backs off on 429s
  (`RATE_LIMIT_BACKOFF_S`), and IG is paced slower via `IG_DELAY_MULTIPLIER`.
- Don't browse Instagram in the scraper's Chrome window while it's scraping.
- If you get login-walled repeatedly, the IP is likely flagged — a residential
  IP or proxy helps; a bare datacenter IP is the worst case.

## Note on parallel windows

This scraper uses **one Chrome per platform** (2 total) and processes keywords
sequentially — it has no built-in multi-window parallel mode. To scrape in
parallel you'd run several copies of `meta_osint.main` with different
`CDP_PORT_INSTAGRAM` / `CDP_PORT_FACEBOOK` values (and ideally separate databases
to merge later), each attached to its own Chrome window. `chrome_cdp.sh` can be
extended to launch those extra windows.
