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
import math
import random
import time

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

    async def close_page(self, page: Page) -> None:
        """Close a single tab we opened and stop tracking it.

        Used when a page dies mid-run and is replaced: without this the dead
        tab stays open in Chrome until the whole run ends, so a batch with many
        failing keywords steadily eats memory."""
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
        try:
            self._own_pages.remove(page)
        except ValueError:
            pass

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


# ── behavioural model ────────────────────────────────────────────────
#
# Uniform random delays are themselves a fingerprint: real humans produce a
# right-skewed distribution (mostly quick, occasionally very slow), vary their
# pace over a session, and don't act with millisecond-flat regularity. These
# helpers reproduce that:
#
#   * log-normal delays  — right-skewed like real inter-action timing
#   * session tempo      — each run gets a persistent "this person is fast /
#                          slow today" multiplier, so runs differ from each other
#   * fatigue drift      — pace gradually slows the longer a session runs
#   * micro-jitter       — sub-second noise so no two waits are identical
#   * attention breaks   — occasional long pauses (distraction / reading)
#   * variable scrolling — differing distances, easing, overshoot + scroll-back
#   * idle mouse motion  — humans move the pointer even when not clicking

# Per-session tempo: some people are just faster. Drawn once per process.
_SESSION_TEMPO = random.uniform(0.80, 1.35)
_SESSION_START = time.monotonic()
_ACTION_COUNT = 0


def _fatigue() -> float:
    """People slow down as a session wears on. Caps at +45%."""
    mins = (time.monotonic() - _SESSION_START) / 60.0
    return min(1.0 + mins * 0.012, 1.45)


def _lognormal_delay(lo: float, hi: float) -> float:
    """A right-skewed sample in roughly [lo, hi] — most values near the low
    end with an occasional long tail, which is how human pauses actually
    distribute. Uniform noise would look machine-made under analysis."""
    mid = (lo + hi) / 2.0
    # sigma controls the tail; mu places the median near the low-middle.
    val = random.lognormvariate(math.log(max(mid * 0.72, 0.05)), 0.42)
    # Keep it sane but allow a rare genuine outlier past `hi`.
    val = max(lo * 0.75, min(val, hi * 1.8))
    return val


async def human_delay(kind: str = "action", platform: str | None = None) -> None:
    """Pause the way a person would between actions."""
    global _ACTION_COUNT
    _ACTION_COUNT += 1

    if kind == "scroll":
        lo, hi = config.MIN_SCROLL_DELAY_S, config.MAX_SCROLL_DELAY_S
    else:
        lo, hi = config.MIN_ACTION_DELAY_S, config.MAX_ACTION_DELAY_S
    if platform == "instagram":
        lo *= config.IG_DELAY_MULTIPLIER
        hi *= config.IG_DELAY_MULTIPLIER

    delay = _lognormal_delay(lo, hi) * _SESSION_TEMPO * _fatigue()
    # Micro-jitter: never emit a suspiciously round interval.
    delay += random.uniform(-0.12, 0.28)
    delay = max(0.25, delay)

    # Attention break — glanced at something else, read a comment, replied.
    if random.random() < config.HUMAN_BREAK_CHANCE:
        delay += random.uniform(*config.HUMAN_BREAK_RANGE_S)

    # Rarely, a proper distraction (phone call, tab switch).
    if random.random() < config.HUMAN_LONG_BREAK_CHANCE:
        delay += random.uniform(*config.HUMAN_LONG_BREAK_RANGE_S)

    await asyncio.sleep(delay)


async def human_mouse(page: Page) -> None:
    """Drift the pointer a little. Real sessions have constant small mouse
    movement; a page that receives zero pointer events while scrolling for
    minutes is an obvious automation signal."""
    if random.random() > config.HUMAN_MOUSE_CHANCE:
        return
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        x = random.uniform(vp["width"] * 0.15, vp["width"] * 0.85)
        y = random.uniform(vp["height"] * 0.15, vp["height"] * 0.85)
        # steps>1 makes Playwright interpolate — a curve, not a teleport.
        await page.mouse.move(x, y, steps=random.randint(6, 18))
    except Exception:
        pass


async def scroll_page(page: Page, times: int, platform: str | None = None) -> None:
    """Scroll like a person: variable distance, easing, overshoot, re-reads."""
    for i in range(times):
        base = config.SCROLL_INCREMENT_PX
        # Distance varies a lot between flicks.
        dist = int(base * random.uniform(0.55, 1.5))

        # Split the flick into a few eased steps rather than one jump.
        steps = random.randint(2, 5)
        for sidx in range(steps):
            frac = (sidx + 1) / steps
            # ease-out: fast start, slow finish (a real flick decelerates)
            eased = 1 - (1 - frac) ** 2
            target = int(dist * eased)
            try:
                await page.evaluate(
                    "(y) => window.scrollBy({top: y, behavior: 'auto'})",
                    max(1, target // steps + random.randint(-12, 12)),
                )
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.04, 0.16))

        await human_mouse(page)
        await human_delay("scroll", platform)

        # Scroll back up a little — re-reading something that caught the eye.
        if random.random() < config.HUMAN_SCROLLBACK_CHANCE:
            try:
                await page.evaluate(
                    "(y) => window.scrollBy({top: -y, behavior: 'auto'})",
                    int(base * random.uniform(0.2, 0.6)),
                )
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.6, 2.2))

        # Pause to actually read something.
        if random.random() < 0.18:
            await asyncio.sleep(random.uniform(1.0, 3.5))


# Per-platform escalating backoff after a rate-limit hit.
_backoff_state: dict[str, float] = {}


async def detect_and_handle_rate_limit(page: Page, platform: str, progress=None) -> bool:
    """Check whether the current page is a 429 / rate-limit / 'try again later'
    wall. If so, pause (escalating backoff) and return True so the caller can
    skip or retry. Returns False when the page looks normal.

    This is what stops the scraper from making throttling worse: when Meta
    says 'slow down', we actually stop for a while instead of hammering on.
    """
    try:
        info = await page.evaluate(
            r"""() => {
                const t = (document.body ? document.body.innerText : '').slice(0, 400);
                const title = document.title || '';
                const rl = /429|too many requests|rate.?limit|please wait a few minutes|try again later|temporarily blocked|we limit how often/i;
                return { hit: rl.test(t) || rl.test(title), url: location.href };
            }"""
        )
    except Exception:
        return False
    if not info or not info.get("hit"):
        _backoff_state[platform] = config.RATE_LIMIT_BACKOFF_S  # reset on healthy page
        return False

    wait = _backoff_state.get(platform, config.RATE_LIMIT_BACKOFF_S)
    msg = f"[{platform}] RATE LIMITED (429) — backing off {int(wait)}s before continuing"
    if progress:
        progress(msg)
    else:
        print(msg)
    await asyncio.sleep(wait)
    # Escalate for next time, capped.
    _backoff_state[platform] = min(wait * 2, config.RATE_LIMIT_MAX_BACKOFF_S)
    return True
