"""Thin client for a local Ollama server.

Everything here degrades gracefully: if Ollama is unreachable or disabled,
methods return None / neutral results instead of raising, so the scraper
keeps working (falling back to heuristic selectors and skipping analysis).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import requests

from .. import config


class OllamaClient:
    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        enabled: bool | None = None,
    ):
        self.url = (url or config.OLLAMA_URL).rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self.timeout = timeout or config.OLLAMA_TIMEOUT_S
        self.enabled = config.LLM_ENABLED if enabled is None else enabled
        self._available: Optional[bool] = None  # cached availability probe

    # ── availability ─────────────────────────────────────────────────

    def is_available(self, force: bool = False) -> bool:
        """Probe /api/tags once and cache the result for the session."""
        if not self.enabled:
            return False
        if self._available is not None and not force:
            return self._available
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            self._available = r.ok
        except requests.RequestException:
            self._available = False
        return self._available

    def available_models(self) -> list[str]:
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            if r.ok:
                return [m.get("name", "") for m in r.json().get("models", [])]
        except requests.RequestException:
            pass
        return []

    # ── generation ───────────────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        temperature: float = 0.1,
        num_predict: int = 400,
        model: str | None = None,
    ) -> Optional[str]:
        """Return the model's text, or None on any failure.

        `model` overrides the default for this call (used to escalate to a
        stronger model when the fast one's output fails validation)."""
        if not self.is_available():
            return None
        try:
            r = requests.post(
                f"{self.url}/api/generate",
                json={
                    "model": model or self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": num_predict},
                },
                timeout=self.timeout,
            )
            if not r.ok:
                return None
            return r.json().get("response")
        except (requests.RequestException, ValueError):
            return None

    def generate_json(self, prompt: str, **kwargs) -> Optional[Any]:
        """Generate and parse the first JSON object/array found in the reply."""
        raw = self.generate(prompt, **kwargs)
        if not raw:
            return None
        return _first_json(raw)


def _first_json(text: str) -> Optional[Any]:
    """Extract the first well-formed JSON value from an LLM reply."""
    # Fast path.
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Strip code fences.
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # Grab the first {...} or [...] block, balanced-ish.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if 0 <= start < end:
            snippet = text[start : end + 1]
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                continue
    return None
