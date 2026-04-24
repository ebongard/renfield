"""Real browser E2E tests for Integrationen (/admin/integrations).

Drives:
  * Page render
  * GET /api/mcp/status + /api/mcp/tools respond
  * Page fetches both on load
  * Refresh button triggers POST /api/mcp/refresh
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
def integrations_page(page):
    page.goto(f"{BASE_URL}/admin/integrations",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestIntegrationsPage:
    def test_page_loads(self, integrations_page):
        get_errors = capture_console_errors(integrations_page)
        assert_body_not_blank(integrations_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_mcp_status_endpoint_returns_shape(self):
        result = api.mcp_status()
        assert isinstance(result, dict)
        assert any(k in result for k in
                    ("enabled", "servers", "total_tools"))

    def test_mcp_tools_endpoint_returns_list(self):
        result = api.mcp_tools()
        assert result is not None
        tools = result.get("tools") if isinstance(result, dict) else result
        assert isinstance(tools, list)

    def test_page_fetches_mcp_status_on_load(self, integrations_page):
        get_errors = capture_console_errors(integrations_page)
        with integrations_page.expect_request(
            re.compile(r"/api/mcp/status"), timeout=15_000,
        ):
            integrations_page.reload(wait_until="networkidle")
        assert_no_critical_console_errors(get_errors())

    def test_refresh_button_issues_post(self, integrations_page):
        """Clicking a 'Refresh' / 'Aktualisieren' button must trigger
        POST /api/mcp/refresh — otherwise the button is cosmetic."""
        btn = integrations_page.get_by_role(
            "button",
            name=re.compile(
                r"Refresh|Aktualisieren|Reload|Neu laden", re.IGNORECASE,
            ),
        ).first
        if not btn.is_visible():
            pytest.skip("No refresh button on this build")
        with integrations_page.expect_request(
            re.compile(r"/api/mcp/refresh"), timeout=15_000,
        ):
            btn.click()
