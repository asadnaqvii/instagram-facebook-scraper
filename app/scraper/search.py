"""Navigation helpers for Instagram and Facebook."""
import asyncio
import random
from playwright.async_api import Page
from app.utils.timing import human_like_behavior, wait


async def navigate_to_ig_profile(page: Page, username: str):
    """Navigate to an Instagram profile."""
    username = username.strip("@/")
    url = f"https://www.instagram.com/{username}/"
    print(f"  Navigating to: {url}")
    await human_like_behavior(page)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait(random.randint(2000, 4000))


async def navigate_to_ig_hashtag(page: Page, tag: str):
    """Navigate to an Instagram hashtag explore page."""
    tag = tag.lstrip("#")
    url = f"https://www.instagram.com/explore/tags/{tag}/"
    print(f"  Navigating to: {url}")
    await human_like_behavior(page)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait(random.randint(2000, 4000))


async def navigate_to_fb_page(page: Page, target: str):
    """Navigate to a Facebook page."""
    target = target.strip("@/")
    url = f"https://www.facebook.com/{target}"
    print(f"  Navigating to: {url}")
    await human_like_behavior(page)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait(random.randint(2000, 4000))


async def navigate_to_fb_reels(page: Page, target: str):
    """Navigate to a Facebook page's reels tab."""
    target = target.strip("@/")
    url = f"https://www.facebook.com/{target}/reels/"
    print(f"  Navigating to reels: {url}")
    await human_like_behavior(page)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait(random.randint(3000, 5000))
