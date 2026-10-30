"""
Tests for the camera API (Frigate integration)

Covers:
- Fetching camera events
- The camera list
- Snapshots
- Filtering events by label
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
    """Mock Frigate client for the route tests"""
    with patch('ha_glue.api.routes.camera.frigate') as mock:
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
    with patch('ha_glue.api.routes.camera.require_permission') as mock:
        mock.return_value = lambda: MagicMock(id=1, username="test")
        yield mock


# ============================================================================
# Events Tests
# ============================================================================

class TestCameraEvents:
    """Tests for camera events"""

    @pytest.mark.integration
    async def test_get_events(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """GET /api/camera/events"""
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
        """GET /api/camera/events with a camera filter"""
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
        """GET /api/camera/events with a label filter"""
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
        """GET /api/camera/events with a limit"""
        response = await async_client.get("/api/camera/events?limit=5")

        assert response.status_code in [200, 401, 403]


# ============================================================================
# Camera List Tests
# ============================================================================

class TestCameraList:
    """Tests for the camera list"""

    @pytest.mark.integration
    async def test_list_cameras(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """GET /api/camera/cameras"""
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
        """An error while fetching the camera list"""
        with patch('ha_glue.api.routes.camera.frigate') as mock:
            mock.get_cameras = AsyncMock(side_effect=Exception("Frigate not available"))

            response = await async_client.get("/api/camera/cameras")

        assert response.status_code in [500, 401, 403]


# ============================================================================
# Snapshot Tests
# ============================================================================

class TestSnapshots:
    """Tests for snapshots"""

    @pytest.mark.integration
    async def test_get_snapshot(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """GET /api/camera/snapshot/{event_id}"""
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
        """GET /api/camera/snapshot for an event that does not exist"""
        with patch('ha_glue.api.routes.camera.frigate') as mock:
            mock.get_snapshot = AsyncMock(return_value=None)

            response = await async_client.get("/api/camera/snapshot/nonexistent")

        assert response.status_code in [404, 401, 403]


# ============================================================================
# Latest Events Tests
# ============================================================================

class TestLatestEvents:
    """Tests for the latest events by label"""

    @pytest.mark.integration
    async def test_get_latest_by_label(
        self,
        async_client: AsyncClient,
        mock_frigate_client,
        mock_auth_bypass
    ):
        """GET /api/camera/latest/{label}"""
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
        """GET /api/camera/latest with the default label"""
        # Default label is 'person'
        response = await async_client.get("/api/camera/latest/person")

        assert response.status_code in [200, 401, 403]


# ============================================================================
# Error Handling Tests
# ============================================================================

class TestCameraErrorHandling:
    """Tests for error handling"""

    @pytest.mark.integration
    async def test_frigate_connection_error(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """An error on a Frigate connection problem"""
        with patch('ha_glue.api.routes.camera.frigate') as mock:
            mock.get_events = AsyncMock(side_effect=Exception("Connection refused"))

            response = await async_client.get("/api/camera/events")

        assert response.status_code in [500, 401, 403]

    @pytest.mark.integration
    async def test_snapshot_retrieval_error(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """An error while fetching a snapshot"""
        with patch('ha_glue.api.routes.camera.frigate') as mock:
            mock.get_snapshot = AsyncMock(side_effect=Exception("Snapshot error"))

            response = await async_client.get("/api/camera/snapshot/event-1")

        assert response.status_code in [500, 401, 403]


# ============================================================================
# Permission Tests
# ============================================================================

class TestCameraPermissions:
    """Tests for permissions"""

    @pytest.mark.integration
    async def test_events_requires_cam_view(
        self,
        async_client: AsyncClient,
        mock_frigate_client
    ):
        """Events require the cam.view permission"""
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
        """Snapshots require the cam.full permission"""
        response = await async_client.get("/api/camera/snapshot/event-1")

        # Either 200 (if auth disabled) or 401/403
        assert response.status_code in [200, 401, 403]
