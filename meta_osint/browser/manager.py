"""CDP browser manager.

Connects to a Chrome you launched yourself with --remote-debugging-port
(see meta_osint/scripts/start_chrome_*.bat). We deliberately do NOT launch
Chrome — attaching to your own logged-in, human-warmed browser session is
what lets us read authenticated pages and stay under Meta's bot radar. A
freshly automated Chromium gets login-walled almost immediately, which is
exactly why the earlier DOM scraper produced empty output.
"""
from __future__ import annotations

import asyncio
import random

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .. import config


class CDPConnectionError(RuntimeError):
    """Raised when we cannot reach the CDP endpoint (Chrome not running)."""


class BrowserManager:
    def __init__(self, platform: str):
        if platform not in config.PLATFORMS:
            raise ValueError(f"Unknown platform: {platform}")
        self.platform = platform
        self.endpoint = config.cdp_endpoint(platform)
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self._own_pages: list[Page] = []

    async def start(self) -> "BrowserManager":
        self._pw = await async_playwright().start()
        try:
            self.browser = await self._pw.chromium.connect_over_cdp(self.endpoint)
        except Exception as e:  # noqa: BLE001 — surface a clear, actionable error
            await self._cleanup_pw()
            raise CDPConnectionError(
                f"Could not connect to Chrome for {self.platform} at {self.endpoint}.\n"
                f"Start it first:\n"
                f"  Windows: meta_osint\\scripts\\start_chrome_{self.platform}.bat\n"
                f"  Linux/macOS: meta_osint/scripts/start_chrome_{self.platform}.sh\n"
                f"Then log in to the site in that window and re-run.\n"
                f"(underlying error: {e})"
            ) from e

        contexts = self.browser.contexts
        self.context = contexts[0] if contexts else await self.browser.new_context(
            user_agent=config.USER_AGENT
        )
        self.context.set_default_timeout(config.BROWSER_TIMEOUT_MS)
        return self

    async def new_page(self) -> Page:
        """Open a fresh tab we own (so closing it won't disturb the user's tabs)."""
        assert self.context is not None, "call start() first"
        page = await self.context.new_page()
        self._own_pages.append(page)
        return page

    async def close(self) -> None:
        # Close only the tabs we opened; leave the user's browser running.
        for page in self._own_pages:
            try:
                await page.close()
            except Exception:
                pass
        self._own_pages.clear()
        if self.browser is not None:
            try:
                await self.browser.close()  # detaches CDP; Chrome stays open
            except Exception:
                pass
            self.browser = None
        await self._cleanup_pw()

    async def _cleanup_pw(self) -> None:
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def __aenter__(self) -> "BrowserManager":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.close()


# ── human-like helpers usable from any extractor ─────────────────────


async def human_delay(kind: str = "action") -> None:
    if kind == "scroll":
        lo, hi = config.MIN_SCROLL_DELAY_S, config.MAX_SCROLL_DELAY_S
    else:
        lo, hi = config.MIN_ACTION_DELAY_S, config.MAX_ACTION_DELAY_S
    await asyncio.sleep(random.uniform(lo, hi))


async def scroll_page(page: Page, times: int) -> None:
    """Scroll down `times` steps with human-ish pauses and occasional pauses."""
    for i in range(times):
        await page.evaluate(f"window.scrollBy(0, {config.SCROLL_INCREMENT_PX})")
        await human_delay("scroll")
        # Occasional longer pause, as a person skimming would.
        if random.random() < 0.15:
            await asyncio.sleep(random.uniform(1.0, 2.5))
