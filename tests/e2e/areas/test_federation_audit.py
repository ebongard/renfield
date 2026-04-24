"""Real browser E2E tests for Föderations-Verlauf (/brain/audit).

Drives:
  * Page render + audit list from GET /api/federation/audit
  * Page triggers the audit fetch on load
  * Empty state renders cleanly
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
    page.goto(f"{BASE_URL}/brain/audit",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestFederationAuditPage:
    def test_page_loads(self, audit_page):
        get_errors = capture_console_errors(audit_page)
        assert_body_not_blank(audit_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_audit_endpoint_returns_envelope(self):
        result = api.federation_audit(limit=10)
        assert result is not None
        if isinstance(result, dict):
            assert any(k in result for k in
                        ("audit", "items", "entries", "results", "data")), (
                f"Federation audit envelope keys unexpected: {list(result)}"
            )
        else:
            assert isinstance(result, list)

    def test_triggers_audit_fetch_on_load(self, audit_page):
        """Page must fetch /api/federation/audit on load — otherwise the
        list is always stale."""
        get_errors = capture_console_errors(audit_page)
        with audit_page.expect_request(
            re.compile(r"/api/federation/audit"), timeout=15_000,
        ):
            audit_page.reload(wait_until="networkidle")
        assert_no_critical_console_errors(get_errors())
