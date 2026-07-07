# meta_osint

Keyword-driven OSINT scraper for **Instagram & Facebook**. Give it a list of
keywords and it pulls the full breadth of public content — posts, captions,
images, videos, hashtags, locations, account info, comments, shares, likes,
views — into a local SQLite database, then lets you browse it from a web
dashboard.

It is built to survive Meta's constant DOM churn (the thing that makes naïve
scrapers return empty results):

1. **CDP into your own logged-in Chrome** — no automated login, so no login
   walls and far less bot detection.
2. **yt-dlp for post metadata + media** — talks to Meta's internal endpoints,
   not the page markup, so it keeps working across redesigns.
3. **Self-healing selectors (local LLM)** — DOM-dependent reads target stable
   semantic anchors; when one stops matching, the relevant HTML is handed to
   a local Ollama model which proposes a fresh selector. The fix is cached to
   a versioned selector map and reused for free until it breaks again.

---

## Setup

```bash
cd "F:/Meta scraper"
pip install -r meta_osint/requirements.txt
python -m playwright install chromium      # only needed as a fallback

# (optional but recommended) local LLM for self-healing + analysis
#   install Ollama, then:
ollama pull llama3.2:latest
```

### 1. Launch a logged-in Chrome per platform

Run these once and log into each site in the window that opens. The sessions
persist in their own Chrome profiles, so you only log in once.

```
meta_osint\scripts\start_chrome_facebook.bat     # CDP on 9222
meta_osint\scripts\start_chrome_instagram.bat    # CDP on 9223
```

### 2. Check the environment

```bash
python -m meta_osint.main diagnose
```

Confirms yt-dlp, Ollama, and both Chrome CDP endpoints are reachable.

---

## Usage

```bash
# Full keyword search across both platforms:
#   accounts + hashtags + places + posts (with comments, media, engagement)
python -m meta_osint.main search -k "climate change" "renewable energy"

# Keywords from a file, Instagram only, 30 posts each, skip comments
python -m meta_osint.main search -f keywords.txt -p instagram -n 30 --no-comments

# Scrape specific accounts / pages
python -m meta_osint.main scrape --mode profile -k natgeo nasa

# Scrape a hashtag feed
python -m meta_osint.main scrape --mode hashtag -k nuclear -p facebook

# Browse everything scraped so far
python -m meta_osint.main stats
python -m meta_osint.main serve            # dashboard at http://localhost:5000
```

`keywords.txt` is one keyword per line; `#` lines are comments.

---

## What gets captured

| Entity   | Fields |
|----------|--------|
| Post     | url, id, kind, author, text, hashtags, mentions, media, location, timestamp, likes, comments_count, shares, views |
| Media    | url, type (image/video/reel), thumbnail, dimensions, duration, **downloaded local file** |
| Comment  | author, text, timestamp, likes (sampled per post) |
| Account  | username, display name, bio, avatar, verified/private, category, external url, followers/following/posts/likes |
| Hashtag  | tag, url, post count |
| Location | name, id, url, lat/long |
| Analysis | sentiment + score, people/orgs/locations entities, topics, language (when Ollama is on) |

Everything lands in `meta_osint/data/meta_osint.db`; downloaded media in
`meta_osint/data/media/`.

---

## How the self-healing works

`meta_osint/selectors/selectors.json` holds every DOM extraction target with
its built-in semantic selectors and a slot for an LLM-healed one. On each run
the healed selector is tried first (no LLM call). When nothing matches and
Ollama is up, `SelectorHealer` captures the DOM subtree, asks the model for a
new selector, validates it against the live page, and writes it back. The file
is plain JSON — inspect it to see exactly how Meta's markup has drifted.

If Ollama is down or disabled (`LLM_ENABLED=false`), the scraper simply uses
its heuristic selectors and skips analysis — it never blocks on the LLM.

---

## Configuration

Copy `.env.example` to `.env` to override any default (CDP ports, scroll
depth, media size cap, Ollama URL/model, feature switches). See
`meta_osint/config.py` for the full list.

---

## Architecture

```
meta_osint/
  config.py            central config (env-overridable)
  models.py            pydantic models — the canonical field set
  orchestrator.py      multi-keyword engine (one CDP conn per platform)
  main.py              CLI (search / scrape / stats / diagnose / serve)
  browser/manager.py   CDP connect to your Chrome; human-like scrolling
  llm/
    ollama_client.py   local LLM client (degrades gracefully)
    selector_store.py  versioned, cached selector map
    healer.py          selector healing + content analysis
  scraper/
    instagram.py       IG search / profile / hashtag / post / comments
    facebook.py        FB search / page / hashtag / post / comments
    media.py           yt-dlp metadata + media download
    helpers.py         parsing utilities
  pipeline/            normalize + dedup + store (+ analysis)
  database/db.py       SQLite persistence (normalised, idempotent)
  web/                 Flask dashboard
  scripts/             Chrome CDP launchers (Windows + shell)
```

## Notes

* Only public content you are authorised to view is accessible — the scraper
  reads what your logged-in account can already see.
* Be considerate with scroll depth and keyword volume; the human-like delays
  are there for a reason.
