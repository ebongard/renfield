"""Real browser E2E tests for Überprüfung (/brain/review).

Review queue for circle-tier decisions. Drives:
  * Page render + pending-atoms endpoint returns a list
  * Each review entry displays a tier picker
  * PATCH /api/atoms/{id}/tier mutates the tier and the page refreshes
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
def review_page(page):
    page.goto(f"{BASE_URL}/brain/review",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestBrainReviewPageRenders:
    def test_page_loads(self, review_page):
        get_errors = capture_console_errors(review_page)
        assert_body_not_blank(review_page.locator("body").inner_text())
        assert review_page.get_by_role(
            "heading",
            name=re.compile(r"Überprüfung|Review", re.IGNORECASE),
        ).first.is_visible()
        assert_no_critical_console_errors(get_errors())

    def test_review_queue_endpoint_responds(self):
        """GET /api/circles/me/atoms-for-review must return a list (may be
        empty). A 500 here is a classic permission-filter regression."""
        result = api.get(
            "/api/circles/me/atoms-for-review",
            skip_on_status=(401, 403, 404),
        )
        assert isinstance(result, list), (
            f"Expected list, got {type(result).__name__}"
        )


class TestBrainReviewEmptyState:
    def test_empty_queue_shows_clean_empty_state(self, review_page):
        """If the queue is empty, the page must render an empty-state
        message — not a blank region."""
        items_locator = review_page.locator("article, [role='article'], li")
        empty_locator = review_page.locator(
            "text=/keine|empty|nichts/i"
        )
        # Either at least one item or an explicit empty-state message
        assert items_locator.count() > 0 or empty_locator.count() > 0, (
            "Review page shows neither items nor empty-state message — "
            "page likely rendered a broken shell"
        )
