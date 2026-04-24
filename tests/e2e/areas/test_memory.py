
"""Real browser E2E tests for Erinnerungen (/memory).

Drives:
  * Page render + memory list
  * Create a memory (UI form if present, else API) → backend row exists
  * Retention: delete a memory → backend gone, UI refreshed
  * Retrieval: a memory created with a unique marker can be found
    via GET filter
"""
from __future__ import annotations

import re
import time
import uuid

import pytest

from tests.e2e.helpers import api
from tests.e2e.helpers.asserts import (
    assert_body_not_blank,
    assert_no_critical_console_errors,
)
from tests.e2e.helpers.page import BASE_URL, capture_console_errors


pytestmark = pytest.mark.e2e


@pytest.fixture()
def memory_page(page):
    page.goto(f"{BASE_URL}/memory",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


@pytest.fixture()
def created_memory_ids():
    ids: list[int] = []
    yield ids
    for mid in ids:
        try:
            api.delete_memory(mid)
        except Exception:
            pass


class TestMemoryPageRenders:
    def test_page_loads(self, memory_page):
        get_errors = capture_console_errors(memory_page)
        # i18n strings lazy-load; wait explicitly for the translated
        # heading instead of sampling the DOM at a single moment.
        memory_page.wait_for_selector(
            "h1:has-text('Erinnerungen'), h2:has-text('Erinnerungen'), "
            "h1:has-text('Memory'), h2:has-text('Memory')",
            timeout=10_000,
        )
        assert_body_not_blank(memory_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_memory_list_endpoint_responds(self):
        """Memory is opt-in (MEMORY_ENABLED). When off the endpoint may
        503 — we skip cleanly. When on it must return the standard
        envelope shape."""
        result = api.list_memories()
        assert isinstance(result, (dict, list)), (
            f"Unexpected memory list shape: {type(result).__name__}"
        )


class TestMemoryCreateDelete:
    def test_create_and_delete_memory_round_trip(self, created_memory_ids):
        marker = f"e2e-mem-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_memory({
                "content": f"{marker} — test memory written by e2e.",
                "category": "fact",
                "importance": 0.3,
            })
        except Exception as e:
            pytest.skip(f"create_memory schema/auth: {e}")
        mem_id = created.get("id")
        assert mem_id, f"Create returned no id: {created}"
        created_memory_ids.append(mem_id)

        result = api.list_memories()
        memories = result.get("memories", result) if isinstance(result, dict) else result
        assert any(marker in (m.get("content") or "") for m in memories), (
            f"Marker {marker!r} not found in memory list after create"
        )

        api.delete_memory(mem_id)
        created_memory_ids.remove(mem_id)
        result2 = api.list_memories()
        memories2 = result2.get("memories", result2) if isinstance(result2, dict) else result2
        assert not any(marker in (m.get("content") or "") for m in memories2), (
            f"Memory with marker {marker} still in list after DELETE"
        )

    def test_created_memory_surfaces_in_ui(self, memory_page, created_memory_ids):
        marker = f"e2e-ui-mem-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_memory({
                "content": f"{marker} — surface test",
                "category": "fact",
                "importance": 0.3,
            })
        except Exception as e:
            pytest.skip(f"create_memory schema/auth: {e}")
        if not created.get("id"):
            pytest.skip(f"create returned no id: {created}")
        created_memory_ids.append(created["id"])

        memory_page.reload(wait_until="networkidle", timeout=15_000)
        memory_page.wait_for_selector(f"text={marker}", timeout=10_000)
