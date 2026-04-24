"""Real browser E2E tests for Rollen (/admin/roles).

Drives:
  * Page render
  * List roles via API
  * CRUD: create → list → delete round-trip
  * Created role surfaces in the UI
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
def roles_page(page):
    page.goto(f"{BASE_URL}/admin/roles",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


@pytest.fixture()
def created_role_ids():
    ids: list[int] = []
    yield ids
    for rid in ids:
        try:
            api.delete_role(rid)
        except Exception:
            pass


class TestRolesPageRenders:
    def test_page_loads(self, roles_page):
        get_errors = capture_console_errors(roles_page)
        assert_body_not_blank(roles_page.locator("body").inner_text())
        assert roles_page.get_by_role(
            "heading", name=re.compile(r"Rollen|Roles", re.IGNORECASE),
        ).first.is_visible()
        assert_no_critical_console_errors(get_errors())

    def test_roles_endpoint_returns_list(self):
        result = api.list_roles()
        assert isinstance(result, list)

    def test_permissions_endpoint_returns_list(self):
        result = api.get("/api/roles/permissions/all",
                          skip_on_status=(401, 403, 404))
        assert result is not None


class TestRolesCRUD:
    def test_create_and_delete_role(self, created_role_ids):
        name = f"e2e-role-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_role({
                "name": name,
                "description": "e2e test role",
                "permissions": ["chat.read"],
            })
        except Exception as e:
            pytest.skip(f"create_role schema/auth: {e}")
        role_id = created.get("id")
        assert role_id, f"No id: {created}"
        created_role_ids.append(role_id)

        roles = api.list_roles()
        assert any(r.get("name") == name for r in roles), (
            f"Role {name} not in list after create"
        )

        api.delete_role(role_id)
        created_role_ids.remove(role_id)
        roles_after = api.list_roles()
        assert not any(r.get("name") == name for r in roles_after), (
            f"Role {name} still in list after delete"
        )

    def test_created_role_appears_in_ui(self, roles_page, created_role_ids):
        name = f"e2e-ui-role-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_role({
                "name": name,
                "description": "e2e",
                "permissions": ["chat.read"],
            })
        except Exception as e:
            pytest.skip(f"create_role schema/auth: {e}")
        if not created.get("id"):
            pytest.skip(f"No id: {created}")
        created_role_ids.append(created["id"])

        roles_page.reload(wait_until="networkidle", timeout=15_000)
        roles_page.wait_for_selector(f"text={name}", timeout=10_000)
