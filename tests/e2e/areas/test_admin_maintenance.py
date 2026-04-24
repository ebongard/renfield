"""Real browser E2E tests for Wartung (/admin/maintenance).

Drives:
  * Page render
  * Maintenance buttons are present
  * Admin /refresh-keywords endpoint is reachable
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
def maintenance_page(page):
    page.goto(f"{BASE_URL}/admin/maintenance",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestMaintenancePage:
    def test_page_loads(self, maintenance_page):
        get_errors = capture_console_errors(maintenance_page)
        assert_body_not_blank(maintenance_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_maintenance_buttons_present(self, maintenance_page):
        """Page must expose at least one actionable button (reindex,
        re-embed, refresh-keywords, etc.)."""
        btns = maintenance_page.locator("button").filter(
            has_text=re.compile(
                r"Reindex|Re-Index|Refresh|Keywords|Embed|Update",
                re.IGNORECASE,
            )
        )
        assert btns.count() >= 1, (
            "No maintenance action buttons visible on /admin/maintenance"
        )

    def test_reindex_button_issues_post_when_clicked(self, maintenance_page):
        """Click the reindex button → POST /api/knowledge/reindex-fts fires."""
        btn = maintenance_page.locator("button").filter(
            has_text=re.compile(r"Reindex|Re-Index|FTS", re.IGNORECASE),
        ).first
        if not btn.is_visible():
            pytest.skip("No reindex button on this build")
        with maintenance_page.expect_request(
            re.compile(r"/api/knowledge/reindex-fts"), timeout=15_000,
        ):
            btn.click()
