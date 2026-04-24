"""Real browser E2E tests for Satellites (/admin/satellites).

Drives:
  * Page render
  * Satellites list from /api/satellites
  * Versions endpoint returns version info
  * Page fetches satellites on load
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
def satellites_page(page):
    page.goto(f"{BASE_URL}/admin/satellites",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestSatellitesPage:
    def test_page_loads(self, satellites_page):
        get_errors = capture_console_errors(satellites_page)
        assert_body_not_blank(satellites_page.locator("body").inner_text())
        assert satellites_page.get_by_role(
            "heading", name=re.compile(r"Satellite|Satelliten", re.IGNORECASE),
        ).first.is_visible()
        assert_no_critical_console_errors(get_errors())

    def test_satellites_endpoint_returns_envelope(self):
        result = api.list_satellites()
        assert result is not None
        items = result.get("satellites") if isinstance(result, dict) else result
        assert isinstance(items, list)

    def test_versions_endpoint_responds(self):
        result = api.get("/api/satellites/versions",
                          skip_on_status=(401, 403, 404))
        assert isinstance(result, dict)

    def test_page_fetches_satellites_on_load(self, satellites_page):
        get_errors = capture_console_errors(satellites_page)
        with satellites_page.expect_request(
            re.compile(r"/api/satellites"), timeout=15_000,
        ):
            satellites_page.reload(wait_until="networkidle")
        assert_no_critical_console_errors(get_errors())
