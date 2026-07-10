"""Route test for GET /api/config/features (eng-review D11).

The Fakten panel's "extraction disabled" empty state depends on this endpoint
exposing `schicht_a_extraction_enabled` to the frontend. No DB needed — the
route reads `settings` + `get_user_or_default`, so this runs without Postgres.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from utils.config import settings


pytestmark = [pytest.mark.asyncio]


def _auth_default(app) -> None:
    from models.database import User
    from services.auth_service import get_user_or_default
    app.dependency_overrides[get_user_or_default] = lambda: User(
        id=1, username="t", password_hash="x", is_active=True, role_id=1,
    )


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("enabled", [True, False])
async def test_features_reports_schicht_a_flag(monkeypatch, enabled):
    monkeypatch.setattr(settings, "schicht_a_extraction_enabled", enabled)
    from main import app
    _auth_default(app)
    try:
        async with await _client(app) as c:
            resp = await c.get("/api/config/features")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["schicht_a_extraction_enabled"] is enabled


@pytest.mark.parametrize("enabled", [True, False])
async def test_features_reports_role_surfacing_flag(monkeypatch, enabled):
    """Item 6: the chat role badge is frontend-gated on this allowlisted flag."""
    monkeypatch.setattr(settings, "role_surfacing_enabled", enabled)
    from main import app
    _auth_default(app)
    try:
        async with await _client(app) as c:
            resp = await c.get("/api/config/features")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["role_surfacing_enabled"] is enabled


async def test_features_wissensbasis_reva_false_in_standalone():
    """Standalone Renfield does NOT mount the Reva-only /me/mix route, so the
    flag is False — the frontend then hides the Reva panels WITHOUT probing an
    endpoint that 404s (the console-noise fix)."""
    from main import app
    _auth_default(app)
    try:
        async with await _client(app) as c:
            resp = await c.get("/api/config/features")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["wissensbasis_reva_available"] is False


async def test_features_wissensbasis_reva_true_when_route_mounted():
    """When the Reva adapter has mounted /api/wissensbasis/me/mix, the route
    introspection reports the surface as available."""
    from main import app
    _auth_default(app)

    @app.get("/api/wissensbasis/me/mix")
    async def _fake_mix():  # pragma: no cover - presence is what matters
        return {}

    try:
        async with await _client(app) as c:
            resp = await c.get("/api/config/features")
        assert resp.status_code == 200
        assert resp.json()["wissensbasis_reva_available"] is True
    finally:
        app.dependency_overrides.clear()
        # Remove the temporary route so other tests see standalone topology.
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", None) != "/api/wissensbasis/me/mix"
        ]
        app.openapi_schema = None
