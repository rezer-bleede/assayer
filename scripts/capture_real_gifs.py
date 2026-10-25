import asyncio
from playwright.async_api import async_playwright
import imageio
import PIL.Image as Image
import os

async def capture_app_screens():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        page = await context.new_page()

        await page.goto("http://localhost:3000")
        await page.wait_for_timeout(2000)

        # Take initial screenshot of empty shell
        await page.screenshot(path="/tmp/screen_init.png")
        print("Captured initial screen")

        await browser.close()

asyncio.run(capture_app_screens())
