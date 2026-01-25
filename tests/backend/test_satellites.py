"""
Tests for Satellite Monitoring API

Tests the satellite API endpoints for monitoring and debugging
satellite voice assistants.
"""
import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch
from httpx import AsyncClient
from datetime import datetime

from services.satellite_manager import (
    SatelliteManager,
    SatelliteInfo,
    SatelliteCapabilities,
    SatelliteState,
    SatelliteSession,
    SatelliteMetrics,
    get_satellite_manager,
)


# =============================================================================
# SatelliteManager Tests
# =============================================================================

class TestSatelliteManager:
    """Tests for SatelliteManager service"""

    @pytest.fixture
    def manager(self):
        """Create a fresh SatelliteManager instance"""
        return SatelliteManager()

    @pytest.mark.unit
    async def test_register_satellite(self, manager):
        """Test registering a new satellite"""
        mock_ws = AsyncMock()

        result = await manager.register(
            satellite_id="test-satellite",
            room="Living Room",
            websocket=mock_ws,
            capabilities={"local_wakeword": True, "speaker": True},
            language="de"
        )

        assert result is True
        assert "test-satellite" in manager.satellites
        assert manager.satellites["test-satellite"].room == "Living Room"
        assert manager.satellites["test-satellite"].language == "de"

    @pytest.mark.unit
    async def test_register_reconnect(self, manager):
        """Test reconnecting an existing satellite"""
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()

        await manager.register("sat-1", "Room A", mock_ws1, {})
        await manager.register("sat-1", "Room B", mock_ws2, {})

        # Should have updated the satellite
        assert manager.satellites["sat-1"].room == "Room B"
        assert manager.satellites["sat-1"].websocket == mock_ws2

    @pytest.mark.unit
    async def test_unregister_satellite(self, manager):
        """Test unregistering a satellite"""
        mock_ws = AsyncMock()
        await manager.register("sat-1", "Room", mock_ws, {})

        await manager.unregister("sat-1")

        assert "sat-1" not in manager.satellites

    @pytest.mark.unit
    async def test_start_session(self, manager):
        """Test starting a voice session"""
        mock_ws = AsyncMock()
        await manager.register("sat-1", "Room", mock_ws, {})

        session_id = await manager.start_session(
            satellite_id="sat-1",
            keyword="alexa",
            confidence=0.85
        )

        assert session_id is not None
        assert session_id in manager.sessions
        assert manager.satellites["sat-1"].state == SatelliteState.LISTENING

    @pytest.mark.unit
    async def test_start_session_unknown_satellite(self, manager):
        """Test starting session for unknown satellite"""
        session_id = await manager.start_session(
            satellite_id="unknown",
            keyword="alexa",
            confidence=0.85
        )

        assert session_id is None

    @pytest.mark.unit
    async def test_update_heartbeat_with_metrics(self, manager):
        """Test updating heartbeat with metrics"""
        mock_ws = AsyncMock()
        await manager.register("sat-1", "Room", mock_ws, {})

        metrics = {
            "audio_rms": 1234.5,
            "audio_db": -18.3,
            "is_speech": True,
            "cpu_percent": 45.2,
            "memory_percent": 60.1,
            "temperature": 52.3,
        }

        manager.update_heartbeat("sat-1", metrics)

        sat = manager.satellites["sat-1"]
        assert sat.metrics["audio_rms"] == 1234.5
        assert sat.metrics["audio_db"] == -18.3
        assert sat.metrics["is_speech"] is True
        assert sat.metrics["cpu_percent"] == 45.2

    @pytest.mark.unit
    async def test_update_heartbeat_without_metrics(self, manager):
        """Test updating heartbeat without metrics"""
        mock_ws = AsyncMock()
        await manager.register("sat-1", "Room", mock_ws, {})

        old_time = manager.satellites["sat-1"].last_heartbeat
        time.sleep(0.01)  # Small delay

        manager.update_heartbeat("sat-1")

        assert manager.satellites["sat-1"].last_heartbeat > old_time

    @pytest.mark.unit
    def test_get_all_satellites(self, manager):
        """Test getting all satellite status"""
        # Register satellites synchronously by adding directly
        manager.satellites["sat-1"] = SatelliteInfo(
            satellite_id="sat-1",
            room="Room A",
            websocket=MagicMock(),
            capabilities=SatelliteCapabilities()
        )
        manager.satellites["sat-2"] = SatelliteInfo(
            satellite_id="sat-2",
            room="Room B",
            websocket=MagicMock(),
            capabilities=SatelliteCapabilities()
        )

        result = manager.get_all_satellites()

        assert len(result) == 2
        sat_ids = [s["satellite_id"] for s in result]
        assert "sat-1" in sat_ids
        assert "sat-2" in sat_ids

    @pytest.mark.unit
    def test_event_tracking(self, manager):
        """Test that events are tracked in history"""
        manager._add_event("sat-1", "test_event", {"key": "value"})

        assert "sat-1" in manager._satellite_history
        assert len(manager._satellite_history["sat-1"]) == 1
        assert manager._satellite_history["sat-1"][0]["type"] == "test_event"

    @pytest.mark.unit
    def test_stats_tracking(self, manager):
        """Test session statistics tracking"""
        manager._update_stats("sat-1", 5.0, True)
        manager._update_stats("sat-1", 3.0, True)
        manager._update_stats("sat-1", 4.0, False)

        stats = manager._satellite_stats["sat-1"]
        assert stats["total_sessions"] == 3
        assert stats["successful_sessions"] == 2
        assert stats["failed_sessions"] == 1
        assert stats["avg_duration"] == 4.0  # (5+3+4)/3


# =============================================================================
# Satellite API Tests
# =============================================================================

class TestSatelliteAPI:
    """Tests for Satellite API endpoints"""

    @pytest.mark.integration
    async def test_list_satellites_empty(self, async_client: AsyncClient):
        """Test listing satellites when none connected"""
        # Clear any existing satellites
        manager = get_satellite_manager()
        manager.satellites.clear()

        response = await async_client.get("/api/satellites")

        assert response.status_code == 200
        data = response.json()
        assert "satellites" in data
        assert "total_count" in data
        assert data["total_count"] == 0

    @pytest.mark.integration
    async def test_list_satellites_with_data(self, async_client: AsyncClient):
        """Test listing satellites with connected satellites"""
        manager = get_satellite_manager()
        manager.satellites.clear()

        # Add a test satellite
        manager.satellites["test-sat"] = SatelliteInfo(
            satellite_id="test-sat",
            room="Test Room",
            websocket=MagicMock(),
            capabilities=SatelliteCapabilities()
        )

        response = await async_client.get("/api/satellites")

        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 1
        assert data["satellites"][0]["satellite_id"] == "test-sat"
        assert data["satellites"][0]["room"] == "Test Room"

        # Cleanup
        manager.satellites.clear()

    @pytest.mark.integration
    async def test_get_satellite_not_found(self, async_client: AsyncClient):
        """Test getting non-existent satellite"""
        manager = get_satellite_manager()
        manager.satellites.clear()

        response = await async_client.get("/api/satellites/non-existent")

        assert response.status_code == 404

    @pytest.mark.integration
    async def test_get_satellite_success(self, async_client: AsyncClient):
        """Test getting existing satellite"""
        manager = get_satellite_manager()
        manager.satellites.clear()

        manager.satellites["my-sat"] = SatelliteInfo(
            satellite_id="my-sat",
            room="Living Room",
            websocket=MagicMock(),
            capabilities=SatelliteCapabilities(led_count=5),
            state=SatelliteState.IDLE
        )

        response = await async_client.get("/api/satellites/my-sat")

        assert response.status_code == 200
        data = response.json()
        assert data["satellite_id"] == "my-sat"
        assert data["room"] == "Living Room"
        assert data["state"] == "idle"
        assert data["capabilities"]["led_count"] == 5

        # Cleanup
        manager.satellites.clear()

    @pytest.mark.integration
    async def test_get_satellite_metrics(self, async_client: AsyncClient):
        """Test getting satellite metrics"""
        manager = get_satellite_manager()
        manager.satellites.clear()

        sat = SatelliteInfo(
            satellite_id="metrics-sat",
            room="Room",
            websocket=MagicMock(),
            capabilities=SatelliteCapabilities()
        )
        sat.metrics = {
            "audio_rms": 500.0,
            "cpu_percent": 30.0
        }
        manager.satellites["metrics-sat"] = sat

        response = await async_client.get("/api/satellites/metrics-sat/metrics")

        assert response.status_code == 200
        data = response.json()
        assert data["audio_rms"] == 500.0
        assert data["cpu_percent"] == 30.0

        # Cleanup
        manager.satellites.clear()

    @pytest.mark.integration
    async def test_get_satellite_session_no_session(self, async_client: AsyncClient):
        """Test getting session when none active"""
        manager = get_satellite_manager()
        manager.satellites.clear()

        manager.satellites["no-session-sat"] = SatelliteInfo(
            satellite_id="no-session-sat",
            room="Room",
            websocket=MagicMock(),
            capabilities=SatelliteCapabilities()
        )

        response = await async_client.get("/api/satellites/no-session-sat/session")

        assert response.status_code == 404

        # Cleanup
        manager.satellites.clear()

    @pytest.mark.integration
    async def test_get_satellite_history(self, async_client: AsyncClient):
        """Test getting satellite event history"""
        manager = get_satellite_manager()
        manager.satellites.clear()

        manager.satellites["history-sat"] = SatelliteInfo(
            satellite_id="history-sat",
            room="Room",
            websocket=MagicMock(),
            capabilities=SatelliteCapabilities()
        )

        # Add some events
        manager._add_event("history-sat", "connected", {"room": "Room"})
        manager._add_event("history-sat", "session_start", {"keyword": "alexa"})

        response = await async_client.get("/api/satellites/history-sat/history")

        assert response.status_code == 200
        data = response.json()
        assert data["satellite_id"] == "history-sat"
        assert len(data["events"]) == 2

        # Cleanup
        manager.satellites.clear()
        manager._satellite_history.clear()

    @pytest.mark.integration
    async def test_ping_satellite_not_found(self, async_client: AsyncClient):
        """Test pinging non-existent satellite"""
        manager = get_satellite_manager()
        manager.satellites.clear()

        response = await async_client.post("/api/satellites/non-existent/ping")

        assert response.status_code == 404

        # Cleanup
        manager.satellites.clear()


# =============================================================================
# SatelliteMetrics Dataclass Tests
# =============================================================================

class TestSatelliteMetrics:
    """Tests for SatelliteMetrics dataclass"""

    @pytest.mark.unit
    def test_default_values(self):
        """Test default metric values"""
        metrics = SatelliteMetrics()

        assert metrics.audio_rms is None
        assert metrics.audio_db is None
        assert metrics.is_speech is None
        assert metrics.session_count_1h == 0
        assert metrics.error_count_1h == 0

    @pytest.mark.unit
    def test_custom_values(self):
        """Test metrics with custom values"""
        metrics = SatelliteMetrics(
            audio_rms=1000.0,
            audio_db=-20.0,
            is_speech=True,
            cpu_percent=50.0,
            temperature=55.0
        )

        assert metrics.audio_rms == 1000.0
        assert metrics.audio_db == -20.0
        assert metrics.is_speech is True
        assert metrics.cpu_percent == 50.0
        assert metrics.temperature == 55.0


# =============================================================================
# Extended Heartbeat Tests
# =============================================================================

class TestExtendedHeartbeat:
    """Tests for extended heartbeat with metrics"""

    @pytest.mark.unit
    async def test_heartbeat_stores_metrics(self):
        """Test that heartbeat message stores metrics correctly"""
        manager = SatelliteManager()
        mock_ws = AsyncMock()

        await manager.register("test-sat", "Room", mock_ws, {})

        metrics = {
            "audio_rms": 2000.0,
            "audio_db": -15.5,
            "is_speech": False,
            "cpu_percent": 35.0,
            "memory_percent": 45.0,
            "temperature": 48.0,
            "last_wakeword": {
                "keyword": "hey_jarvis",
                "confidence": 0.92,
                "timestamp": time.time()
            },
            "session_count_1h": 5,
            "error_count_1h": 1
        }

        manager.update_heartbeat("test-sat", metrics)

        sat = manager.satellites["test-sat"]
        assert sat.metrics["audio_rms"] == 2000.0
        assert sat.metrics["is_speech"] is False
        assert sat.metrics["last_wakeword"]["keyword"] == "hey_jarvis"
        assert sat.metrics["session_count_1h"] == 5

    @pytest.mark.unit
    async def test_heartbeat_creates_event(self):
        """Test that heartbeat creates history event"""
        manager = SatelliteManager()
        mock_ws = AsyncMock()

        await manager.register("event-sat", "Room", mock_ws, {})

        manager.update_heartbeat("event-sat", {"audio_rms": 100.0})

        assert "event-sat" in manager._satellite_history
        events = manager._satellite_history["event-sat"]
        # Should have connected event and heartbeat event
        assert len(events) >= 1
        heartbeat_events = [e for e in events if e["type"] == "heartbeat"]
        assert len(heartbeat_events) >= 1
