import asyncio
from playwright.async_api import async_playwright
import PIL.Image as Image
import imageio
import os

async def capture_real_app_components():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1024, 'height': 680}, device_scale_factor=2)
        page = await context.new_page()

        # Navigate to dev-preview route
        await page.goto("http://localhost:3000/dev-preview", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        # Capture screenshots of PlanCard and ResultsCard
        plan_card = page.locator(".surface").nth(0)
        results_card = page.locator(".surface").nth(1)

        await page.screenshot(path="/tmp/full_app_real.png")
        await plan_card.screenshot(path="/tmp/plan_card_real.png")
        await results_card.screenshot(path="/tmp/results_card_real.png")

        print("Captured real component screenshots!")
        await browser.close()

asyncio.run(capture_real_app_components())
