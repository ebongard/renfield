"""Real browser E2E tests for Zweites Gehirn (/brain).

The Brain page is the unified semantic search across atoms (documents,
memories, KG entities). Drives:
  * Page render + search input
  * Backend /api/atoms list returns the expected envelope
  * Typing a query issues a request + displays results OR a clean
    empty state
  * Tier filter UI if present → query params reflect the choice
"""
from __future__ import annotations

import re

import pytest

from tests.e2e.helpers import api
from tests.e2e.helpers.asserts import (
    assert_body_not_blank,
    assert_no_critical_console_errors,
)
from tests.e2e.helpers.page import BASE_URL, capture_console_errors


pytestmark = pytest.mark.e2e


@pytest.fixture()
def brain_page(page):
    page.goto(f"{BASE_URL}/brain", wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestBrainPageRenders:
    def test_page_loads(self, brain_page):
        get_errors = capture_console_errors(brain_page)
        assert_body_not_blank(brain_page.locator("body").inner_text())
        assert brain_page.get_by_role(
            "heading", name=re.compile(r"Brain|Gehirn", re.IGNORECASE),
        ).first.is_visible()
        assert_no_critical_console_errors(get_errors())

    def test_search_input_is_present(self, brain_page):
        """A search input must exist — otherwise the page is useless."""
        search = brain_page.locator(
            "input[type='search'], input[placeholder*='uche'], "
            "input[placeholder*='earch'], textarea"
        ).first
        assert search.is_visible(), "No search input visible on /brain"

    def test_atoms_endpoint_returns_list(self):
        """Post-audit K2/circles: /api/atoms must not 500 under normal
        permissions. Empty list is fine."""
        result = api.list_atoms(limit=5)
        assert isinstance(result, list), (
            f"Expected list, got {type(result).__name__}: {result!r}"
        )


class TestBrainSearch:
    def test_empty_search_does_not_crash(self, brain_page):
        """Running a blank search must either disable the submit or
        produce a clean response — not a JS error."""
        get_errors = capture_console_errors(brain_page)
        search = brain_page.locator(
            "input[type='search'], input[placeholder*='uche'], "
            "input[placeholder*='earch'], textarea"
        ).first
        search.fill("")
        brain_page.keyboard.press("Enter")
        # Wait a moment for any request to fire
        brain_page.wait_for_timeout(1_500)
        assert_no_critical_console_errors(get_errors())

    def test_keyword_search_request_issues(self, brain_page):
        """Typing a keyword + submitting must trigger a backend call
        and the page must not crash — a 500 on /api/atoms shows up as
        a console error."""
        get_errors = capture_console_errors(brain_page)
        with brain_page.expect_request(
            re.compile(r"/api/atoms"), timeout=15_000,
        ):
            search = brain_page.locator(
                "input[type='search'], input[placeholder*='uche'], "
                "input[placeholder*='earch'], textarea"
            ).first
            search.fill("rechnung")
            brain_page.keyboard.press("Enter")
        assert_no_critical_console_errors(get_errors())
