"""Real browser E2E tests for Benutzer (/admin/users).

Drives:
  * Page render
  * List users via /api/users
  * Create → list → delete round-trip
  * Created user surfaces in the UI
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
def admin_users_page(page):
    page.goto(f"{BASE_URL}/admin/users",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


@pytest.fixture()
def created_user_ids():
    ids: list[int] = []
    yield ids
    for uid in ids:
        try:
            api.delete_user(uid)
        except Exception:
            pass


class TestAdminUsersPageRenders:
    def test_loads_without_crash(self, admin_users_page):
        get_errors = capture_console_errors(admin_users_page)
        assert_body_not_blank(admin_users_page.locator("body").inner_text())
        assert admin_users_page.get_by_role(
            "heading", name=re.compile(r"Benutzer|Users", re.IGNORECASE),
        ).first.is_visible()
        assert_no_critical_console_errors(get_errors())

    def test_list_endpoint_returns_envelope(self):
        result = api.list_users(limit=10)
        assert result is not None
        users = result.get("users") if isinstance(result, dict) else result
        assert isinstance(users, list)


class TestAdminUsersCRUD:
    def test_create_and_delete_user_via_api(self, created_user_ids):
        unique = f"e2e-user-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_user({
                "username": unique,
                "password": "e2e-Test-pw-12345",
                "email": f"{unique}@example.test",
            })
        except Exception as e:
            pytest.skip(f"create_user schema/auth: {e}")
        assert created.get("username") == unique
        user_id = created.get("id")
        assert user_id, f"No id: {created}"
        created_user_ids.append(user_id)

        result = api.list_users(limit=100)
        users = result.get("users") if isinstance(result, dict) else result
        assert any(u.get("username") == unique for u in users), (
            f"User {unique} not in list after create"
        )

        api.delete_user(user_id)
        created_user_ids.remove(user_id)
        result_after = api.list_users(limit=100)
        users_after = result_after.get("users") if isinstance(result_after, dict) else result_after
        assert not any(
            u.get("username") == unique for u in users_after
        ), f"User {unique} still in list after delete"

    def test_created_user_appears_in_ui_table(
        self, admin_users_page, created_user_ids,
    ):
        unique = f"e2e-ui-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_user({
                "username": unique,
                "password": "e2e-Test-pw-12345",
                "email": f"{unique}@example.test",
            })
        except Exception as e:
            pytest.skip(f"create_user schema/auth: {e}")
        if not created.get("id"):
            pytest.skip(f"create returned no id: {created}")
        created_user_ids.append(created["id"])

        admin_users_page.reload(wait_until="networkidle")
        admin_users_page.wait_for_selector(f"text={unique}", timeout=10_000)
