"""Tar en skärmdump av en sida: python -m sajtvakt_server.shoot URL UT.jpg

Körs som en egen process, så att en webbläsare som hänger eller kraschar aldrig
påverkar resten av Sajtvakt. Bilden blir 640 × 400 px (sidan visas som på en
1280 px bred skärm, i halv upplösning)."""
from __future__ import annotations

import sys

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36 Sajtvakt-skarmdump")


def main(url: str, out: str) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-dev-shm-usage", "--disable-gpu", "--hide-scrollbars", "--mute-audio"])
        try:
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800}, device_scale_factor=0.5, locale="sv-SE",
                user_agent=UA, ignore_https_errors=True, reduced_motion="reduce", service_workers="block",
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="load", timeout=45_000)
            except PlaywrightError:
                pass  # Ta det som hunnit visas – en långsam sida är också information.
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightError:
                pass
            page.wait_for_timeout(1_500)
            page.screenshot(path=out, type="jpeg", quality=74, animations="disabled")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Användning: python -m sajtvakt_server.shoot URL UT.jpg", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
