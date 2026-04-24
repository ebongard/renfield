"""Real browser E2E tests for Einstellungen (/admin/settings).

Drives:
  * Page render
  * Wakeword settings endpoint responds
  * Page fetches wakeword on load
  * Wakeword models endpoint returns list
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
def settings_page(page):
    page.goto(f"{BASE_URL}/admin/settings",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestAdminSettingsPage:
    def test_page_loads(self, settings_page):
        get_errors = capture_console_errors(settings_page)
        assert_body_not_blank(settings_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_wakeword_settings_endpoint_responds(self):
        result = api.wakeword_settings()
        assert isinstance(result, dict)

    def test_wakeword_models_endpoint_returns_list(self):
        result = api.wakeword_models()
        assert result is not None
        models = result.get("models") if isinstance(result, dict) else result
        assert isinstance(models, list)

    def test_page_fetches_wakeword_on_load(self, settings_page):
        get_errors = capture_console_errors(settings_page)
        with settings_page.expect_request(
            re.compile(r"/api/settings/wakeword"), timeout=15_000,
        ):
            settings_page.reload(wait_until="networkidle")
        assert_no_critical_console_errors(get_errors())
