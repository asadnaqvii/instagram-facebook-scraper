"""Timing and human-like behavior utilities."""
import random
import asyncio
from playwright.async_api import Page
from app.config import (
    MIN_DELAY_BETWEEN_ACTIONS, MAX_DELAY_BETWEEN_ACTIONS,
    RANDOM_PAUSE_CHANCE, MOUSE_MOVEMENT_CHANCE,
)


async def wait(ms: int):
    """Sleep for ms milliseconds."""
    await asyncio.sleep(ms / 1000)


async def random_delay():
    """Wait a random amount between configured min/max."""
    ms = random.randint(MIN_DELAY_BETWEEN_ACTIONS, MAX_DELAY_BETWEEN_ACTIONS)
    await wait(ms)


async def human_like_behavior(page: Page):
    """Random mouse movements and pauses to appear human."""
    try:
        if random.random() < RANDOM_PAUSE_CHANCE:
            await wait(random.randint(1000, 3000))

        if random.random() < MOUSE_MOVEMENT_CHANCE:
            viewport = page.viewport_size
            if viewport:
                for _ in range(random.randint(1, 3)):
                    x = random.randint(50, viewport["width"] - 50)
                    y = random.randint(50, viewport["height"] - 50)
                    await page.mouse.move(x, y, steps=random.randint(3, 8))
                    await asyncio.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass
