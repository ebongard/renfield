"""
Integration Tests for Satellite OTA Update System

Tests the complete update flow including WebSocket communication.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient


# =============================================================================
# WebSocket Update Flow Integration Tests
# =============================================================================

class TestSatelliteUpdateWebSocketFlow:
    """Tests for the complete WebSocket update flow"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_update_progress_updates_satellite_status(self, async_client: AsyncClient):
        """update_progress message should update satellite's update status"""
        from services.satellite_manager import (
            SatelliteManager, SatelliteInfo, SatelliteCapabilities, UpdateStatus
        )

        manager = SatelliteManager()
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()

        # Register satellite with version
        manager.satellites["test-sat"] = SatelliteInfo(
            satellite_id="test-sat",
            room="Living Room",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0"
        )

        # Simulate update_progress message
        manager.set_update_status(
            "test-sat",
            UpdateStatus.IN_PROGRESS,
            stage="downloading",
            progress=25
        )

        sat = manager.get_satellite("test-sat")
        assert sat.update_status == UpdateStatus.IN_PROGRESS
        assert sat.update_stage == "downloading"
        assert sat.update_progress == 25

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_update_complete_updates_version(self, async_client: AsyncClient):
        """update_complete message should update satellite version"""
        from services.satellite_manager import (
            SatelliteManager, SatelliteInfo, SatelliteCapabilities, UpdateStatus
        )

        manager = SatelliteManager()
        mock_ws = MagicMock()

        # Register satellite with old version
        manager.satellites["test-sat"] = SatelliteInfo(
            satellite_id="test-sat",
            room="Living Room",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0",
            update_status=UpdateStatus.IN_PROGRESS,
            update_stage="restarting",
            update_progress=95
        )

        # Simulate update_complete - version updated via heartbeat
        manager.update_heartbeat("test-sat", None, "1.1.0")
        manager.set_update_status("test-sat", UpdateStatus.COMPLETED, stage="completed", progress=100)

        sat = manager.get_satellite("test-sat")
        assert sat.version == "1.1.0"
        assert sat.update_status == UpdateStatus.COMPLETED
        assert sat.update_progress == 100

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_update_failed_sets_error(self, async_client: AsyncClient):
        """update_failed message should set error status"""
        from services.satellite_manager import (
            SatelliteManager, SatelliteInfo, SatelliteCapabilities, UpdateStatus
        )

        manager = SatelliteManager()
        mock_ws = MagicMock()

        # Register satellite in updating state
        manager.satellites["test-sat"] = SatelliteInfo(
            satellite_id="test-sat",
            room="Living Room",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0",
            update_status=UpdateStatus.IN_PROGRESS,
            update_stage="installing",
            update_progress=75
        )

        # Simulate update_failed
        manager.set_update_status(
            "test-sat",
            UpdateStatus.FAILED,
            stage="installing",
            progress=75,
            error="Checksum verification failed"
        )

        sat = manager.get_satellite("test-sat")
        assert sat.update_status == UpdateStatus.FAILED
        assert sat.update_error == "Checksum verification failed"
        # Version should NOT change on failure
        assert sat.version == "1.0.0"


# =============================================================================
# API Update Flow Integration Tests
# =============================================================================

class TestSatelliteUpdateAPIFlow:
    """Tests for the API-driven update flow"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_full_update_flow_via_api(self, async_client: AsyncClient):
        """Test complete update flow: check version -> initiate -> progress -> complete"""
        from services.satellite_manager import (
            get_satellite_manager, SatelliteInfo, SatelliteCapabilities, UpdateStatus
        )

        manager = get_satellite_manager()
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()

        # Register satellite with old version
        manager.satellites["flow-test-sat"] = SatelliteInfo(
            satellite_id="flow-test-sat",
            room="Test Room",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0"
        )

        try:
            # Step 1: Check versions - should show update available
            with patch('services.satellite_update_service.settings') as mock_settings:
                mock_settings.satellite_latest_version = "2.0.0"

                response = await async_client.get("/api/satellites/versions")
                assert response.status_code == 200
                data = response.json()
                assert data["latest_version"] == "2.0.0"

                # Find our test satellite in the list
                test_sat = next(
                    (s for s in data["satellites"] if s["satellite_id"] == "flow-test-sat"),
                    None
                )
                assert test_sat is not None
                assert test_sat["version"] == "1.0.0"
                assert test_sat["update_available"] is True

            # Step 2: Get update status
            response = await async_client.get("/api/satellites/flow-test-sat/update-status")
            assert response.status_code == 200
            status_data = response.json()
            assert status_data["version"] == "1.0.0"
            assert status_data["update_status"] is None or status_data["update_status"] == "none"

            # Step 3: Simulate update progress (as if satellite sent update_progress)
            manager.set_update_status(
                "flow-test-sat",
                UpdateStatus.IN_PROGRESS,
                stage="downloading",
                progress=50
            )

            # Verify progress via API
            response = await async_client.get("/api/satellites/flow-test-sat/update-status")
            assert response.status_code == 200
            status_data = response.json()
            assert status_data["update_status"] == "in_progress"
            assert status_data["update_stage"] == "downloading"
            assert status_data["update_progress"] == 50

            # Step 4: Simulate update complete
            manager.update_heartbeat("flow-test-sat", None, "2.0.0")
            manager.set_update_status(
                "flow-test-sat",
                UpdateStatus.COMPLETED,
                stage="completed",
                progress=100
            )

            # Verify completion via API
            response = await async_client.get("/api/satellites/flow-test-sat/update-status")
            assert response.status_code == 200
            status_data = response.json()
            assert status_data["version"] == "2.0.0"
            assert status_data["update_status"] == "completed"
            assert status_data["update_progress"] == 100

        finally:
            # Cleanup
            if "flow-test-sat" in manager.satellites:
                del manager.satellites["flow-test-sat"]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_initiate_update_sends_websocket_message(self, async_client: AsyncClient):
        """POST /api/satellites/{id}/update should send update_request via WebSocket"""
        from services.satellite_manager import (
            get_satellite_manager, SatelliteInfo, SatelliteCapabilities
        )

        manager = get_satellite_manager()
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()

        # Register satellite
        manager.satellites["ws-test-sat"] = SatelliteInfo(
            satellite_id="ws-test-sat",
            room="Test Room",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0"
        )

        try:
            with patch('services.satellite_update_service.settings') as mock_settings:
                mock_settings.satellite_latest_version = "2.0.0"
                mock_settings.advertise_host = "localhost"
                mock_settings.advertise_port = 8000

                # Initiate update
                response = await async_client.post("/api/satellites/ws-test-sat/update")
                assert response.status_code == 200

                data = response.json()
                assert data["success"] is True
                assert data["target_version"] == "2.0.0"

                # Verify WebSocket message was sent
                mock_ws.send_json.assert_called_once()
                call_args = mock_ws.send_json.call_args[0][0]
                assert call_args["type"] == "update_request"
                assert call_args["target_version"] == "2.0.0"
                assert "package_url" in call_args
                assert "checksum" in call_args

        finally:
            # Cleanup
            if "ws-test-sat" in manager.satellites:
                del manager.satellites["ws-test-sat"]


# =============================================================================
# Version Comparison Integration Tests
# =============================================================================

class TestVersionComparisonIntegration:
    """Integration tests for version comparison across the system"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_satellites_endpoint_shows_update_available(self, async_client: AsyncClient):
        """GET /api/satellites should correctly show update_available for each satellite"""
        from services.satellite_manager import (
            get_satellite_manager, SatelliteInfo, SatelliteCapabilities
        )

        manager = get_satellite_manager()
        mock_ws = MagicMock()

        # Register satellites with different versions
        manager.satellites["sat-old"] = SatelliteInfo(
            satellite_id="sat-old",
            room="Room A",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0"
        )
        manager.satellites["sat-current"] = SatelliteInfo(
            satellite_id="sat-current",
            room="Room B",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="2.0.0"
        )
        manager.satellites["sat-unknown"] = SatelliteInfo(
            satellite_id="sat-unknown",
            room="Room C",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="unknown"
        )

        try:
            with patch('utils.config.settings') as mock_settings:
                mock_settings.satellite_latest_version = "2.0.0"

                response = await async_client.get("/api/satellites")
                assert response.status_code == 200

                data = response.json()
                satellites = {s["satellite_id"]: s for s in data["satellites"]}

                # Old version should have update available
                assert satellites["sat-old"]["update_available"] is True
                assert satellites["sat-old"]["version"] == "1.0.0"

                # Current version should NOT have update available
                assert satellites["sat-current"]["update_available"] is False
                assert satellites["sat-current"]["version"] == "2.0.0"

                # Unknown version should NOT have update available
                assert satellites["sat-unknown"]["update_available"] is False
                assert satellites["sat-unknown"]["version"] == "unknown"

        finally:
            # Cleanup
            for sat_id in ["sat-old", "sat-current", "sat-unknown"]:
                if sat_id in manager.satellites:
                    del manager.satellites[sat_id]


# =============================================================================
# Update Package Integration Tests
# =============================================================================

class TestUpdatePackageIntegration:
    """Integration tests for update package generation and download"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_update_package_endpoint_returns_tarball(self, async_client: AsyncClient):
        """GET /api/satellites/update-package should return a valid tarball"""
        import tempfile
        import tarfile
        from pathlib import Path

        with patch('services.satellite_update_service.settings') as mock_settings:
            mock_settings.satellite_latest_version = "1.0.0"

            # Create a mock package
            with tempfile.TemporaryDirectory() as tmpdir:
                package_path = Path(tmpdir) / "test-package.tar.gz"

                # Create a minimal tarball
                with tarfile.open(package_path, "w:gz") as tar:
                    # Add a dummy file
                    import io
                    data = b"test content"
                    info = tarfile.TarInfo(name="test.txt")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))

                # Mock the update service to return this package
                with patch('services.satellite_update_service.get_satellite_update_service') as mock_service:
                    mock_instance = MagicMock()
                    mock_instance.get_package_info.return_value = {
                        "path": str(package_path),
                        "version": "1.0.0",
                        "checksum": "sha256:abc123",
                        "size": package_path.stat().st_size
                    }
                    mock_service.return_value = mock_instance

                    response = await async_client.get("/api/satellites/update-package")
                    assert response.status_code == 200
                    assert response.headers["content-type"] == "application/gzip"
                    assert "X-Package-Version" in response.headers
                    assert response.headers["X-Package-Version"] == "1.0.0"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_update_package_not_available(self, async_client: AsyncClient):
        """GET /api/satellites/update-package should return 503 when package not available"""
        with patch('services.satellite_update_service.get_satellite_update_service') as mock_service:
            mock_instance = MagicMock()
            mock_instance.get_package_info.return_value = None
            mock_service.return_value = mock_instance

            response = await async_client.get("/api/satellites/update-package")
            assert response.status_code == 503


# =============================================================================
# Rollback Scenario Integration Tests
# =============================================================================

class TestRollbackScenarios:
    """Integration tests for rollback scenarios"""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_failed_update_preserves_original_version(self, async_client: AsyncClient):
        """When update fails, satellite should keep original version"""
        from services.satellite_manager import (
            get_satellite_manager, SatelliteInfo, SatelliteCapabilities, UpdateStatus
        )

        manager = get_satellite_manager()
        mock_ws = MagicMock()

        manager.satellites["rollback-test"] = SatelliteInfo(
            satellite_id="rollback-test",
            room="Test Room",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0"
        )

        try:
            # Start update
            manager.set_update_status(
                "rollback-test",
                UpdateStatus.IN_PROGRESS,
                stage="downloading",
                progress=25
            )

            # Progress to installing
            manager.set_update_status(
                "rollback-test",
                UpdateStatus.IN_PROGRESS,
                stage="installing",
                progress=75
            )

            # Simulate failure with rollback
            manager.set_update_status(
                "rollback-test",
                UpdateStatus.FAILED,
                stage="installing",
                progress=75,
                error="Installation failed: disk full"
            )

            # Version should still be original
            sat = manager.get_satellite("rollback-test")
            assert sat.version == "1.0.0"
            assert sat.update_status == UpdateStatus.FAILED
            assert sat.update_error == "Installation failed: disk full"

            # Verify via API
            response = await async_client.get("/api/satellites/rollback-test/update-status")
            assert response.status_code == 200
            data = response.json()
            assert data["version"] == "1.0.0"
            assert data["update_status"] == "failed"
            assert "disk full" in data["update_error"]

        finally:
            if "rollback-test" in manager.satellites:
                del manager.satellites["rollback-test"]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_clear_update_status_after_failure(self, async_client: AsyncClient):
        """After acknowledging failure, update status should be clearable"""
        from services.satellite_manager import (
            get_satellite_manager, SatelliteInfo, SatelliteCapabilities, UpdateStatus
        )

        manager = get_satellite_manager()
        mock_ws = MagicMock()

        manager.satellites["clear-test"] = SatelliteInfo(
            satellite_id="clear-test",
            room="Test Room",
            websocket=mock_ws,
            capabilities=SatelliteCapabilities(),
            version="1.0.0",
            update_status=UpdateStatus.FAILED,
            update_stage="installing",
            update_progress=75,
            update_error="Some error"
        )

        try:
            # Clear the failed status
            manager.clear_update_status("clear-test")

            sat = manager.get_satellite("clear-test")
            assert sat.update_status == UpdateStatus.NONE
            assert sat.update_stage is None
            assert sat.update_progress == 0
            assert sat.update_error is None

        finally:
            if "clear-test" in manager.satellites:
                del manager.satellites["clear-test"]
