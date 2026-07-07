# Instagram & Facebook OSINT Scraper (`meta_osint`)

A keyword-driven scraper that extracts the full breadth of public content from
**Instagram and Facebook** — posts, captions, images, videos, hashtags,
locations, account info, comments, shares, likes, views — into a local SQLite
database, with a web dashboard to browse it.

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

## Quick start

```bash
pip install -r meta_osint/requirements.txt
python -m playwright install chromium         # fallback browser (CDP is default)

# (optional) local LLM for self-healing:  install Ollama, then
ollama pull llama3.2:latest

# 1. launch a logged-in Chrome per platform, log in once in each window
meta_osint\scripts\start_chrome_facebook.bat     # CDP :9222
meta_osint\scripts\start_chrome_instagram.bat    # CDP :9223

# 2. check the environment
python -m meta_osint.main diagnose

# 3. scrape (both platforms, accounts + hashtags + posts per keyword)
python -m meta_osint.main search -k "nuclear" "ballistic missile"

# 4. browse the results
python -m meta_osint.main serve                  # http://localhost:5000
```

See [`meta_osint/README.md`](meta_osint/README.md) for the full documentation,
architecture, and configuration.

## Use responsibly

Only scrape public content you are authorised to view, from a dedicated
account, at a human pace. This is a research/OSINT tool.
