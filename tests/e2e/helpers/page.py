"""Page-level helpers: go-to-area, wait-for-ready, console-error capture.

Intent: let a per-area test read as "navigate, check the page is
actually rendered, assert no critical console errors" without each
test reimplementing the same timing + selector scaffolding.
"""
from __future__ import annotations

import os
from typing import Callable

from playwright.sync_api import Page

from tests.e2e.helpers.routes import Area

BASE_URL = "https://renfield.local"


def goto_area(page: Page, area: Area, *, timeout_ms: int = 15_000) -> None:
    """Navigate to `area.path` and wait for its ready-selector.

    `ready_selector` is a CSS selector (may be a comma-separated list of
    alternatives). We wait for the FIRST match with visibility — if none
    of the alternatives show up, Playwright raises, which surfaces as
    a clean test failure "area didn't render".
    """
    url = f"{BASE_URL}{area.path}"
    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    page.wait_for_selector(area.ready_selector, state="visible", timeout=timeout_ms)


def capture_console_errors(page: Page) -> Callable[[], list[str]]:
    """Start collecting console errors from this page.

    Returns a zero-arg callable that, when invoked, returns the list of
    error messages captured so far (no reset). Use after navigation so
    bootstrap chatter doesn't pollute the assertion surface.

    Caller filters for "critical" errors — some noise (favicons,
    third-party analytics, service-worker cert errors on self-signed
    installs) is expected and fine.
    """
    errors: list[str] = []

    def _on_console(msg) -> None:
        if msg.type == "error":
            errors.append(msg.text)

    page.on("console", _on_console)
    return lambda: list(errors)  # defensive copy


def screenshot_for(page: Page, directory: str, name: str) -> str:
    """Save a full-page PNG to `directory/<name>.png`. Returns the path."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    return path
