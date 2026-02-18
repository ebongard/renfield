"""
E2E Test Fixtures — Playwright browser fixtures for renfield.local.

Provides browser, context, page, and screenshot helpers for all E2E tests.
Target: https://renfield.local (production, self-signed certs).
"""

import os
import pytest
from playwright.sync_api import sync_playwright

BASE_URL = "https://renfield.local"
SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")


@pytest.fixture(scope="session")
def _playwright_instance():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(_playwright_instance):
    browser = _playwright_instance.chromium.launch(headless=True)
    yield browser
    browser.close()


@pytest.fixture(scope="session")
def browser_context(browser):
    context = browser.new_context(ignore_https_errors=True)
    yield context
    context.close()


@pytest.fixture
def page(browser_context, request):
    """New page per test with automatic screenshot on completion."""
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    pg = browser_context.new_page()
    pg.set_default_timeout(30_000)

    yield pg

    # Screenshot after every test (pass or fail)
    test_name = request.node.name
    pg.screenshot(
        path=os.path.join(SCREENSHOTS_DIR, f"{test_name}.png"),
        full_page=True,
    )
    pg.close()


@pytest.fixture
def knowledge_page(page):
    """Navigate to /knowledge and wait for stats to load."""
    page.goto(f"{BASE_URL}/knowledge", wait_until="networkidle", timeout=15_000)
    # Wait for stats grid to render (at least one stat card with a number)
    page.wait_for_selector(".text-2xl.font-bold", timeout=10_000)
    return page


@pytest.fixture
def chat_page(page):
    """Navigate to / and wait for chat to be ready."""
    page.goto(BASE_URL, wait_until="networkidle", timeout=15_000)
    page.wait_for_selector("#chat-input", timeout=10_000)
    return page
