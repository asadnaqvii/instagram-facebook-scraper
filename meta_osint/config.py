"""Central configuration for meta_osint.

All values can be overridden via environment variables (a .env file is
loaded automatically if python-dotenv is installed). Nothing here should
require editing for a normal run — the defaults target a local setup with
two Chrome instances (one for Facebook, one for Instagram) exposed over
CDP, and a local Ollama server.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional
    pass


# ── Paths ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("META_OSINT_DATA_DIR", str(BASE_DIR / "data")))
MEDIA_DIR = DATA_DIR / "media"
SELECTOR_DIR = Path(os.getenv("META_OSINT_SELECTOR_DIR", str(BASE_DIR / "selectors")))
DB_PATH = Path(os.getenv("META_OSINT_DB", str(DATA_DIR / "meta_osint.db")))

for _d in (DATA_DIR, MEDIA_DIR, SELECTOR_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ── Database backend ─────────────────────────────────────────────────
# 'sqlite' (default, local file) or 'mysql' (shared server — set the MYSQL_*
# vars below). The rest of the app goes through database.get_database(), which
# returns the right backend, so switching is purely config: copy the code +
# data/media to another PC, point these at the same MySQL, and it runs.
DB_BACKEND = os.getenv("META_OSINT_DB_BACKEND", "sqlite").lower()

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv("MYSQL_DB", "")


def mysql_dsn() -> dict:
    """Connection kwargs for the MySQL driver."""
    return {
        "host": MYSQL_HOST,
        "port": MYSQL_PORT,
        "user": MYSQL_USER,
        "password": MYSQL_PASSWORD,
        "database": MYSQL_DB,
    }


# ── Browser / CDP ────────────────────────────────────────────────────
# One Chrome instance per platform so their sessions never clash. Launch
# them with the helper scripts in scripts/ (or manually with
# --remote-debugging-port). The scraper connects, it never launches Chrome
# itself — that is what keeps you logged in and under the bot-detection
# radar.
CDP_PORT_FACEBOOK = int(os.getenv("CDP_PORT_FACEBOOK", "9222"))
CDP_PORT_INSTAGRAM = int(os.getenv("CDP_PORT_INSTAGRAM", "9223"))
BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "60000"))


# Chrome binds its CDP port to IPv4 only. On hosts where "localhost" resolves to
# IPv6 (::1) first, connecting to "http://localhost:PORT" fails with
# ECONNREFUSED ::1:PORT even though Chrome is running — so default to the
# explicit IPv4 loopback. Override with CDP_HOST if Chrome runs elsewhere.
CDP_HOST = os.getenv("CDP_HOST", "127.0.0.1")


def cdp_endpoint(platform: str) -> str:
    """CDP URL for a platform ('facebook' | 'instagram')."""
    port = CDP_PORT_INSTAGRAM if platform == "instagram" else CDP_PORT_FACEBOOK
    return f"http://{CDP_HOST}:{port}"


# ── Scraping behaviour ───────────────────────────────────────────────
MAX_SCROLLS = int(os.getenv("MAX_SCROLLS", "20"))
SCROLL_INCREMENT_PX = int(os.getenv("SCROLL_INCREMENT_PX", "800"))
# Delays are randomised within [min, max] to look human.
# Base delays (used for Facebook). Instagram is stricter about rate limits,
# so it gets a multiplier (below) on top of these.
MIN_ACTION_DELAY_S = float(os.getenv("MIN_ACTION_DELAY_S", "2.0"))
MAX_ACTION_DELAY_S = float(os.getenv("MAX_ACTION_DELAY_S", "4.5"))
MIN_SCROLL_DELAY_S = float(os.getenv("MIN_SCROLL_DELAY_S", "2.0"))
MAX_SCROLL_DELAY_S = float(os.getenv("MAX_SCROLL_DELAY_S", "4.0"))

# Instagram throttles (HTTP 429) far more readily than Facebook, so pace it
# slower. All IG delays are multiplied by this. Raise it if you keep hitting
# 429s; lower it (toward 1.0) if you want more speed and fewer 429 worries.
IG_DELAY_MULTIPLIER = float(os.getenv("IG_DELAY_MULTIPLIER", "2.2"))

# Extra cooldown between opening each Instagram post. Opening a post is a full
# page navigation — the main 429 trigger — so a real pause here (on top of the
# per-action delay) matters most. Randomised up to +50%.
IG_PER_POST_COOLDOWN_S = float(os.getenv("IG_PER_POST_COOLDOWN_S", "4.0"))

# Cap IG posts-per-keyword lower than the global default, so each keyword is a
# smaller burst. Set to 0 to use the requested max_posts unchanged.
IG_MAX_POSTS_CAP = int(os.getenv("IG_MAX_POSTS_CAP", "8"))

# When a 429 (rate limit) is detected, pause this long before continuing.
# Doubles on repeated hits (capped). This is what stops the scraper from
# hammering Instagram harder when it's already asking us to slow down.
RATE_LIMIT_BACKOFF_S = float(os.getenv("RATE_LIMIT_BACKOFF_S", "90"))
RATE_LIMIT_MAX_BACKOFF_S = float(os.getenv("RATE_LIMIT_MAX_BACKOFF_S", "600"))

# Per-post / per-comment collection caps (safety valves).
MAX_MEDIA_PER_POST = int(os.getenv("MAX_MEDIA_PER_POST", "20"))
MAX_COMMENTS_PER_POST = int(os.getenv("MAX_COMMENTS_PER_POST", "50"))
MAX_ACCOUNTS_PER_SEARCH = int(os.getenv("MAX_ACCOUNTS_PER_SEARCH", "20"))
MAX_HASHTAGS_PER_SEARCH = int(os.getenv("MAX_HASHTAGS_PER_SEARCH", "20"))

# yt-dlp media download ceiling.
MAX_MEDIA_FILESIZE = os.getenv("MAX_MEDIA_FILESIZE", "80M")
DOWNLOAD_MEDIA = os.getenv("DOWNLOAD_MEDIA", "true").lower() == "true"


# ── LLM (Ollama) ─────────────────────────────────────────────────────
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:latest")
OLLAMA_TIMEOUT_S = int(os.getenv("OLLAMA_TIMEOUT_S", "60"))
# When the fast model's selector proposals all fail validation, the healer
# escalates to this stronger model for one more attempt. Leave empty to
# disable escalation. llama3.1 (8B) is a reliable pick for selector reasoning;
# avoid pure "reasoning" models (e.g. gpt-oss) here — they wrap output in
# think-tags that don't parse as JSON.
OLLAMA_HEAL_MODEL = os.getenv("OLLAMA_HEAL_MODEL", "llama3.1:latest")
# Master switch. When false (or Ollama unreachable) the scraper falls back
# to its built-in heuristic selectors and skips content analysis.
LLM_ENABLED = os.getenv("LLM_ENABLED", "true").lower() == "true"
# Turn the self-healing selector proposals on/off independently of analysis.
LLM_SELECTOR_HEALING = os.getenv("LLM_SELECTOR_HEALING", "true").lower() == "true"
# Content analysis (sentiment/entities/topics) is OFF by default: it costs one
# LLM call per post and is pure overhead when you only want the raw data.
# Enable with LLM_CONTENT_ANALYSIS=true or the --analyze CLI flag.
LLM_CONTENT_ANALYSIS = os.getenv("LLM_CONTENT_ANALYSIS", "false").lower() == "true"


# ── Misc ─────────────────────────────────────────────────────────────
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)

PLATFORMS = ("instagram", "facebook")


# ── JSON API (for integrating the backend into other apps) ───────────
# Optional shared-secret auth: set META_OSINT_API_KEY and callers send it as
# `X-API-Key: <key>` (or `Authorization: Bearer <key>`). Empty = open (typical
# for a local backend). Set META_OSINT_API_CORS=true to allow browser apps on
# other origins to call the API.
API_KEY = os.getenv("META_OSINT_API_KEY", "")
API_CORS = os.getenv("META_OSINT_API_CORS", "false").lower() == "true"


def as_dict() -> dict:
    """Snapshot the effective config (for the dashboard / debugging)."""
    return {
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_PATH),
        "media_dir": str(MEDIA_DIR),
        "selector_dir": str(SELECTOR_DIR),
        "cdp_facebook": cdp_endpoint("facebook"),
        "cdp_instagram": cdp_endpoint("instagram"),
        "max_scrolls": MAX_SCROLLS,
        "download_media": DOWNLOAD_MEDIA,
        "ollama_url": OLLAMA_URL,
        "ollama_model": OLLAMA_MODEL,
        "llm_enabled": LLM_ENABLED,
        "llm_selector_healing": LLM_SELECTOR_HEALING,
        "llm_content_analysis": LLM_CONTENT_ANALYSIS,
    }
