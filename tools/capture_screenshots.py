#!/usr/bin/env python3
"""Capture the README screenshots into docs/screenshots/.

Run against a live server (default http://127.0.0.1:8000):

    .venv/bin/python tools/capture_screenshots.py
    BASE=http://127.0.0.1:8000 .venv/bin/python tools/capture_screenshots.py

Requires the dev dependency playwright (+ `playwright install chromium`).
Images are committed launch assets — recapture after any visual change so the
public README never shows a stale UI.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots"

# (filename, route, pre-shot wait ms) — the three shots the README references.
SHOTS = [
    ("daily.png", "#daily", 2500),          # daily phrase needs the API + images
    ("workbench.png", "#workbench", 1500),
    ("byok-settings.png", "#settings", 1500),
]


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright not installed — use the dev venv: "
            "`.venv/bin/python tools/capture_screenshots.py`",
            file=sys.stderr,
        )
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    errors = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        for name, route, wait_ms in SHOTS:
            try:
                page.goto(f"{BASE}/{route}", wait_until="networkidle", timeout=30_000)
                time.sleep(wait_ms / 1000)  # fonts + photo rotation settle
                page.screenshot(path=str(OUT_DIR / name))
                print(f"✓ {name:<22} <- {route}")
            except Exception as exc:  # noqa: BLE001 — report all shots, don't die on first
                errors += 1
                print(f"✗ {name:<22} {exc}", file=sys.stderr)
        browser.close()
    if console_errors:
        print(f"warning: {len(console_errors)} console error(s) during capture", file=sys.stderr)
    print(f"saved to {OUT_DIR}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
