import asyncio
from playwright.async_api import async_playwright

async def dump_ui():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1280, 'height': 800})
        await page.goto("http://localhost:3000")
        await page.wait_for_timeout(2000)
        content = await page.content()
        print("Page HTML length:", len(content))
        print("Body text snippet:", (await page.text_content("body"))[:300])
        await browser.close()

asyncio.run(dump_ui())
