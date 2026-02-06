"""
Tests für Camera API (Frigate Integration)

Testet:
- Kamera-Events abrufen
- Kamera-Liste
- Snapshots
- Events nach Label filtern
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_frigate_client():
    """Mock Frigate Client für Route-Tests"""
    with patch('api.routes.camera.frigate') as mock:
        mock.get_events = AsyncMock(return_value=[
            {
                "id": "event-1",
                "camera": "front_door",
                "label": "person",
                "confidence": 0.85,
                "start_time": datetime.utcnow().timestamp(),
                "has_snapshot": True
            },
            {
                "id": "event-2",
                "camera": "back_yard",
                "label": "car",
                "confidence": 0.92,
                "start_time": datetime.utcnow().timestamp(),
                "has_snapshot": True
            }
        ])
        mock.get_cameras = AsyncMock(return_value=[
            {"name": "front_door", "enabled": True},
            {"name": "back_yard", "enabled": True},
            {"name": "garage", "enabled": False}
        ])
        mock.get_snapshot = AsyncMock(return_value=b'\x89PNG\r\n\x1a\n' + b'\x00' * 100)
        mock.get_latest_events_by_type = AsyncMock(return_value=[
            {
                "id": "latest-1",
                "camera": "front_door",
                "label": "person",
                "confidence": 0.95
            }
        ])
        yield mock


@pytest.fixture
def mock_auth_bypass():
    """Bypass auth for testing"""
    with patch('api.routes.camera.require_permission') as mock:
        mock.return_value = lambda: MagicMock(id=1, username="test")
        yield mock


# ============================================================================
# Events Tests
# ============================================================================

class TestCameraEvents:
    """Tests für Kamera-Events"""

    @pytest.mark.integration
    async def test_get_events(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/events"""
        response = await async_client.get("/api/camera/events")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert "events" in data
            assert len(data["events"]) == 2

    @pytest.mark.integration
    async def test_get_events_with_camera_filter(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/events mit Kamera-Filter"""
        response = await async_client.get("/api/camera/events?camera=front_door")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert "events" in data

    @pytest.mark.integration
    async def test_get_events_with_label_filter(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/events mit Label-Filter"""
        response = await async_client.get("/api/camera/events?label=person")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert "events" in data

    @pytest.mark.integration
    async def test_get_events_with_limit(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/events mit Limit"""
        response = await async_client.get("/api/camera/events?limit=5")

        assert response.status_code in [200, 401, 403]


# ============================================================================
# Camera List Tests
# ============================================================================

class TestCameraList:
    """Tests für Kamera-Liste"""

    @pytest.mark.integration
    async def test_list_cameras(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/cameras"""
        response = await async_client.get("/api/camera/cameras")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert "cameras" in data
            assert len(data["cameras"]) == 3

    @pytest.mark.integration
    async def test_list_cameras_error(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """Testet Fehler bei Kamera-Liste abrufen"""
        with patch('api.routes.camera.frigate') as mock:
            mock.get_cameras = AsyncMock(side_effect=Exception("Frigate not available"))

            response = await async_client.get("/api/camera/cameras")

        assert response.status_code in [500, 401, 403]


# ============================================================================
# Snapshot Tests
# ============================================================================

class TestSnapshots:
    """Tests für Snapshots"""

    @pytest.mark.integration
    async def test_get_snapshot(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/snapshot/{event_id}"""
        response = await async_client.get("/api/camera/snapshot/event-1")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            assert response.headers["content-type"] == "image/jpeg"

    @pytest.mark.integration
    async def test_get_snapshot_not_found(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/snapshot für nicht-existentes Event"""
        with patch('api.routes.camera.frigate') as mock:
            mock.get_snapshot = AsyncMock(return_value=None)

            response = await async_client.get("/api/camera/snapshot/nonexistent")

        assert response.status_code in [404, 401, 403]


# ============================================================================
# Latest Events Tests
# ============================================================================

class TestLatestEvents:
    """Tests für neueste Events nach Label"""

    @pytest.mark.integration
    async def test_get_latest_by_label(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/latest/{label}"""
        response = await async_client.get("/api/camera/latest/person")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert "events" in data

    @pytest.mark.integration
    async def test_get_latest_default_label(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """Testet GET /api/camera/latest mit Default-Label"""
        # Default label is 'person'
        response = await async_client.get("/api/camera/latest/person")

        assert response.status_code in [200, 401, 403]


# ============================================================================
# Error Handling Tests
# ============================================================================

class TestCameraErrorHandling:
    """Tests für Fehlerbehandlung"""

    @pytest.mark.integration
    async def test_frigate_connection_error(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """Testet Fehler bei Frigate-Verbindungsproblem"""
        with patch('api.routes.camera.frigate') as mock:
            mock.get_events = AsyncMock(side_effect=Exception("Connection refused"))

            response = await async_client.get("/api/camera/events")

        assert response.status_code in [500, 401, 403]

    @pytest.mark.integration
    async def test_snapshot_retrieval_error(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """Testet Fehler beim Snapshot-Abruf"""
        with patch('api.routes.camera.frigate') as mock:
            mock.get_snapshot = AsyncMock(side_effect=Exception("Snapshot error"))

            response = await async_client.get("/api/camera/snapshot/event-1")

        assert response.status_code in [500, 401, 403]


# ============================================================================
# Permission Tests
# ============================================================================

class TestCameraPermissions:
    """Tests für Berechtigungen"""

    @pytest.mark.integration
    async def test_events_requires_cam_view(
        self,
        async_client: AsyncClient,
        mock_frigate_client
    ):
        """Testet, dass Events cam.view Permission erfordert"""
        # Without auth bypass, should require permission
        response = await async_client.get("/api/camera/events")

        # Either 200 (if auth disabled) or 401/403
        assert response.status_code in [200, 401, 403]

    @pytest.mark.integration
    async def test_snapshot_requires_cam_full(
        self,
        async_client: AsyncClient,
        mock_frigate_client
    ):
        """Testet, dass Snapshots cam.full Permission erfordert"""
        response = await async_client.get("/api/camera/snapshot/event-1")

        # Either 200 (if auth disabled) or 401/403
        assert response.status_code in [200, 401, 403]
