"""Comprehensive functional tests for Satellites (/admin/satellites).

TODO: flesh out to cover every interactive flow exposed by this page.
Today it carries a single "page actually renders" guard so the suite
stays green while additional coverage lands area by area. Follow the
pattern in test_chat.py / test_knowledge.py — drive the UI action AND
assert the downstream backend state (DB row, MCP call, Paperless state,
etc.). A pure DOM-render check is NOT enough — it misses the class of
bug that shipped in PR #464 and PR #467 where the UI looked correct
but the backend landed in the wrong state.

Specifically needs:
  - Satellite list matches /api/satellites
    - Config push → satellite WS ACKs new wakeword config
    - OTA update button triggers /api/satellites/{id}/update → job created
"""
from __future__ import annotations

import pytest

from tests.e2e.helpers.asserts import (
    assert_body_not_blank,
    assert_no_critical_console_errors,
)
from tests.e2e.helpers.page import BASE_URL, capture_console_errors


pytestmark = pytest.mark.e2e


@pytest.fixture()
def area_page(page):
    page.goto(f"{BASE_URL}/admin/satellites",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestAdminSatellitesRenders:
    def test_page_loads_without_crash(self, area_page):
        get_errors = capture_console_errors(area_page)
        assert_body_not_blank(area_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())
