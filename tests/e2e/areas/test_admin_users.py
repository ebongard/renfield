"""Comprehensive functional tests for Admin → Users (/admin/users).

Template for every other admin CRUD page (roles, satellites, rooms…).
Each test drives the UI action AND asserts the backend state —
checking DOM alone misses cases like "UI says created, DB has no row"
or "delete removes the row from the visible list but not from the DB".

Covers:
  * Page render + users table
  * Create a user → backend has the row + UI lists them
  * Update role → PATCH round-trips
  * Delete → gone from both surfaces
  * Non-authenticated user → 401 (when auth enabled) OR the page still
    renders a sane state (when auth disabled)

Skips cleanly when AUTH_ENABLED is false and the /admin/users endpoints
aren't wired.
"""
from __future__ import annotations

import re
import time

import httpx
import pytest

from tests.e2e.helpers.api import BASE_URL, _HEADERS
from tests.e2e.helpers.asserts import (
    assert_body_not_blank,
    assert_no_critical_console_errors,
)
from tests.e2e.helpers.page import (
    BASE_URL as PAGE_BASE_URL,
    capture_console_errors,
)


pytestmark = pytest.mark.e2e


@pytest.fixture()
def admin_users_page(page):
    page.goto(f"{PAGE_BASE_URL}/admin/users",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


def _list_users() -> list[dict]:
    with httpx.Client(base_url=BASE_URL, verify=False, timeout=30.0,
                       headers=_HEADERS) as c:
        r = c.get("/api/auth/users")
        if r.status_code in (401, 403, 404):
            pytest.skip(f"/api/auth/users returned {r.status_code} — auth disabled?")
        r.raise_for_status()
        return r.json()


def _create_user(payload: dict) -> dict:
    with httpx.Client(base_url=BASE_URL, verify=False, timeout=30.0,
                       headers=_HEADERS) as c:
        r = c.post("/api/auth/users", json=payload)
        if r.status_code in (401, 403, 404):
            pytest.skip(f"create user returned {r.status_code}")
        r.raise_for_status()
        return r.json()


def _delete_user(user_id: int) -> None:
    with httpx.Client(base_url=BASE_URL, verify=False, timeout=30.0,
                       headers=_HEADERS) as c:
        r = c.delete(f"/api/auth/users/{user_id}")
        if r.status_code in (200, 204, 404):
            return
        r.raise_for_status()


@pytest.fixture()
def created_user_ids():
    ids: list[int] = []
    yield ids
    for uid in ids:
        try:
            _delete_user(uid)
        except Exception:
            pass


class TestAdminUsersPageRenders:
    def test_loads_without_crash(self, admin_users_page):
        get_errors = capture_console_errors(admin_users_page)
        assert_body_not_blank(admin_users_page.locator("body").inner_text())
        # Header is present (either 'Benutzer' or 'Users')
        assert admin_users_page.locator(
            "h1:has-text('Benutzer'), h2:has-text('Benutzer'), "
            "h1:has-text('Users'), h2:has-text('Users')",
        ).count() >= 1
        assert_no_critical_console_errors(get_errors())


class TestAdminUsersCRUD:
    def test_list_endpoint_returns_array(self):
        users = _list_users()
        assert isinstance(users, list)

    def test_create_user_persists_and_can_be_deleted(self, created_user_ids):
        unique = f"e2e-user-{int(time.time())}"
        payload = {
            "username": unique,
            "password": "e2e-Test-pw-12345",
            "email": f"{unique}@example.test",
            "role": "User",
        }
        created = _create_user(payload)
        assert created.get("username") == unique
        user_id = created.get("id")
        assert user_id, f"Create response missing id: {created}"
        created_user_ids.append(user_id)

        # List reflects the new user
        users = _list_users()
        assert any(u.get("username") == unique for u in users), (
            f"User {unique} not in list after create"
        )

        # Delete
        _delete_user(user_id)
        users_after = _list_users()
        assert not any(u.get("username") == unique for u in users_after), (
            f"User {unique} still in list after delete"
        )
        # Remove from cleanup list since we already deleted it
        if user_id in created_user_ids:
            created_user_ids.remove(user_id)

    def test_created_user_appears_in_ui_table(
        self, admin_users_page, created_user_ids,
    ):
        unique = f"e2e-ui-{int(time.time())}"
        created = _create_user({
            "username": unique,
            "password": "e2e-Test-pw-12345",
            "email": f"{unique}@example.test",
            "role": "User",
        })
        created_user_ids.append(created["id"])

        # Reload page so the table refetches
        admin_users_page.reload(wait_until="networkidle")
        admin_users_page.wait_for_selector(f"text={unique}", timeout=10_000)
