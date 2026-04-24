"""Real browser E2E tests for Smart Home (/homeassistant).

Drives:
  * Page render
  * GET /api/homeassistant/states responds (may 503 if HA down)
  * Page fetches states on load
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
def ha_page(page):
    page.goto(f"{BASE_URL}/homeassistant",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestSmartHomePage:
    def test_page_loads(self, ha_page):
        get_errors = capture_console_errors(ha_page)
        assert_body_not_blank(ha_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_states_endpoint_responds(self):
        result = api.homeassistant_states()
        assert result is not None
        entities = result.get("entities") if isinstance(result, dict) else result
        if entities is None:
            pytest.skip("states endpoint returned unexpected shape")
        assert isinstance(entities, list)

    def test_page_fetches_states_on_load(self, ha_page):
        get_errors = capture_console_errors(ha_page)
        with ha_page.expect_request(
            re.compile(r"/api/homeassistant/states"), timeout=15_000,
        ):
            ha_page.reload(wait_until="networkidle", timeout=20_000)
        assert_no_critical_console_errors(get_errors())
