"""Browser management — connects to existing Chrome via CDP (like Twitter scraper)."""
import asyncio
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
from app.config import BROWSER_TIMEOUT


class BrowserManager:
    """Connects to an already-running Chrome browser via CDP.

    The user must:
    1. Run start_chrome_debug.bat to launch Chrome with --remote-debugging-port=9222
    2. Log into Instagram/Facebook manually in that Chrome window
    3. Then scrapes will use that logged-in session automatically
    """

    def __init__(self, cdp_endpoint: str = "http://localhost:9222"):
        self.cdp_endpoint = cdp_endpoint
        self.playwright: Playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None

    async def start(self):
        """Connect to existing Chrome browser via CDP."""
        self.playwright = await async_playwright().start()

        print(f"  Connecting to Chrome at {self.cdp_endpoint}...")
        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_endpoint)

            # Use the existing browser context (with all cookies/sessions)
            contexts = self.browser.contexts
            if contexts:
                self.context = contexts[0]
                print(f"  Connected to existing browser context")
            else:
                self.context = await self.browser.new_context()
                print("  Created new context in existing browser")

            # Use first page or create new one
            pages = self.context.pages
            if pages:
                self.page = pages[0]
                print(f"  Using existing page: {self.page.url}")
            else:
                self.page = await self.context.new_page()
                print("  Created new page")

            self.context.set_default_timeout(BROWSER_TIMEOUT)

        except Exception as e:
            print(f"  ERROR: Could not connect to Chrome: {e}")
            print()
            print("  Make sure Chrome is running with remote debugging:")
            print("    Run: start_chrome_debug.bat")
            print()
            print("  Or manually:")
            print('    chrome.exe --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\Google\\Chrome-MetaScraper\\User Data"')
            raise

    async def new_page(self) -> Page:
        """Create a new tab/page in the existing browser."""
        return await self.context.new_page()

    async def close(self):
        """Disconnect from browser (browser stays open)."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()
