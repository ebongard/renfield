"""Security review H1 — satellite enrollment admin API + register-time gating.

Route-level tests run with auth disabled (the default test posture), so they
exercise the enrollment logic, not the permission dependency (covered
elsewhere). Also covers the satellite_manager eviction guard and the
/api/ws/token hardening.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Satellite
from utils.config import settings


@pytest.mark.backend
@pytest.mark.database
class TestEnrollmentRoutes:
    async def test_enroll_returns_token_once(self, async_client: AsyncClient):
        r = await async_client.post(
            "/api/satellite-enrollment/enroll",
            json={"satellite_id": "sat-wohnzimmer", "room": "Wohnzimmer"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["satellite_id"] == "sat-wohnzimmer"
        assert body["token"] and len(body["token"]) > 20
        assert body["rotated"] is False

    async def test_list_never_exposes_token(self, async_client: AsyncClient):
        await async_client.post(
            "/api/satellite-enrollment/enroll", json={"satellite_id": "sat-x"}
        )
        r = await async_client.get("/api/satellite-enrollment")
        assert r.status_code == 200
        rows = r.json()
        assert any(row["satellite_id"] == "sat-x" for row in rows)
        for row in rows:
            assert "token" not in row
            assert "token_hash" not in row

    async def test_duplicate_enroll_409(self, async_client: AsyncClient):
        await async_client.post(
            "/api/satellite-enrollment/enroll", json={"satellite_id": "sat-x"}
        )
        r = await async_client.post(
            "/api/satellite-enrollment/enroll", json={"satellite_id": "sat-x"}
        )
        assert r.status_code == 409

    async def test_rotate_succeeds_and_flags_rotated(self, async_client: AsyncClient):
        await async_client.post(
            "/api/satellite-enrollment/enroll", json={"satellite_id": "sat-x"}
        )
        r = await async_client.post(
            "/api/satellite-enrollment/enroll",
            json={"satellite_id": "sat-x", "rotate": True},
        )
        assert r.status_code == 201
        assert r.json()["rotated"] is True

    async def test_revoke_204_then_404(self, async_client: AsyncClient):
        await async_client.post(
            "/api/satellite-enrollment/enroll", json={"satellite_id": "sat-x"}
        )
        r = await async_client.delete("/api/satellite-enrollment/sat-x")
        assert r.status_code == 204
        # A never-enrolled id → 404.
        r2 = await async_client.delete("/api/satellite-enrollment/never-existed")
        assert r2.status_code == 404

    async def test_invalid_satellite_id_rejected(self, async_client: AsyncClient):
        r = await async_client.post(
            "/api/satellite-enrollment/enroll", json={"satellite_id": "bad id!"}
        )
        assert r.status_code == 422  # pattern validation

    async def test_status_reports_pending(self, async_client: AsyncClient, monkeypatch):
        monkeypatch.setattr(settings, "satellite_enrollment_enabled", True)
        await async_client.post(
            "/api/satellite-enrollment/enroll", json={"satellite_id": "sat-1"}
        )
        r = await async_client.get("/api/satellite-enrollment/status")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["total_enrolled"] >= 1
        assert body["pending_first_auth"] >= 1
        assert body["enforcing"] is False


@pytest.mark.backend
@pytest.mark.database
class TestWsTokenHardening:
    async def test_ws_token_disabled_returns_null(self, async_client: AsyncClient, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", False)
        r = await async_client.post("/api/ws/token", params={"device_id": "x"})
        assert r.status_code == 200
        assert r.json()["token"] is None

    async def test_ws_token_requires_auth_when_enabled(self, async_client: AsyncClient, monkeypatch):
        # H1: with WS auth on and no authenticated user, the faucet is closed.
        monkeypatch.setattr(settings, "auth_enabled", True)
        r = await async_client.post("/api/ws/token", params={"device_id": "x"})
        assert r.status_code == 401


@pytest.mark.backend
class TestManagerEvictionGuard:
    async def test_unauthenticated_cannot_evict_authenticated(self):
        from unittest.mock import AsyncMock

        from ha_glue.services.satellite_manager import SatelliteManager

        mgr = SatelliteManager()
        ws_old = AsyncMock()
        ws_new = AsyncMock()

        # An authenticated incumbent registers.
        assert await mgr.register("sat-x", "Room", ws_old, {}, authenticated=True) is True
        # An UNauthenticated newcomer claiming the same id is refused.
        assert await mgr.register("sat-x", "Room", ws_new, {}, authenticated=False) is False
        # The incumbent's connection was NOT closed.
        ws_old.close.assert_not_called()
        # Registry still holds the authenticated incumbent.
        assert mgr.get_satellite("sat-x").websocket is ws_old

    async def test_authenticated_can_reconnect(self):
        from unittest.mock import AsyncMock

        from ha_glue.services.satellite_manager import SatelliteManager

        mgr = SatelliteManager()
        ws_old = AsyncMock()
        ws_new = AsyncMock()
        assert await mgr.register("sat-x", "Room", ws_old, {}, authenticated=True) is True
        # A new authenticated connection DOES take over (legit reconnect).
        assert await mgr.register("sat-x", "Room", ws_new, {}, authenticated=True) is True
        ws_old.close.assert_called_once()
        assert mgr.get_satellite("sat-x").websocket is ws_new
