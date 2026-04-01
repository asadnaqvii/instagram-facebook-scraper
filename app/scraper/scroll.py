"""Sequential page scrolling — small increments to not miss content."""
import random
import asyncio
from playwright.async_api import Page
from app.config import SCROLL_DELAY, MAX_SCROLLS, SCROLL_INCREMENT, MIN_SCROLL_DELAY, MAX_SCROLL_DELAY
from app.utils.timing import wait


async def scroll_until_no_new_content(
    page: Page,
    max_scrolls: int = MAX_SCROLLS,
    scroll_delay: int = SCROLL_DELAY,
    stability_threshold: int = 3,
) -> int:
    """Sequential scroll from current position; stops when page height stabilises."""
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)

    last_height = await page.evaluate("document.body.scrollHeight")
    scroll_count = 0
    stable_count = 0

    while scroll_count < max_scrolls:
        current_height = await page.evaluate("document.body.scrollHeight")
        viewport_height = await page.evaluate("window.innerHeight")
        scroll_pos = await page.evaluate("window.pageYOffset || window.scrollY")
        at_bottom = (scroll_pos + viewport_height) >= (current_height - 50)

        if at_bottom:
            await asyncio.sleep(0.5)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == current_height:
                stable_count += 1
                if stable_count >= stability_threshold:
                    break
            else:
                stable_count = 0
                last_height = new_height

        await page.evaluate(f"window.scrollBy(0, {SCROLL_INCREMENT})")
        scroll_count += 1

        actual_delay = scroll_delay + random.randint(-200, 200)
        actual_delay = max(500, actual_delay)
        await wait(actual_delay)

        if scroll_count % 5 == 0:
            pct = int((scroll_pos / max(current_height, 1)) * 100)
            print(f"    Scrolled {scroll_count}x ({pct}% down)")

    return scroll_count
