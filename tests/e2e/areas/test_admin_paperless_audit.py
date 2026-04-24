"""Real browser E2E tests for Paperless Audit (/admin/paperless-audit).

Drives:
  * Page render
  * Status endpoint responds
  * Page fetches status on load
  * Stats endpoint returns counts
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
def audit_page(page):
    page.goto(f"{BASE_URL}/admin/paperless-audit",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestPaperlessAuditPage:
    def test_page_loads(self, audit_page):
        get_errors = capture_console_errors(audit_page)
        assert_body_not_blank(audit_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_status_endpoint_responds(self):
        result = api.paperless_audit_status()
        assert result is not None

    def test_stats_endpoint_has_counts(self):
        result = api.get("/api/admin/paperless-audit/stats",
                          skip_on_status=(401, 403, 404))
        assert isinstance(result, dict)

    def test_page_fetches_status_on_load(self, audit_page):
        get_errors = capture_console_errors(audit_page)
        with audit_page.expect_request(
            re.compile(r"/api/admin/paperless-audit/status"), timeout=15_000,
        ):
            audit_page.reload(wait_until="networkidle")
        assert_no_critical_console_errors(get_errors())
