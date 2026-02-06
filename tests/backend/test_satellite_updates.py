"""
Tests for Satellite OTA Update System

Tests cover:
- Version tracking in satellite registration
- Update service functionality
- API endpoints for updates
- WebSocket message handling for updates
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

# =============================================================================
# SatelliteManager Version Tracking Tests
# =============================================================================

class TestSatelliteManagerVersionTracking:
    """Tests for version tracking in SatelliteManager"""

    @pytest.mark.unit
    def test_satellite_info_has_version_field(self):
        """SatelliteInfo should have version and update fields"""
        from services.satellite_manager import SatelliteCapabilities, SatelliteInfo, UpdateStatus

        caps = SatelliteCapabilities()
        mock_ws = MagicMock()

        sat = SatelliteInfo(
            satellite_id="test-sat",
            room="Living Room",
            websocket=mock_ws,
            capabilities=caps,
            version="1.0.0"
        )

        assert sat.version == "1.0.0"
        assert sat.update_status == UpdateStatus.NONE
        assert sat.update_stage is None
        assert sat.update_progress == 0
        assert sat.update_error is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_register_with_version(self):
        """Register should store version"""
        from services.satellite_manager import SatelliteManager

        manager = SatelliteManager()
        mock_ws = MagicMock()
        mock_ws.close = AsyncMock()

        success = await manager.register(
            satellite_id="test-sat",
            room="Living Room",
            websocket=mock_ws,
            capabilities={},
            version="1.2.3"
        )

        assert success
        sat = manager.get_satellite("test-sat")
        assert sat is not None
        assert sat.version == "1.2.3"

    @pytest.mark.unit
    def test_update_heartbeat_with_version(self):
        """Heartbeat should update version"""
        from services.satellite_manager import SatelliteCapabilities, SatelliteInfo, SatelliteManager

        manager = SatelliteManager()
        mock_ws = MagicMock()

        # Manually add satellite with old version
        manager.satellites["test-sat"] = SatelliteInfo(
            satellite_id="test-sat",
            room="Kitchen",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0"
        )

        # Update via heartbeat with new version
        manager.update_heartbeat("test-sat", None, "1.1.0")

        sat = manager.get_satellite("test-sat")
        assert sat.version == "1.1.0"

    @pytest.mark.unit
    def test_set_update_status(self):
        """set_update_status should update all update fields"""
        from services.satellite_manager import SatelliteCapabilities, SatelliteInfo, SatelliteManager, UpdateStatus

        manager = SatelliteManager()
        mock_ws = MagicMock()

        manager.satellites["test-sat"] = SatelliteInfo(
            satellite_id="test-sat",
            room="Kitchen",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities()
        )

        manager.set_update_status(
            "test-sat",
            UpdateStatus.IN_PROGRESS,
            stage="downloading",
            progress=45,
            error=None
        )

        sat = manager.get_satellite("test-sat")
        assert sat.update_status == UpdateStatus.IN_PROGRESS
        assert sat.update_stage == "downloading"
        assert sat.update_progress == 45
        assert sat.update_error is None

    @pytest.mark.unit
    def test_clear_update_status(self):
        """clear_update_status should reset all update fields"""
        from services.satellite_manager import SatelliteCapabilities, SatelliteInfo, SatelliteManager, UpdateStatus

        manager = SatelliteManager()
        mock_ws = MagicMock()

        manager.satellites["test-sat"] = SatelliteInfo(
            satellite_id="test-sat",
            room="Kitchen",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            update_status=UpdateStatus.COMPLETED,
            update_stage="completed",
            update_progress=100
        )

        manager.clear_update_status("test-sat")

        sat = manager.get_satellite("test-sat")
        assert sat.update_status == UpdateStatus.NONE
        assert sat.update_stage is None
        assert sat.update_progress == 0

    @pytest.mark.unit
    def test_get_all_satellites_includes_version(self):
        """get_all_satellites should include version and update info"""
        from services.satellite_manager import SatelliteCapabilities, SatelliteInfo, SatelliteManager, UpdateStatus

        manager = SatelliteManager()
        mock_ws = MagicMock()

        manager.satellites["test-sat"] = SatelliteInfo(
            satellite_id="test-sat",
            room="Kitchen",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0",
            update_status=UpdateStatus.IN_PROGRESS,
            update_stage="downloading",
            update_progress=50
        )

        satellites = manager.get_all_satellites()
        assert len(satellites) == 1

        sat_data = satellites[0]
        assert sat_data["version"] == "1.0.0"
        assert sat_data["update_status"] == "in_progress"
        assert sat_data["update_stage"] == "downloading"
        assert sat_data["update_progress"] == 50


# =============================================================================
# SatelliteUpdateService Tests
# =============================================================================

class TestSatelliteUpdateService:
    """Tests for SatelliteUpdateService"""

    @pytest.mark.unit
    def test_get_latest_version(self):
        """get_latest_version should return config value"""
        from services.satellite_update_service import SatelliteUpdateService

        with patch('services.satellite_update_service.settings') as mock_settings:
            mock_settings.satellite_latest_version = "2.0.0"
            service = SatelliteUpdateService()
            assert service.get_latest_version() == "2.0.0"

    @pytest.mark.unit
    def test_is_update_available_newer(self):
        """is_update_available should return True when newer version exists"""
        from services.satellite_update_service import SatelliteUpdateService

        with patch('services.satellite_update_service.settings') as mock_settings:
            mock_settings.satellite_latest_version = "2.0.0"
            service = SatelliteUpdateService()

            assert service.is_update_available("1.0.0") is True
            assert service.is_update_available("1.9.9") is True

    @pytest.mark.unit
    def test_is_update_available_same(self):
        """is_update_available should return False when same version"""
        from services.satellite_update_service import SatelliteUpdateService

        with patch('services.satellite_update_service.settings') as mock_settings:
            mock_settings.satellite_latest_version = "1.0.0"
            service = SatelliteUpdateService()

            assert service.is_update_available("1.0.0") is False

    @pytest.mark.unit
    def test_is_update_available_newer_current(self):
        """is_update_available should return False when current is newer"""
        from services.satellite_update_service import SatelliteUpdateService

        with patch('services.satellite_update_service.settings') as mock_settings:
            mock_settings.satellite_latest_version = "1.0.0"
            service = SatelliteUpdateService()

            assert service.is_update_available("2.0.0") is False

    @pytest.mark.unit
    def test_is_update_available_unknown(self):
        """is_update_available should return False for unknown version"""
        from services.satellite_update_service import SatelliteUpdateService

        with patch('services.satellite_update_service.settings') as mock_settings:
            mock_settings.satellite_latest_version = "1.0.0"
            service = SatelliteUpdateService()

            assert service.is_update_available("unknown") is False


# =============================================================================
# API Endpoint Tests
# =============================================================================

class TestSatelliteUpdateEndpoints:
    """Tests for satellite update API endpoints"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_list_satellites_includes_version(self, async_client: AsyncClient):
        """GET /api/satellites should include version info"""
        response = await async_client.get("/api/satellites")
        assert response.status_code == 200

        data = response.json()
        assert "latest_version" in data
        assert isinstance(data["latest_version"], str)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_versions_endpoint(self, async_client: AsyncClient):
        """GET /api/satellites/versions should return version info"""
        response = await async_client.get("/api/satellites/versions")
        assert response.status_code == 200

        data = response.json()
        assert "latest_version" in data
        assert "satellites" in data
        assert isinstance(data["satellites"], list)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_update_endpoint_satellite_not_found(self, async_client: AsyncClient):
        """POST /api/satellites/{id}/update should return 400 for unknown satellite"""
        response = await async_client.post("/api/satellites/unknown-sat/update")
        assert response.status_code == 400

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_update_status_endpoint_not_found(self, async_client: AsyncClient):
        """GET /api/satellites/{id}/update-status should return 404 for unknown satellite"""
        response = await async_client.get("/api/satellites/unknown-sat/update-status")
        assert response.status_code == 404


# =============================================================================
# Version Comparison Helper Tests
# =============================================================================

class TestVersionComparison:
    """Tests for version comparison logic"""

    @pytest.mark.unit
    def test_version_comparison_in_api(self):
        """_is_update_available should correctly compare versions"""
        from api.routes.satellites import _is_update_available

        # Newer version available
        assert _is_update_available("1.0.0", "2.0.0") is True
        assert _is_update_available("1.0.0", "1.1.0") is True
        assert _is_update_available("1.0.0", "1.0.1") is True

        # Same version
        assert _is_update_available("1.0.0", "1.0.0") is False

        # Current is newer
        assert _is_update_available("2.0.0", "1.0.0") is False
        assert _is_update_available("1.1.0", "1.0.0") is False

        # Unknown version
        assert _is_update_available("unknown", "1.0.0") is False

        # Different length versions
        assert _is_update_available("1.0", "1.0.1") is True
        assert _is_update_available("1.0.0", "1.1") is True
