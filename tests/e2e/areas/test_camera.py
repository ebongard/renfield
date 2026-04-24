"""Real browser E2E tests for Kameras (/camera).

Drives:
  * Page render
  * GET /api/camera/cameras returns a list
  * Page fetches cameras + events on load
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
def camera_page(page):
    page.goto(f"{BASE_URL}/camera",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestCameraPage:
    def test_page_loads(self, camera_page):
        get_errors = capture_console_errors(camera_page)
        assert_body_not_blank(camera_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_cameras_endpoint_returns_list(self):
        result = api.list_cameras()
        assert result is not None
        cams = result.get("cameras") if isinstance(result, dict) else result
        assert isinstance(cams, list)

    def test_page_fetches_cameras_on_load(self, camera_page):
        with camera_page.expect_request(
            re.compile(r"/api/camera/cameras"), timeout=15_000,
        ):
            camera_page.reload(wait_until="networkidle", timeout=20_000)

    def test_page_fetches_events_on_load(self, camera_page):
        get_errors = capture_console_errors(camera_page)
        with camera_page.expect_request(
            re.compile(r"/api/camera/events"), timeout=15_000,
        ):
            camera_page.reload(wait_until="networkidle", timeout=20_000)
        assert_no_critical_console_errors(get_errors())
