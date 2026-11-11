"""Screenshots of the Warsztat UI for the documentation.

Run it against a stack filled with INVENTED data, never against your real
inventory (the screenshots end up in a public repository):

    pip install playwright && playwright install chromium
    python tools/screenshots.py http://localhost:8091 docs/img
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8091"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/img")
OUT.mkdir(parents=True, exist_ok=True)

# Headless Chromium has no colour emoji font; fall back to a monochrome one
# if it is installed (Noto Emoji), instead of drawing empty boxes.
EMOJI_CSS = (
    "@font-face{font-family:EmojiFB;src:local('Noto Emoji');"
    "unicode-range:U+1F000-1FAFF,U+2600-27BF,U+2190-21FF,U+23E9-23FA,U+2B00-2BFF,U+FE0F}"
    "body,button,input,textarea{font-family:inherit,EmojiFB}"
)


def part_id(name_fragment: str) -> int:
    with urllib.request.urlopen(f"{BASE}/api/warsztat/snapshot") as response:
        snapshot = json.load(response)
    rows = snapshot.get("parts") or snapshot.get("rows") or []
    for row in rows:
        if name_fragment.casefold() in str(row.get("name", "")).casefold():
            return int(row["id"])
    raise SystemExit(f"no part containing {name_fragment!r} in the snapshot")


def shoot(page, url: str, name: str, *, wait: str | None = None, full: bool = False) -> None:
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.add_style_tag(content=EMOJI_CSS)
    if wait:
        page.wait_for_selector(wait, timeout=10_000)
    page.wait_for_timeout(900)
    if full:
        # full_page=True would paste the sticky tab bar into the middle of the
        # picture; make the window as tall as the content instead.
        height = page.evaluate("document.documentElement.scrollHeight")
        page.set_viewport_size({"width": page.viewport_size["width"], "height": min(height, 2400)})
        page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", OUT / f"{name}.png")


def main() -> None:
    esp = part_id("ESP32")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                    is_mobile=True, has_touch=True, locale="pl-PL")
        page = phone.new_page()
        shoot(page, f"{BASE}/#szukaj", "phone-search")
        page.fill("input[type=search], input[placeholder^='Czego']", "przeka")
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "phone-search-results.png"))
        print("saved", OUT / "phone-search-results.png")
        shoot(page, f"{BASE}/#przegladaj", "phone-browse")
        shoot(page, f"{BASE}/#listy", "phone-lists")
        shoot(page, f"{BASE}/#dodaj", "phone-add", full=True)
        page.set_viewport_size({"width": 390, "height": 844})
        shoot(page, f"{BASE}/#c/{esp}", "phone-part-card", full=True)
        page.set_viewport_size({"width": 390, "height": 844})

        desk = browser.new_context(viewport={"width": 1280, "height": 860}, device_scale_factor=1, locale="pl-PL")
        page = desk.new_page()
        shoot(page, f"{BASE}/#c/{esp}", "desktop-part-card")
        shoot(page, f"{BASE}/#dodaj", "desktop-add")
        fresh = browser.new_context(viewport={"width": 1280, "height": 860}, locale="pl-PL").new_page()
        shoot(fresh, f"{BASE}/#przegladaj", "desktop-browse")
        browser.close()


if __name__ == "__main__":
    main()
