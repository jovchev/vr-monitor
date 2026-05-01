"""
Simple VR availability monitor using direct search URL.

Checks if tickets are available for:
Pasila -> Rovaniemi
Date: 14 Dec 2026
1 adult + 1 child + car + night train bed cabin prices

If available -> writes alert.md
If unavailable -> exits normally without alert.md

Designed for GitHub Actions or local use.
"""

import asyncio
import os
import re
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SEARCH_URL = os.getenv(
    "SEARCH_URL",
    "https://www.vr.fi/en/single-ticket-outbound-search-results"
    "?from=PSL&to=ROI"
    "&passengers[0][key]=78bf5506-7e85-4767-a98f-2de2fd6412f4"
    "&passengers[0][type]=ADULT"
    "&passengers[0][vehicle][vehicleType]=CAR"
    "&passengers[0][vehicle][height]=165"
    "&passengers[0][vehicle][length]=500"
    "&passengers[0][vehicle][key]=921c9846-849e-4e77-9627-d19d4029f372"
    "&passengers[0][vehicle][weight]=2999"
    "&passengers[1][key]=806ed19c-6cbe-4e99-860d-d53c0d6d604c"
    "&passengers[1][type]=CHILD"
    "&scope=ONLY_BED_CABINS"
    "&outboundDate=2026-12-14",
)

HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
ALERT_FILE = Path("alert.md")
SCREENSHOT = Path("debug.png")
PAGE_TEXT = Path("page-text.txt")

BLOCKED = re.compile(r"captcha|cloudflare|verify you are human|access denied|blocked", re.I)

CLOSED_MARKERS = [
    "Couldn't find any connections",
    "Could not find any connections",
    "Unfortunately we couldn't find any matches",
]

# Match the actual positive state from your screenshot:
# - a visible journey card with "Night train 265" or similar
# - or a visible ticket price like €478.00
OPEN_REGEX = re.compile(
    r"night\s+train\s+\d+|"
    r"\bintercity\s+\d+\b|"
    r"\bpendolino\s+\d+\b|"
    r"€\s*\d+[,.]?\d*|"
    r"\d+[,.]\d{2}\s*€",
    re.I,
)


def write_alert(text: str) -> None:
    ALERT_FILE.write_text(
        "# VR tickets appear to be available\n\n"
        "The monitor found a journey/price on VR.fi. Book ASAP.\n\n"
        f"URL:\n{SEARCH_URL}\n\n"
        "## Page evidence\n\n"
        "```text\n"
        f"{text[:3000]}\n"
        "```\n",
        encoding="utf-8",
    )


async def visible_text_count(page, text: str) -> int:
    try:
        return await page.get_by_text(text, exact=False).count()
    except Exception:
        return 0


async def run() -> int:
    if ALERT_FILE.exists():
        ALERT_FILE.unlink()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, slow_mo=200)
        page = await browser.new_page(viewport={"width": 1500, "height": 950})

        try:
            print(f"Opening URL: {SEARCH_URL}", flush=True)
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)

            # Wait until either the no-connections card or a journey/price has time to render.
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                print("Timed out waiting for networkidle; continuing", flush=True)

            await page.wait_for_timeout(5000)
            await page.screenshot(path=str(SCREENSHOT), full_page=True)

            text = await page.locator("body").inner_text(timeout=10000)
            PAGE_TEXT.write_text(text, encoding="utf-8")
            text_lower = text.lower()

            print("--- Page text preview ---", flush=True)
            print(text[:2000], flush=True)
            print("--- End preview ---", flush=True)

            if BLOCKED.search(text):
                print("UNKNOWN: Blocked by VR / Cloudflare / verification page", flush=True)
                return 2

            closed_count = 0
            for marker in CLOSED_MARKERS:
                count = await visible_text_count(page, marker)
                closed_count += count
                print(f"Closed marker '{marker}': {count}", flush=True)

            open_match = OPEN_REGEX.search(text)
            print(f"Open regex match: {open_match.group(0) if open_match else 'none'}", flush=True)

            # Important: the no-connections page may contain generic sales-window text,
            # so CLOSED wins unless we also have a real journey/price signal.
            if open_match:
                print("OPEN: Journey/price found. Creating alert.md", flush=True)
                write_alert(text)
                return 0

            if closed_count > 0:
                print("CLOSED: No connections found / not bookable yet", flush=True)
                return 0

            print("UNKNOWN: Neither journey/price nor no-connections marker detected", flush=True)
            print(f"Saved screenshot: {SCREENSHOT}", flush=True)
            print(f"Saved page text: {PAGE_TEXT}", flush=True)
            return 2

        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
