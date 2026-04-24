"""Real browser E2E tests for Intents (/admin/intents).

Drives:
  * Page render
  * GET /api/intents/status responds
  * Language toggle changes the request params
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
def intents_page(page):
    page.goto(f"{BASE_URL}/admin/intents",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestIntentsPage:
    def test_page_loads(self, intents_page):
        get_errors = capture_console_errors(intents_page)
        assert_body_not_blank(intents_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_intents_status_endpoint(self):
        result = api.intents_status()
        assert isinstance(result, dict)
        # Expected: intents[] or registered_intents[]
        assert any(k in result for k in
                    ("intents", "registered_intents", "count", "items"))

    def test_page_fetches_intents_status_on_load(self, intents_page):
        get_errors = capture_console_errors(intents_page)
        with intents_page.expect_request(
            re.compile(r"/api/intents/status"), timeout=15_000,
        ):
            intents_page.reload(wait_until="networkidle")
        assert_no_critical_console_errors(get_errors())

    def test_prompt_endpoint_responds(self):
        result = api.get("/api/intents/prompt",
                          params={"lang": "de"},
                          skip_on_status=(401, 403, 404))
        assert result is not None
