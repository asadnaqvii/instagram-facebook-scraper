"""Configuration settings for the Meta scraper."""
import os
from dotenv import load_dotenv

load_dotenv()

# Browser settings
HEADLESS = os.getenv("HEADLESS", "True").lower() == "true"
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "60000"))

# Scraper settings
MAX_SCROLLS = int(os.getenv("MAX_SCROLLS", "20"))
SCROLL_DELAY = int(os.getenv("SCROLL_DELAY", "2000"))
SCROLL_INCREMENT = int(os.getenv("SCROLL_INCREMENT", "600"))

# Human-like behavior
MIN_DELAY_BETWEEN_ACTIONS = int(os.getenv("MIN_DELAY_BETWEEN_ACTIONS", "2000"))
MAX_DELAY_BETWEEN_ACTIONS = int(os.getenv("MAX_DELAY_BETWEEN_ACTIONS", "5000"))
MIN_SCROLL_DELAY = int(os.getenv("MIN_SCROLL_DELAY", "1500"))
MAX_SCROLL_DELAY = int(os.getenv("MAX_SCROLL_DELAY", "4000"))
RANDOM_PAUSE_CHANCE = float(os.getenv("RANDOM_PAUSE_CHANCE", "0.3"))
MOUSE_MOVEMENT_CHANCE = float(os.getenv("MOUSE_MOVEMENT_CHANCE", "0.4"))

# Database
DB_PATH = os.getenv("DB_PATH", "data/meta_scraper.db")

# CDP ports — one Chrome instance per platform
CDP_PORT_FACEBOOK = int(os.getenv("CDP_PORT_FACEBOOK", "9222"))
CDP_PORT_INSTAGRAM = int(os.getenv("CDP_PORT_INSTAGRAM", "9223"))

def get_cdp_endpoint(platform: str) -> str:
    if platform == "instagram":
        return f"http://localhost:{CDP_PORT_INSTAGRAM}"
    return f"http://localhost:{CDP_PORT_FACEBOOK}"

# Session persistence
SESSION_DIR = os.getenv("SESSION_DIR", "sessions")

# User agent
USER_AGENT = os.getenv("USER_AGENT", (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
))
