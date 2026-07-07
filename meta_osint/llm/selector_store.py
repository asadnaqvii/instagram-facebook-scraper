"""Versioned, on-disk selector map.

This is what makes the scraper *future-proof* rather than just LLM-assisted.
Each extraction target (e.g. instagram.post.like_count) has:
  * a list of built-in heuristic candidate selectors (semantic, stable), and
  * a slot for an LLM-healed selector that gets written back here after a
    successful heal.

On the next run the healed selector is tried first, for free — no LLM call —
until it too stops matching, at which point the healer proposes a new one and
overwrites it. The file is human-readable JSON so you can inspect/edit it and
see exactly how the DOM has drifted over time.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config

# Built-in defaults. These are intentionally SEMANTIC — role, aria-label,
# meta tags, URL patterns, dir attributes — because Meta's CSS class names
# are machine-generated and reshuffle constantly, but the accessibility and
# routing layers are far more stable. `js` entries are page.evaluate bodies
# used by the extractors directly; `candidates` are querySelector strings the
# healer/among-fallbacks logic can iterate.
DEFAULT_SELECTORS: dict[str, dict] = {
    # ── Instagram ────────────────────────────────────────────────
    "instagram.profile.description_meta": {
        "candidates": ['meta[name="description"]', 'meta[property="og:description"]'],
        "note": "Followers/following/posts + bio are packed into the description meta tag.",
    },
    "instagram.profile.title_meta": {
        "candidates": ['meta[property="og:title"]'],
    },
    "instagram.profile.image_meta": {
        "candidates": ['meta[property="og:image"]'],
    },
    "instagram.post.link": {
        "candidates": ['a[href*="/p/"]', 'a[href*="/reel/"]'],
    },
    "instagram.post.caption": {
        "candidates": ['h1', 'div[role="dialog"] h1', 'article h1'],
        "note": "Main caption is usually the first h1 in the post view.",
    },
    "instagram.post.like_count": {
        "candidates": [
            'section span:has-text("likes")',
            'a[href$="/liked_by/"] span',
            'section button span',
        ],
    },
    "instagram.post.time": {
        "candidates": ["time"],
    },
    "instagram.post.location": {
        "candidates": ['a[href*="/explore/locations/"]'],
    },
    "instagram.comment.list": {
        "candidates": [
            'ul ul[class] div[role="button"] + div',
            'div[role="dialog"] ul li',
            'article ul li',
        ],
        "note": "Comment nodes; healing likely needed — IG comment DOM is volatile.",
    },
    "instagram.search.account_link": {
        "candidates": ['a[href^="/"][role="link"]', 'a[href*="instagram.com/"]'],
    },
    # ── Facebook ─────────────────────────────────────────────────
    "facebook.profile.title_meta": {
        "candidates": ['meta[property="og:title"]'],
    },
    "facebook.profile.description_meta": {
        "candidates": ['meta[property="og:description"]'],
    },
    "facebook.profile.image_meta": {
        "candidates": ['meta[property="og:image"]'],
    },
    "facebook.search.post_container": {
        "candidates": ['div[role="feed"] > div', 'div[role="article"]'],
    },
    "facebook.post.text": {
        "candidates": ['div[data-ad-comet-above-more-text]', 'div[dir="auto"]'],
    },
    "facebook.post.link": {
        "candidates": [
            'a[href*="/posts/"]',
            'a[href*="/reel/"]',
            'a[href*="/videos/"]',
            'a[href*="/permalink/"]',
            'a[href*="story_fbid"]',
        ],
    },
    "facebook.post.image": {
        "candidates": ['img[src*="scontent"]'],
    },
    "facebook.post.time": {
        "candidates": ["abbr", 'a[role="link"] span[id]'],
    },
    "facebook.comment.list": {
        "candidates": [
            'div[role="article"][aria-label*="Comment"]',
            'ul[role="list"] div[dir="auto"]',
        ],
        "note": "FB comment blocks are marked with aria-label 'Comment by ...'.",
    },
    "facebook.search.account_link": {
        "candidates": ['a[href*="facebook.com/"][role="presentation"]', 'a[aria-label][href*="facebook.com/"]'],
    },
}


class SelectorStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else (config.SELECTOR_DIR / "selectors.json")
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
        # Merge in any new defaults without clobbering healed values.
        changed = False
        targets = self._data.setdefault("targets", {})
        for key, spec in DEFAULT_SELECTORS.items():
            if key not in targets:
                targets[key] = {
                    "candidates": list(spec.get("candidates", [])),
                    "note": spec.get("note"),
                    "healed": None,
                    "healed_at": None,
                    "heal_count": 0,
                }
                changed = True
            else:
                # Keep healed value, but refresh built-in candidates.
                targets[key]["candidates"] = list(spec.get("candidates", []))
        self._data.setdefault("version", 1)
        if changed:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ── access ───────────────────────────────────────────────────────

    def candidates(self, key: str) -> list[str]:
        """Ordered selectors to try: healed value first, then built-ins."""
        target = self._data.get("targets", {}).get(key)
        if not target:
            return []
        out: list[str] = []
        if target.get("healed"):
            out.append(target["healed"])
        for c in target.get("candidates", []):
            if c not in out:
                out.append(c)
        return out

    def record_heal(self, key: str, selector: str) -> None:
        """Persist a newly-healed selector as the preferred one for `key`."""
        with self._lock:
            targets = self._data.setdefault("targets", {})
            target = targets.setdefault(
                key, {"candidates": [], "healed": None, "heal_count": 0}
            )
            target["healed"] = selector
            target["healed_at"] = datetime.now(timezone.utc).isoformat()
            target["heal_count"] = target.get("heal_count", 0) + 1
            self._save()

    def clear_heal(self, key: str) -> None:
        """Drop a healed selector (e.g. it stopped working) and revert to defaults."""
        with self._lock:
            target = self._data.get("targets", {}).get(key)
            if target and target.get("healed"):
                target["healed"] = None
                self._save()

    def all_targets(self) -> dict:
        return dict(self._data.get("targets", {}))
