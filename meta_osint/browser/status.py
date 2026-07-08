"""Live connection + login status for each platform.

Two things matter before a scrape will work:
  1. Is the platform's Chrome running and reachable over CDP?
  2. Is that Chrome session actually logged in?

This module answers both, quickly, so the dashboard can show a clear
"Instagram: logged in / not logged in / Chrome not running" panel — the
thing the old app had and users relied on.
"""
from __future__ import annotations

import asyncio
import socket

from .. import config


def _port_open(port: int, timeout: float = 1.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


async def _check_login(platform: str) -> tuple[bool, str | None]:
    """Attach to the platform's Chrome and check whether it's logged in.

    Returns (logged_in, username_or_none). Any connection failure returns
    (False, None) — the caller distinguishes "chrome down" via the port check.
    """
    from .manager import BrowserManager, CDPConnectionError

    try:
        async with BrowserManager(platform) as bm:
            page = await bm.new_page()
            if platform == "instagram":
                from ..scraper.instagram import check_login
                await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
                logged = await check_login(page)
                who = None
                if logged:
                    try:
                        who = await page.evaluate(
                            r"""() => {
                                for (const a of document.querySelectorAll('a[href^="/"]')) {
                                    const m = (a.getAttribute('href')||'').match(/^\/([A-Za-z0-9._]+)\/$/);
                                    if (m && a.querySelector('img')) return m[1];
                                }
                                return null;
                            }"""
                        )
                    except Exception:
                        pass
                return logged, who
            else:  # facebook
                await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
                url = (page.url or "").lower()
                if "login" in url:
                    return False, None
                # Logged-out FB shows the big email+password login form.
                has_login_form = await page.evaluate(
                    """() => !!document.querySelector('input[name="email"]') &&
                             !!document.querySelector('input[name="pass"]')"""
                )
                return (not has_login_form), None
    except CDPConnectionError:
        return False, None
    except Exception:
        return False, None


async def get_status_async() -> dict:
    """Full status for both platforms."""
    out: dict = {}
    for platform in config.PLATFORMS:
        port = config.CDP_PORT_INSTAGRAM if platform == "instagram" else config.CDP_PORT_FACEBOOK
        chrome_up = _port_open(port)
        logged_in, who = (False, None)
        if chrome_up:
            logged_in, who = await _check_login(platform)
        if not chrome_up:
            state = "chrome_down"
        elif logged_in:
            state = "logged_in"
        else:
            state = "not_logged_in"
        out[platform] = {
            "platform": platform,
            "port": port,
            "chrome_up": chrome_up,
            "logged_in": logged_in,
            "username": who,
            "state": state,
        }
    return out


def get_status() -> dict:
    """Sync wrapper for Flask."""
    return asyncio.run(get_status_async())
