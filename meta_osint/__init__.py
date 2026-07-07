"""
meta_osint — OSINT scraper for Instagram & Facebook.

A keyword-driven scraper that extracts the full breadth of public content
(posts, text, images, videos, hashtags, locations, account info, comments,
shares, likes, views) into a local SQLite database.

Design principles:
  * Connects to a real, logged-in Chrome via CDP to bypass login walls and
    bot detection (static-selector scraping against Meta's obfuscated DOM
    does not work — this is why the previous version returned empty results).
  * Uses yt-dlp for reliable post/reel metadata + media, which relies on
    Meta's own internal endpoints rather than the DOM, so it survives
    redesigns.
  * A self-healing selector layer: DOM-dependent extraction targets stable
    semantic anchors (role, aria-label, meta tags, URL patterns). When a
    field yields nothing, the DOM subtree is handed to a local LLM (Ollama)
    which proposes a fresh selector; the result is cached to a versioned
    selector map and reused until it breaks again.
"""

__version__ = "1.0.0"
