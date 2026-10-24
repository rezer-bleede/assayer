#!/usr/bin/env python3
"""Render the Assayer SVG mark into the favicon and PWA icon set."""

from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "web" / "public"
SVG = PUBLIC / "favicon.svg"
SIZES = (16, 32, 48, 64, 128, 180, 192, 256, 512)


def main() -> None:
    svg = SVG.read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 512, "height": 512}, device_scale_factor=1)
        svg_markup = svg.replace('<svg ', '<svg width="512" height="512" ', 1)
        page.set_content(
            '<html><body style="margin:0;width:512px;height:512px">'
            + svg_markup
            + "</body></html>"
        )
        rendered = Image.open(__import__("io").BytesIO(page.locator("svg").screenshot()))
        browser.close()

    for size in SIZES:
        icon = rendered.resize((size, size), Image.Resampling.LANCZOS)
        if size == 180:
            filename = "apple-touch-icon.png"
        elif size in (192, 512):
            filename = f"android-chrome-{size}x{size}.png"
        else:
            filename = f"favicon-{size}x{size}.png"
        icon.save(PUBLIC / filename, format="PNG", optimize=True)

    rendered.save(
        PUBLIC / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )
    print(f"Generated {len(SIZES)} PNG icons and favicon.ico from {SVG.name}")


if __name__ == "__main__":
    main()
