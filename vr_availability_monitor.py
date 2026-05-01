"""
VR.fi availability monitor for tickets after the current sales window.

Goal:
- Check whether 14 Dec 2026 is open/bookable on VR.fi for:
  Pasila -> Rovaniemi
  1 adult + 1 child
  car: 165 cm high, 500 cm long, under 3000 kg
  sleeping cabin / night train flow

Designed for GitHub Actions:
- Run once.
- If booking appears open, write alert.md.
- A later workflow step can create a GitHub Issue from alert.md, which triggers GitHub mobile push notifications.

Install:
  pip install playwright
  playwright install --with-deps chromium

Run locally:
  python vr_availability_monitor.py

Environment overrides:
  TRAVEL_DATE=2026-12-14
  FROM_STATION=Pasila
  TO_STATION=Rovaniemi
  HEADLESS=true
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright, Page

VR_URL = "https://www.vr.fi/en"
ALERT_FILE = Path("alert.md")
SCREENSHOT_FILE = Path("vr-availability-debug.png")

TRAVEL_DATE = dt.date.fromisoformat(os.getenv("TRAVEL_DATE", "2026-12-14"))
FROM_STATION = os.getenv("FROM_STATION", "Pasila")
TO_STATION = os.getenv("TO_STATION", "Rovaniemi")
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

ADULTS = 1
CHILDREN = 1
CAR_HEIGHT_CM = 165
CAR_LENGTH_CM = 500
CAR_WEIGHT_KG = 2999

BLOCKED_PATTERNS = re.compile(
    r"captcha|cloudflare|checking your browser|verify you are human|access denied|blocked",
    re.I,
)
UNAVAILABLE_PATTERNS = re.compile(
    r"not yet available|not available|sales have not started|no tickets|no journeys|choose another date|cannot be booked|not possible to book",
    re.I,
)
BOOKABLE_PATTERNS = re.compile(
    r"€|EUR|select|continue|add to cart|reserve|book|cabin|sleeping cabin|vehicle|car carrier",
    re.I,
)


def write_alert(page_url: str, evidence: str) -> None:
    ALERT_FILE.write_text(
        f"# VR tickets may now be open for {TRAVEL_DATE.isoformat()}\n\n"
        f"Route: **{FROM_STATION} → {TO_STATION}**\n\n"
        f"Passengers: **{ADULTS} adult + {CHILDREN} child**\n\n"
        f"Vehicle: **{CAR_HEIGHT_CM} cm high, {CAR_LENGTH_CM} cm long, under 3000 kg**\n\n"
        f"Cabin: **sleeping cabin / night train**\n\n"
        f"Open VR and book ASAP: {page_url}\n\n"
        f"## Evidence from monitor\n\n"
        f"```text\n{evidence[:3500]}\n```\n",
        encoding="utf-8",
    )


async def accept_cookies(page: Page) -> None:
    for text in ["Accept all", "Accept", "Allow all", "Hyväksy kaikki", "Hyväksy"]:
        try:
            await page.get_by_text(re.compile(text, re.I)).first.click(timeout=2000)
            return
        except Exception:
            pass


async def click_text(page: Page, *patterns: str, timeout: int = 3000) -> bool:
    for pattern in patterns:
        try:
            await page.get_by_text(re.compile(pattern, re.I)).first.click(timeout=timeout)
            return True
        except Exception:
            pass
    return False


async def fill_autocomplete(page: Page, label_patterns: list[str], value: str) -> bool:
    fields = []
    for label in label_patterns:
        fields.extend(
            [
                page.get_by_label(re.compile(label, re.I)).first,
                page.get_by_placeholder(re.compile(label, re.I)).first,
                page.locator(f"input[aria-label*='{label}' i]").first,
            ]
        )

    for field in fields:
        try:
            await field.click(timeout=2500)
            await field.fill(value)
            await page.wait_for_timeout(500)
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            return True
        except Exception:
            pass
    return False


async def fill_date(page: Page, date_value: dt.date) -> bool:
    # VR English UI usually accepts Finnish-style date text, but also try ISO if date inputs are used.
    date_texts = [date_value.strftime("%d.%m.%Y"), date_value.isoformat()]
    date_labels = ["Departure", "Outbound", "Date", "Lähtö", "Päivämäärä"]

    for label in date_labels:
        for field in [
            page.get_by_label(re.compile(label, re.I)).first,
            page.get_by_placeholder(re.compile(label, re.I)).first,
            page.locator(f"input[aria-label*='{label}' i]").first,
        ]:
            for value in date_texts:
                try:
                    await field.click(timeout=2500)
                    await field.fill(value)
                    await page.keyboard.press("Enter")
                    return True
                except Exception:
                    pass

    # Fallback: click date-related text and type.
    if await click_text(page, "Departure", "Outbound", "Date", "Lähtö", "Päivämäärä"):
        await page.keyboard.type(date_value.strftime("%d.%m.%Y"))
        await page.keyboard.press("Enter")
        return True

    return False


async def configure_passengers_vehicle_and_cabin(page: Page) -> None:
    # These are best-effort because VR can change the exact UI text.
    await click_text(page, "Passengers", "Passenger", "Matkustajat", timeout=2000)
    await click_text(page, "Child", "Children", "Lapsi", timeout=2000)
    await click_text(page, r"\+", "Add", "Lisää", timeout=1500)
    await click_text(page, "Done", "Apply", "Confirm", "Valmis", "Käytä", timeout=1500)

    await click_text(page, "Vehicle", "Car", "Auto", "Ajoneuvo", timeout=2500)
    await click_text(page, "Add vehicle", "Car", "Auto", "Ajoneuvo", timeout=2000)

    for label, value in [
        ("height|korkeus", str(CAR_HEIGHT_CM)),
        ("length|pituus", str(CAR_LENGTH_CM)),
        ("weight|mass|paino", str(CAR_WEIGHT_KG)),
    ]:
        try:
            await page.get_by_label(re.compile(label, re.I)).first.fill(value, timeout=1500)
        except Exception:
            pass

    await click_text(page, "Done", "Apply", "Confirm", "Save", "Valmis", "Käytä", timeout=1500)

    # If the result page asks for cabin choice, select a sleeping cabin option.
    await click_text(page, "Sleeping cabin", "Sleeper", "Cabin", "Makuu", "Hytti", timeout=2000)


async def check_availability() -> int:
    if ALERT_FILE.exists():
        ALERT_FILE.unlink()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            locale="en-GB",
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await page.goto(VR_URL, wait_until="domcontentloaded", timeout=45000)
            await accept_cookies(page)

            ok_from = await fill_autocomplete(page, ["From", "Departure station", "Mistä"], FROM_STATION)
            ok_to = await fill_autocomplete(page, ["To", "Destination", "Mihin"], TO_STATION)
            ok_date = await fill_date(page, TRAVEL_DATE)

            await configure_passengers_vehicle_and_cabin(page)

            searched = await click_text(page, "Search for journeys", "Search", "Find tickets", "Hae", timeout=5000)
            if not searched:
                raise RuntimeError("Could not find/click the search button on vr.fi")

            try:
                await page.wait_for_load_state("networkidle", timeout=45000)
            except PlaywrightTimeoutError:
                pass

            await page.wait_for_timeout(4000)
            await configure_passengers_vehicle_and_cabin(page)
            await page.screenshot(path=str(SCREENSHOT_FILE), full_page=True)

            text = await page.locator("body").inner_text(timeout=10000)
            compact_text = "\n".join(line.strip() for line in text.splitlines() if line.strip())

            if BLOCKED_PATTERNS.search(compact_text):
                print("UNKNOWN: vr.fi may have blocked or challenged the GitHub runner.")
                print(compact_text[:2000])
                return 2

            if UNAVAILABLE_PATTERNS.search(compact_text):
                print(f"CLOSED: {TRAVEL_DATE.isoformat()} does not appear bookable yet.")
                print(compact_text[:2000])
                return 0

            # Positive signal: results page has price/booking/cabin wording and station/date context.
            has_route = FROM_STATION.lower() in compact_text.lower() and TO_STATION.lower() in compact_text.lower()
            has_bookable_signal = BOOKABLE_PATTERNS.search(compact_text) is not None

            if has_route and has_bookable_signal:
                write_alert(page.url, compact_text)
                print(f"OPEN: {TRAVEL_DATE.isoformat()} may be bookable. Wrote alert.md")
                return 0

            print("UNKNOWN: Could not confidently decide availability. Check screenshot artifact.")
            print(compact_text[:2500])
            return 2

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check_availability()))
