"""
Tests for Satellite OTA Update System

Tests cover:
- Version tracking in satellite registration
- Update service functionality
- API endpoints for updates
- WebSocket message handling for updates
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

# A source path guaranteed not to exist, so get_latest_version() falls back to
# the configured value (these tests assert the config-fallback behavior, not the
# bundled-source read which is covered separately below).
_NO_SOURCE = Path("/nonexistent/satellite-source")

# =============================================================================
# SatelliteManager Version Tracking Tests
# =============================================================================

class TestSatelliteManagerVersionTracking:
    """Tests for version tracking in SatelliteManager"""

    @pytest.mark.unit
    def test_satellite_info_has_version_field(self):
        """SatelliteInfo should have version and update fields"""
        from ha_glue.services.satellite_manager import SatelliteCapabilities, SatelliteInfo, UpdateStatus

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
        from ha_glue.services.satellite_manager import SatelliteManager

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
        from ha_glue.services.satellite_manager import SatelliteCapabilities, SatelliteInfo, SatelliteManager

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
        from ha_glue.services.satellite_manager import SatelliteCapabilities, SatelliteInfo, SatelliteManager, UpdateStatus

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
        from ha_glue.services.satellite_manager import SatelliteCapabilities, SatelliteInfo, SatelliteManager, UpdateStatus

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
        from ha_glue.services.satellite_manager import SatelliteCapabilities, SatelliteInfo, SatelliteManager, UpdateStatus

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
    def test_get_latest_version_falls_back_to_config(self):
        """get_latest_version falls back to the config value when no source is bundled"""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        with patch('ha_glue.services.satellite_update_service.ha_glue_settings') as mock_settings:
            mock_settings.satellite_latest_version = "2.0.0"
            service = SatelliteUpdateService()
            service.satellite_source_path = _NO_SOURCE
            assert service.get_latest_version() == "2.0.0"

    @pytest.mark.unit
    def test_get_latest_version_reads_bundled_source(self, tmp_path):
        """get_latest_version reads __version__ from the bundled source, ignoring config drift"""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        # Lay out a fake bundled source: <root>/renfield_satellite/__init__.py
        pkg = tmp_path / "renfield_satellite"
        pkg.mkdir()
        (pkg / "__init__.py").write_text('"""doc"""\n__version__ = "9.9.9"\n')

        with patch('ha_glue.services.satellite_update_service.ha_glue_settings') as mock_settings:
            # Config says something stale — the source must win.
            mock_settings.satellite_latest_version = "1.0.0"
            service = SatelliteUpdateService()
            service.satellite_source_path = tmp_path
            assert service.get_latest_version() == "9.9.9"

    @pytest.mark.unit
    def test_read_source_version_handles_missing_marker(self, tmp_path):
        """_read_source_version returns None when __version__ is absent"""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        pkg = tmp_path / "renfield_satellite"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("# no version here\n")

        service = SatelliteUpdateService()
        service.satellite_source_path = tmp_path
        assert service._read_source_version() is None

    @pytest.mark.unit
    def test_read_source_version_tolerates_inline_comment(self, tmp_path):
        """An inline comment / extra whitespace must not corrupt the parsed version.

        Regression guard: the old end-strip parser turned `__version__ = "1.4.0"  # x`
        into `1.4.0"  # x`, which silently disabled OTA (non-numeric -> int() fails ->
        is_update_available False for every satellite).
        """
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        pkg = tmp_path / "renfield_satellite"
        pkg.mkdir()
        (pkg / "__init__.py").write_text('__version__ = "1.4.0"   # noqa: E501\n')

        service = SatelliteUpdateService()
        service.satellite_source_path = tmp_path
        assert service._read_source_version() == "1.4.0"

    @pytest.mark.unit
    def test_read_source_version_rejects_non_numeric(self, tmp_path):
        """A non-dotted-digit version is rejected (None) rather than poisoning compare."""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        pkg = tmp_path / "renfield_satellite"
        pkg.mkdir()
        (pkg / "__init__.py").write_text('__version__ = "1.4.0-dev/oops"\n')

        service = SatelliteUpdateService()
        service.satellite_source_path = tmp_path
        assert service._read_source_version() is None

    @pytest.mark.unit
    def test_read_source_version_ignores_version_info_sibling(self, tmp_path):
        """__version_info__ must not be mistaken for __version__."""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        pkg = tmp_path / "renfield_satellite"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            '__version_info__ = (1, 4, 0)\n__version__ = "1.4.0"\n'
        )

        service = SatelliteUpdateService()
        service.satellite_source_path = tmp_path
        assert service._read_source_version() == "1.4.0"

    @pytest.mark.unit
    def test_is_update_available_newer(self):
        """is_update_available should return True when newer version exists"""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        with patch('ha_glue.services.satellite_update_service.ha_glue_settings') as mock_settings:
            mock_settings.satellite_latest_version = "2.0.0"
            service = SatelliteUpdateService()
            service.satellite_source_path = _NO_SOURCE

            assert service.is_update_available("1.0.0") is True
            assert service.is_update_available("1.9.9") is True

    @pytest.mark.unit
    def test_is_update_available_same(self):
        """is_update_available should return False when same version"""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        with patch('ha_glue.services.satellite_update_service.ha_glue_settings') as mock_settings:
            mock_settings.satellite_latest_version = "1.0.0"
            service = SatelliteUpdateService()
            service.satellite_source_path = _NO_SOURCE

            assert service.is_update_available("1.0.0") is False

    @pytest.mark.unit
    def test_is_update_available_newer_current(self):
        """is_update_available should return False when current is newer"""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        with patch('ha_glue.services.satellite_update_service.ha_glue_settings') as mock_settings:
            mock_settings.satellite_latest_version = "1.0.0"
            service = SatelliteUpdateService()
            service.satellite_source_path = _NO_SOURCE

            assert service.is_update_available("2.0.0") is False

    @pytest.mark.unit
    def test_is_update_available_unknown(self):
        """is_update_available should return False for unknown version"""
        from ha_glue.services.satellite_update_service import SatelliteUpdateService

        with patch('ha_glue.services.satellite_update_service.ha_glue_settings') as mock_settings:
            mock_settings.satellite_latest_version = "1.0.0"
            service = SatelliteUpdateService()
            service.satellite_source_path = _NO_SOURCE

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
        from ha_glue.api.routes.satellites import _is_update_available

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
