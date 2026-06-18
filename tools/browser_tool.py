import asyncio
import logging
from playwright.async_api import async_playwright
from config import CDP_URL

logger = logging.getLogger(__name__)

class CDPConnectionManager:
    def __init__(self, cdp_url=CDP_URL):
        self.cdp_url = cdp_url
        self.playwright = None
        self.browser = None
        self.context = None

    async def __aenter__(self):
        logger.info(f"Connecting to Chrome CDP at {self.cdp_url}...")
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
            # Fetch the default persistent context
            self.context = self.browser.contexts[0]
            logger.info("CDP attachment successful.")
            return self.context
        except Exception as e:
            logger.error(f"Failed to connect over CDP: {e}")
            await self.playwright.stop()
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # We must NOT close the browser (browser.close()) because it will shut down
        # the host's Chrome instance. Just stop the Playwright connection/driver.
        logger.info("Closing CDP WebSocket connection...")
        try:
            await self.playwright.stop()
            logger.info("CDP WebSocket connection closed successfully.")
        except Exception as e:
            logger.error(f"Error while disconnecting from CDP: {e}")

async def scrape_finance_news(url: str) -> dict:
    """
    Control host Chrome over CDP to scrape news safely without triggering anti-bot.
    Cleans up the page tab immediately after scraping to prevent tab leaks.
    """
    async with CDPConnectionManager() as context:
        page = await context.new_page()
        try:
            logger.info(f"Navigating to: {url}")
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1.5)  # Evasion sleep
            
            title = await page.title()
            # Extract main visible text
            content = await page.evaluate("() => document.body.innerText")
            
            logger.info(f"Successfully scraped '{title}' (length: {len(content)} chars)")
            return {"title": title, "content": content}
        except Exception as e:
            logger.error(f"Failed to scrape site {url}: {e}")
            raise RuntimeError(f"CDP Scraping failed: {e}")
        finally:
            # Crucial: Close the tab page so we don't leak open tabs in the host Chrome
            logger.info("Closing scraped tab...")
            await page.close()
