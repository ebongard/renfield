"""HTTP route tests for the Paperless-audit review-overlay endpoint.

PATCH /api/admin/paperless-audit/results/{id} is ADMIN-gated; these cover the
status-code contract (200 / 400 / 404) and the auth gate at the HTTP boundary.
The service internals (update_review / _validate_*) are unit-tested in
test_paperless_audit.py — here the service is mocked so we exercise only the
route wiring, error mapping, and permission dependency.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ha_glue.api.routes.paperless_audit import router
from models.permissions import Permission
from services.auth_service import get_current_user
from services.database import get_db

pytestmark = [pytest.mark.backend]

_PATH = "/api/admin/paperless-audit/results/5"


class _FakeUser:
    """Avoids ORM lazy-load: exposes has_permission directly (mirrors the
    tool-health route tests)."""

    def __init__(self, is_admin: bool):
        self.id = 1
        self.username = "u"
        self._admin = is_admin

    def has_permission(self, perm: str) -> bool:
        return self._admin and perm == Permission.ADMIN.value


def _app(service, *, is_admin: bool, monkeypatch) -> FastAPI:
    # auth_enabled must be True or require_permission short-circuits to allow.
    monkeypatch.setattr("services.auth_service.settings.auth_enabled", True)
    app = FastAPI()
    app.include_router(router)
    app.state.paperless_audit = service
    app.dependency_overrides[get_current_user] = lambda: _FakeUser(is_admin)
    app.dependency_overrides[get_db] = lambda: None  # unused by permission_checker
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_patch_review_200_on_success(monkeypatch):
    svc = AsyncMock()
    svc.update_review.return_value = {"id": 5, "user_overrides": {"title": "X"}}
    app = _app(svc, is_admin=True, monkeypatch=monkeypatch)
    async with _client(app) as c:
        r = await c.patch(_PATH, json={"overrides": {"title": "X"}, "field_selection": ["title"]})
    assert r.status_code == 200
    assert r.json()["id"] == 5
    svc.update_review.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_review_400_on_validation_error(monkeypatch):
    svc = AsyncMock()
    svc.update_review.side_effect = ValueError("unknown field: 'x'")
    app = _app(svc, is_admin=True, monkeypatch=monkeypatch)
    async with _client(app) as c:
        r = await c.patch(_PATH, json={"overrides": {"x": "y"}})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_review_404_on_unknown_id(monkeypatch):
    svc = AsyncMock()
    svc.update_review.return_value = None
    app = _app(svc, is_admin=True, monkeypatch=monkeypatch)
    async with _client(app) as c:
        r = await c.patch("/api/admin/paperless-audit/results/999", json={"field_selection": ["title"]})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_review_requires_admin(monkeypatch):
    svc = AsyncMock()
    app = _app(svc, is_admin=False, monkeypatch=monkeypatch)
    async with _client(app) as c:
        r = await c.patch(_PATH, json={"overrides": {"title": "X"}})
    assert r.status_code in (401, 403)
    svc.update_review.assert_not_awaited()


_TAX_PATH = "/api/admin/paperless-audit/taxonomy"


@pytest.mark.asyncio
async def test_taxonomy_200_for_admin(monkeypatch):
    svc = AsyncMock()
    svc.get_taxonomy.return_value = {
        "correspondents": ["A"], "document_types": [], "tags": [], "storage_paths": [],
        "allow_create": {"correspondent": True, "document_type": True, "tags": True, "storage_path": False},
    }
    app = _app(svc, is_admin=True, monkeypatch=monkeypatch)
    async with _client(app) as c:
        r = await c.get(_TAX_PATH)
    assert r.status_code == 200
    assert r.json()["correspondents"] == ["A"]
    svc.get_taxonomy.assert_awaited_once()


@pytest.mark.asyncio
async def test_taxonomy_requires_admin(monkeypatch):
    svc = AsyncMock()
    app = _app(svc, is_admin=False, monkeypatch=monkeypatch)
    async with _client(app) as c:
        r = await c.get(_TAX_PATH)
    assert r.status_code in (401, 403)
    svc.get_taxonomy.assert_not_awaited()
