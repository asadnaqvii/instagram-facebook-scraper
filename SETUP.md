# Setup — running Strat-Watch Meta on a fresh computer

This gets you from **nothing installed** to a working dashboard.
**You do NOT need Ollama.** (It's an optional add-on — see the end.)

---

## The absolute fastest path (Windows) — one click

1. **Install Python** (one time): download Python 3.10+ from
   <https://www.python.org/downloads/>. During install, **tick
   "Add Python to PATH"**.
2. **Install Google Chrome** if you don't have it.
3. **Double-click `run.bat`** in this folder.

`run.bat` does the rest automatically:
- installs the Python packages,
- installs the browser engine,
- opens **two Chrome windows** (Facebook + Instagram),
- starts the dashboard and opens <http://localhost:5000>.

**Your one job:** when the two Chrome windows open, **log into Facebook in one
and Instagram in the other**, and leave them open. That's it.

> On macOS / Linux, run `bash run.sh` instead of `run.bat`.

---

## What "logging in" is for (important)

This tool does **not** ask for your password. Instead it *attaches* to a
Chrome window that you personally logged into. That's what keeps it working
and avoids the bot-blocks that break normal scrapers. So the login step is
required, and it's a normal human login in a normal Chrome window.

Use a **dedicated / throwaway account**, not your main one.

---

## Manual steps (if you prefer, or the one-click fails)

```bash
# 1. install packages (from this folder)
pip install -r meta_osint/requirements.txt
python -m playwright install chromium

# 2. open the two Chrome windows and LOG IN to each
meta_osint\scripts\start_chrome_facebook.bat      # Facebook  (port 9222)
meta_osint\scripts\start_chrome_instagram.bat     # Instagram (port 9223)

# 3. check everything is ready
python -m meta_osint.main diagnose

# 4. start the dashboard
python -m meta_osint.main serve
#    -> open http://localhost:5000
```

`diagnose` prints a clear checklist:

```
  yt-dlp           : found ...
  Ollama           : NOT reachable ... (self-healing + analysis skipped)   <- fine!
  Chrome CDP instagram: reachable on 9223
  Chrome CDP facebook : reachable on 9222
```

Seeing `Ollama: NOT reachable` is **completely fine** — the scraper works
without it.

---

## Using it

- **Dashboard** (`http://localhost:5000`) → **New scrape** → type keywords
  (comma or newline separated) → **Start scrape**.
- Or from the command line:
  ```bash
  python -m meta_osint.main search -k "nuclear" "ballistic missile"
  ```
- Browse results on the dashboard: metric tiles, a keyword mosaic (bigger
  tile = more data), posts with media, per-keyword drill-downs, and a
  light/dark theme toggle.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not connect to Chrome` | The Chrome windows aren't running / you closed them. Re-run the `start_chrome_*.bat` scripts and log in. |
| Scrape returns 0 posts | Make sure you're **logged in** in the Chrome windows. Run `diagnose`. |
| `python` not found | Reinstall Python with "Add to PATH" ticked, or use `py` instead of `python`. |
| Login challenge / CAPTCHA appears | Meta is rate-limiting. Stop, wait a while, use a throwaway account, scrape less aggressively. |
| Port 5000 in use | `python -m meta_osint.main serve --port 5001` |

---

## Optional: enable self-healing + content analysis (Ollama)

Only if you want the extra resilience layer (auto-repair of selectors if Meta
changes their page structure) and sentiment/entity analysis:

1. Install Ollama from <https://ollama.com>.
2. Pull a model:
   ```bash
   ollama pull llama3.2:latest
   ```
3. That's it — the scraper auto-detects it on the next run. `diagnose` will
   then show `Ollama: up`.

Without this, the scraper uses its built-in selectors and still captures
everything; it just can't auto-repair if Meta reshuffles their DOM.
