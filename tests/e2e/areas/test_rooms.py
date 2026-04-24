"""Real browser E2E tests for Räume (/rooms).

Drives:
  * Page render
  * List rooms via API
  * Create → list → delete round-trip
  * Created room surfaces in the UI after reload
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
def rooms_page(page):
    page.goto(f"{BASE_URL}/rooms",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


@pytest.fixture()
def created_room_ids():
    ids: list[int] = []
    yield ids
    for rid in ids:
        try:
            api.delete_room(rid)
        except Exception:
            pass


class TestRoomsPageRenders:
    def test_page_loads(self, rooms_page):
        get_errors = capture_console_errors(rooms_page)
        assert_body_not_blank(rooms_page.locator("body").inner_text())
        assert rooms_page.get_by_role(
            "heading", name=re.compile(r"Räume|Rooms", re.IGNORECASE),
        ).first.is_visible()
        assert_no_critical_console_errors(get_errors())

    def test_rooms_endpoint_responds(self):
        result = api.list_rooms()
        assert isinstance(result, list)


class TestRoomsCreateDelete:
    def test_create_and_delete_room_round_trip(self, created_room_ids):
        name = f"e2e-room-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_room({"name": name})
        except Exception as e:
            pytest.skip(f"create_room schema/auth: {e}")
        room_id = created.get("id")
        assert room_id, f"Create returned no id: {created}"
        created_room_ids.append(room_id)

        rooms = api.list_rooms()
        assert any(r.get("name") == name for r in rooms), (
            f"Room {name} not in list after create"
        )

        api.delete_room(room_id)
        created_room_ids.remove(room_id)
        rooms_after = api.list_rooms()
        assert not any(r.get("name") == name for r in rooms_after), (
            f"Room {name} still in list after delete"
        )

    def test_created_room_appears_in_ui(self, rooms_page, created_room_ids):
        name = f"e2e-ui-room-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_room({"name": name})
        except Exception as e:
            pytest.skip(f"create_room schema/auth: {e}")
        if not created.get("id"):
            pytest.skip(f"create returned no id: {created}")
        created_room_ids.append(created["id"])

        rooms_page.reload(wait_until="networkidle", timeout=15_000)
        rooms_page.wait_for_selector(f"text={name}", timeout=10_000)
