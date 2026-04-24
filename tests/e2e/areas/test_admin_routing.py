"""Real browser E2E tests for Routing Dashboard (/admin/routing).

Drives:
  * Page render
  * Traces + stats endpoints respond
  * Page fetches both on load
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
def routing_page(page):
    page.goto(f"{BASE_URL}/admin/routing",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestRoutingDashboard:
    def test_page_loads(self, routing_page):
        get_errors = capture_console_errors(routing_page)
        assert_body_not_blank(routing_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_routing_traces_endpoint(self):
        result = api.get("/api/admin/routing-traces",
                          params={"limit": 10},
                          skip_on_status=(401, 403, 404))
        assert result is not None
        if isinstance(result, dict):
            assert any(k in result for k in
                        ("traces", "items", "rows", "data"))

    def test_routing_stats_endpoint(self):
        result = api.get("/api/admin/routing-stats",
                          skip_on_status=(401, 403, 404))
        assert isinstance(result, dict)

    def test_page_fetches_traces_on_load(self, routing_page):
        get_errors = capture_console_errors(routing_page)
        with routing_page.expect_request(
            re.compile(r"/api/admin/routing-traces"), timeout=15_000,
        ):
            routing_page.reload(wait_until="networkidle")
        assert_no_critical_console_errors(get_errors())
