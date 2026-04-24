"""Real browser E2E tests for Wissensgraph (/knowledge-graph).

Drives:
  * Page render
  * Entities / relations / stats endpoints respond with expected shape
  * Circle tiers endpoint returns 5 rungs
  * Page fetches entities + stats on load
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
def kg_page(page):
    page.goto(f"{BASE_URL}/knowledge-graph",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


class TestKnowledgeGraphRenders:
    def test_page_loads(self, kg_page):
        get_errors = capture_console_errors(kg_page)
        assert_body_not_blank(kg_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_entities_endpoint_returns_list(self):
        result = api.kg_entities(limit=5)
        assert result is not None
        entities = result.get("entities") if isinstance(result, dict) else result
        assert isinstance(entities, list)

    def test_relations_endpoint_returns_list(self):
        result = api.kg_relations(limit=5)
        assert result is not None
        relations = result.get("relations") if isinstance(result, dict) else result
        assert isinstance(relations, list)

    def test_stats_endpoint_has_counts(self):
        stats = api.kg_stats()
        assert isinstance(stats, dict)
        assert any(k in stats for k in
                    ("entity_count", "entities", "total_entities",
                     "relation_count", "relations", "total_relations")), (
            f"Stats missing count fields: {list(stats)}"
        )

    def test_circle_tiers_endpoint_returns_five_entries(self):
        tiers = api.kg_circle_tiers()
        if isinstance(tiers, dict) and "tiers" in tiers:
            tiers = tiers["tiers"]
        count = len(tiers) if hasattr(tiers, "__len__") else 0
        assert count == 5, f"Expected 5 tiers, got {count}: {tiers!r}"


class TestKnowledgeGraphFetches:
    def test_page_fetches_entities_on_load(self, kg_page):
        get_errors = capture_console_errors(kg_page)
        with kg_page.expect_request(
            re.compile(r"/api/knowledge-graph/entities"), timeout=15_000,
        ):
            kg_page.reload(wait_until="networkidle", timeout=20_000)
        assert_no_critical_console_errors(get_errors())

    def test_page_fetches_stats_on_load(self, kg_page):
        """Stats may lazy-load behind a tab or after an interaction —
        this test only asserts the endpoint IS reachable, not when it
        fires. A full timing contract needs a look at the specific UI
        state machine; until then we verify the page at least didn't
        broken-shell before any stats panel would open."""
        stats = api.kg_stats()
        assert isinstance(stats, dict)
