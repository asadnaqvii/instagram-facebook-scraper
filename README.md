# Strat-Watch Meta — Instagram & Facebook OSINT Scraper

A keyword-driven scraper that extracts the full breadth of public content from
**Instagram and Facebook** — posts, captions, images, videos, hashtags,
locations, account info, comments, shares, likes, views — into a local SQLite
database, with a web dashboard to browse it.

> **New here?** The fastest path is **[`SETUP.md`](SETUP.md)** — on Windows you
> can literally double-click **`run.bat`** and it installs everything, opens
> Chrome to log in, and starts the dashboard. **Ollama is not required.**

It is built to survive Meta's constant DOM churn (the thing that makes naïve
selector-based scrapers silently return empty results):

1. **CDP into your own logged-in Chrome** — attaches to a browser you launched
   and logged into, so no automated login, no login walls, minimal bot
   detection.
2. **yt-dlp for post metadata + media** — reads Meta's own internal data
   endpoints rather than the page HTML, so caption/likes/comments/views/media
   keep working across visual redesigns.
3. **Self-healing selectors (local LLM)** — the HTML we do read targets stable
   semantic anchors (roles, aria-labels, URL patterns). When one breaks, the
   DOM is handed to a local Ollama model that proposes a fresh selector; the
   fix is validated against the live page and cached to a versioned file, so
   the repair is permanent and free until it too breaks.

Everything degrades gracefully: if Ollama is off, a field is missing, or a
post is private, nothing crashes — it captures what it can and moves on.

## What you need (and what's optional)

| Requirement | Needed? | If you skip it |
|---|---|---|
| **Python 3.10+** + `pip install` | **Required** | — |
| **yt-dlp** (installed by pip) | **Required** | Post captions, likes, comments, views, and media downloads won't work — this is the backbone |
| **Chrome, logged into IG/FB** (launched via the scripts) | **Required** | "Could not connect to Chrome" — the scraper reads *your* logged-in session, so this is the main setup step |
| **Ollama** (a separate local-LLM app) | **Optional** | The scraper still works fully. You only lose: (1) *self-healing* — if Meta changes their DOM and a built-in selector breaks, it can't auto-repair that one field; (2) *content analysis* — sentiment/entities/topics (off by default anyway) |

**In plain terms: you do NOT need Ollama to scrape.** It's a resilience layer.
Without it, everything runs on the built-in semantic selectors + yt-dlp, and
the scraper degrades gracefully (it never crashes because Ollama is missing).
Install it only if you want the self-healing safety net for the long term.

## Quick start

```bash
# 1. install (required)
pip install -r meta_osint/requirements.txt
python -m playwright install chromium         # fallback browser (CDP is default)

# 2. launch a logged-in Chrome per platform, log in once in each window (required)
meta_osint\scripts\start_chrome_facebook.bat     # CDP :9222
meta_osint\scripts\start_chrome_instagram.bat    # CDP :9223

# 3. check the environment — tells you exactly what's ready / missing
python -m meta_osint.main diagnose

# 4. scrape (both platforms, accounts + hashtags + posts per keyword)
python -m meta_osint.main search -k "nuclear" "ballistic missile"

# 5. browse the results
python -m meta_osint.main serve                  # http://localhost:5000
```

### Optional: enable self-healing + analysis

```bash
# install Ollama from https://ollama.com, then:
ollama pull llama3.2:latest      # fast model for selector healing
ollama pull llama3.1:latest      # stronger fallback (optional)
# that's it — the scraper auto-detects Ollama on the next run
```

`python -m meta_osint.main diagnose` will show `Ollama: up` once it's running,
or `NOT reachable ... (self-healing + analysis will be skipped)` if it isn't —
either way the scraper works.

See [`meta_osint/README.md`](meta_osint/README.md) for the full documentation,
architecture, and configuration.

## Use responsibly

Only scrape public content you are authorised to view, from a dedicated
account, at a human pace. This is a research/OSINT tool.
