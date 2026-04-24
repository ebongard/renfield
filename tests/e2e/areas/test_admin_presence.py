"""Real browser E2E tests for Anwesenheit (/admin/presence).

Drives:
  * Page render
  * Status + rooms + devices endpoints respond
  * Page fetches presence state on load
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
def presence_page(page):
    page.goto(f"{BASE_URL}/admin/presence",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestPresencePage:
    def test_page_loads(self, presence_page):
        get_errors = capture_console_errors(presence_page)
        assert_body_not_blank(presence_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_status_endpoint(self):
        result = api.presence_status()
        assert isinstance(result, dict)

    def test_rooms_endpoint_returns_list(self):
        result = api.presence_rooms()
        assert isinstance(result, list)

    def test_devices_endpoint_returns_list(self):
        result = api.get("/api/presence/devices",
                          skip_on_status=(401, 403, 404))
        assert isinstance(result, list)

    def test_page_fetches_rooms_on_load(self, presence_page):
        get_errors = capture_console_errors(presence_page)
        with presence_page.expect_request(
            re.compile(r"/api/presence/rooms"), timeout=15_000,
        ):
            presence_page.reload(wait_until="networkidle")
        assert_no_critical_console_errors(get_errors())
