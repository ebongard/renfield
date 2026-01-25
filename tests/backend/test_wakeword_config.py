"""
Tests for Wake Word Configuration Management

Tests the centralized wake word settings system including:
- WakeWordConfigManager service
- Settings API endpoints (GET/PUT)
- WebSocket broadcast functionality
- Permission checks
- Device sync status
- Model download endpoints
- config_ack handling
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from services.wakeword_config_manager import (
    WakeWordConfigManager,
    WakeWordConfig,
    DeviceSyncStatus,
    get_wakeword_config_manager,
    AVAILABLE_KEYWORDS,
    VALID_KEYWORDS,
)
from models.database import SystemSetting, SETTING_WAKEWORD_KEYWORD, User, Role
from models.permissions import Permission
from utils.config import settings


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """Create an admin user for tests"""
    # Create admin role with all permissions
    admin_role = Role(
        name="TestAdmin",
        description="Test admin role",
        permissions=[Permission.ADMIN.value, Permission.SETTINGS_MANAGE.value],
    )
    db_session.add(admin_role)
    await db_session.flush()

    # Create admin user
    from services.auth_service import get_password_hash
    admin = User(
        username="test_admin",
        password_hash=get_password_hash("admin123"),
        role_id=admin_role.id,
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    return admin


@pytest.fixture
async def regular_user(db_session: AsyncSession) -> User:
    """Create a regular user without admin permissions"""
    # Create regular role
    user_role = Role(
        name="TestUser",
        description="Test user role",
        permissions=[Permission.CHAT_OWN.value],
    )
    db_session.add(user_role)
    await db_session.flush()

    # Create regular user
    from services.auth_service import get_password_hash
    user = User(
        username="test_user",
        password_hash=get_password_hash("user123"),
        role_id=user_role.id,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def admin_auth_headers(async_client: AsyncClient, admin_user: User) -> dict:
    """Get auth headers for admin user"""
    original = settings.auth_enabled
    settings.auth_enabled = True
    try:
        response = await async_client.post(
            "/api/auth/login",
            data={"username": "test_admin", "password": "admin123"}
        )
        if response.status_code == 200:
            token = response.json()["access_token"]
            return {"Authorization": f"Bearer {token}"}
        else:
            pytest.fail(f"Login failed with status {response.status_code}: {response.text}")
    finally:
        settings.auth_enabled = original
    return {}


@pytest.fixture
async def user_auth_headers(async_client: AsyncClient, regular_user: User) -> dict:
    """Get auth headers for regular user"""
    original = settings.auth_enabled
    settings.auth_enabled = True
    try:
        response = await async_client.post(
            "/api/auth/login",
            data={"username": "test_user", "password": "user123"}
        )
        if response.status_code == 200:
            token = response.json()["access_token"]
            return {"Authorization": f"Bearer {token}"}
        else:
            pytest.fail(f"Login failed with status {response.status_code}: {response.text}")
    finally:
        settings.auth_enabled = original
    return {}


class TestWakeWordConfig:
    """Tests for WakeWordConfig dataclass"""

    def test_to_dict(self):
        """Test converting config to dictionary"""
        config = WakeWordConfig(
            keyword="alexa",
            threshold=0.5,
            cooldown_ms=2000,
            enabled=True
        )

        result = config.to_dict()

        assert result["keyword"] == "alexa"
        assert result["threshold"] == 0.5
        assert result["cooldown_ms"] == 2000
        assert result["enabled"] is True
        assert result["wake_words"] == ["alexa"]

    def test_to_satellite_config(self):
        """Test converting config to satellite format"""
        config = WakeWordConfig(
            keyword="hey_jarvis",
            threshold=0.7,
            cooldown_ms=3000
        )

        result = config.to_satellite_config()

        assert result["wake_words"] == ["hey_jarvis"]
        assert result["threshold"] == 0.7
        assert result["cooldown_ms"] == 3000


class TestDeviceSyncStatus:
    """Tests for DeviceSyncStatus dataclass"""

    def test_to_dict(self):
        """Test converting sync status to dictionary"""
        status = DeviceSyncStatus(
            device_id="satellite-living-room",
            device_type="satellite",
            synced=True,
            active_keywords=["alexa"],
            failed_keywords=[],
            last_ack_time=datetime(2026, 1, 25, 12, 0, 0),
            error=None
        )

        result = status.to_dict()

        assert result["device_id"] == "satellite-living-room"
        assert result["device_type"] == "satellite"
        assert result["synced"] is True
        assert result["active_keywords"] == ["alexa"]
        assert result["failed_keywords"] == []
        assert "2026-01-25" in result["last_ack_time"]
        assert result["error"] is None

    def test_to_dict_with_error(self):
        """Test sync status with error"""
        status = DeviceSyncStatus(
            device_id="satellite-kitchen",
            device_type="satellite",
            synced=False,
            active_keywords=[],
            failed_keywords=["hey_custom"],
            error="Model not found"
        )

        result = status.to_dict()

        assert result["synced"] is False
        assert result["failed_keywords"] == ["hey_custom"]
        assert result["error"] == "Model not found"


class TestWakeWordConfigManager:
    """Tests for WakeWordConfigManager service"""

    @pytest.fixture
    def manager(self):
        """Create a fresh manager for each test"""
        return WakeWordConfigManager()

    @pytest.mark.unit
    async def test_get_config_with_defaults(self, manager, db_session):
        """Test getting config when no DB settings exist (falls back to env)"""
        config = await manager.get_config(db_session)

        assert config.keyword in VALID_KEYWORDS
        assert 0.1 <= config.threshold <= 1.0
        assert config.cooldown_ms > 0

    @pytest.mark.unit
    async def test_get_config_from_db(self, manager, db_session):
        """Test getting config from database"""
        # Insert test setting
        setting = SystemSetting(
            key=SETTING_WAKEWORD_KEYWORD,
            value="hey_jarvis"
        )
        db_session.add(setting)
        await db_session.commit()

        config = await manager.get_config(db_session)

        assert config.keyword == "hey_jarvis"

    @pytest.mark.unit
    async def test_update_config_keyword(self, manager, db_session):
        """Test updating wake word keyword"""
        config = await manager.update_config(
            db_session,
            keyword="hey_mycroft"
        )

        assert config.keyword == "hey_mycroft"

        # Verify persisted to DB
        new_config = await manager.get_config(db_session)
        assert new_config.keyword == "hey_mycroft"

    @pytest.mark.unit
    async def test_update_config_invalid_keyword(self, manager, db_session):
        """Test that invalid keywords are rejected"""
        with pytest.raises(ValueError) as excinfo:
            await manager.update_config(
                db_session,
                keyword="invalid_keyword"
            )

        assert "Invalid keyword" in str(excinfo.value)

    @pytest.mark.unit
    async def test_update_config_threshold(self, manager, db_session):
        """Test updating detection threshold"""
        config = await manager.update_config(
            db_session,
            threshold=0.8
        )

        assert config.threshold == 0.8

    @pytest.mark.unit
    async def test_update_config_invalid_threshold(self, manager, db_session):
        """Test that invalid thresholds are rejected"""
        with pytest.raises(ValueError) as excinfo:
            await manager.update_config(
                db_session,
                threshold=1.5  # Too high
            )

        assert "Invalid threshold" in str(excinfo.value)

    @pytest.mark.unit
    async def test_update_config_cooldown(self, manager, db_session):
        """Test updating cooldown"""
        config = await manager.update_config(
            db_session,
            cooldown_ms=5000
        )

        assert config.cooldown_ms == 5000

    @pytest.mark.unit
    async def test_update_config_invalid_cooldown(self, manager, db_session):
        """Test that invalid cooldown values are rejected"""
        with pytest.raises(ValueError) as excinfo:
            await manager.update_config(
                db_session,
                cooldown_ms=100  # Too low
            )

        assert "Invalid cooldown" in str(excinfo.value)

    @pytest.mark.unit
    def test_subscribe_simple(self, manager):
        """Test subscribing a WebSocket without device info"""
        mock_ws = MagicMock()
        manager.subscribe(mock_ws)

        assert mock_ws in manager._subscribers
        assert manager.get_subscriber_count() == 1

    @pytest.mark.unit
    def test_subscribe_with_device_info(self, manager):
        """Test subscribing with device ID and type"""
        mock_ws = MagicMock()
        manager.subscribe(
            websocket=mock_ws,
            device_id="satellite-living-room",
            device_type="satellite"
        )

        assert mock_ws in manager._subscribers
        assert "satellite-living-room" in manager._device_sync_status
        assert manager._device_sync_status["satellite-living-room"].device_type == "satellite"

    @pytest.mark.unit
    def test_unsubscribe(self, manager):
        """Test unsubscribing a WebSocket"""
        mock_ws = MagicMock()
        manager.subscribe(mock_ws, device_id="test-device")
        manager.unsubscribe(mock_ws)

        assert mock_ws not in manager._subscribers
        assert manager.get_subscriber_count() == 0

    @pytest.mark.unit
    async def test_broadcast_config(self, manager):
        """Test broadcasting config to subscribers"""
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        manager.subscribe(mock_ws1, device_id="device-1")
        manager.subscribe(mock_ws2, device_id="device-2")

        config = WakeWordConfig(keyword="alexa", threshold=0.5, cooldown_ms=2000)
        await manager.broadcast_config(config)

        # Both should have received the message
        assert mock_ws1.send_json.called
        assert mock_ws2.send_json.called

        # Check message format
        call_args = mock_ws1.send_json.call_args[0][0]
        assert call_args["type"] == "config_update"
        assert call_args["config"]["wake_words"] == ["alexa"]
        assert "config_version" in call_args

    @pytest.mark.unit
    async def test_broadcast_increments_version(self, manager):
        """Test that broadcast increments config version"""
        mock_ws = AsyncMock()
        manager.subscribe(mock_ws)

        initial_version = manager._pending_config_version

        config = WakeWordConfig(keyword="alexa", threshold=0.5, cooldown_ms=2000)
        await manager.broadcast_config(config)

        assert manager._pending_config_version == initial_version + 1

    @pytest.mark.unit
    async def test_broadcast_marks_devices_pending(self, manager):
        """Test that broadcast marks all devices as pending sync"""
        mock_ws = AsyncMock()
        manager.subscribe(mock_ws, device_id="test-device", device_type="satellite")

        # Manually set synced
        manager._device_sync_status["test-device"].synced = True

        config = WakeWordConfig(keyword="alexa", threshold=0.5, cooldown_ms=2000)
        await manager.broadcast_config(config)

        # Should be marked as pending after broadcast
        assert manager._device_sync_status["test-device"].synced is False

    @pytest.mark.unit
    async def test_broadcast_removes_failed_subscribers(self, manager):
        """Test that failed subscribers are removed during broadcast"""
        mock_ws_good = AsyncMock()
        mock_ws_bad = AsyncMock()
        mock_ws_bad.send_json.side_effect = Exception("Connection lost")

        manager.subscribe(mock_ws_good)
        manager.subscribe(mock_ws_bad)

        config = WakeWordConfig(keyword="alexa", threshold=0.5, cooldown_ms=2000)
        await manager.broadcast_config(config)

        # Bad subscriber should be removed
        assert mock_ws_bad not in manager._subscribers
        assert mock_ws_good in manager._subscribers
        assert manager.get_subscriber_count() == 1

    @pytest.mark.unit
    def test_get_available_keywords(self, manager):
        """Test getting available keywords"""
        keywords = manager.get_available_keywords()

        assert len(keywords) > 0
        assert all("id" in kw and "label" in kw for kw in keywords)

    @pytest.mark.unit
    def test_handle_config_ack_success(self, manager):
        """Test handling successful config acknowledgment"""
        manager.subscribe(MagicMock(), device_id="satellite-1", device_type="satellite")

        status = manager.handle_config_ack(
            device_id="satellite-1",
            success=True,
            active_keywords=["alexa"],
            failed_keywords=[],
            error=None
        )

        assert status.synced is True
        assert status.active_keywords == ["alexa"]
        assert status.failed_keywords == []
        assert status.last_ack_time is not None

    @pytest.mark.unit
    def test_handle_config_ack_failure(self, manager):
        """Test handling failed config acknowledgment"""
        manager.subscribe(MagicMock(), device_id="satellite-1", device_type="satellite")

        status = manager.handle_config_ack(
            device_id="satellite-1",
            success=False,
            active_keywords=["alexa"],
            failed_keywords=["hey_custom"],
            error="Model not found"
        )

        assert status.synced is False
        assert status.active_keywords == ["alexa"]
        assert status.failed_keywords == ["hey_custom"]
        assert status.error == "Model not found"

    @pytest.mark.unit
    def test_handle_config_ack_unknown_device(self, manager):
        """Test handling ack from unknown device creates new status"""
        status = manager.handle_config_ack(
            device_id="unknown-device",
            success=True,
            active_keywords=["alexa"]
        )

        assert "unknown-device" in manager._device_sync_status
        assert status.synced is True

    @pytest.mark.unit
    def test_get_device_sync_status_all(self, manager):
        """Test getting sync status for all devices"""
        manager.subscribe(MagicMock(), device_id="device-1", device_type="satellite")
        manager.subscribe(MagicMock(), device_id="device-2", device_type="web_device")

        manager.handle_config_ack("device-1", True, ["alexa"])
        manager.handle_config_ack("device-2", False, [], ["hey_jarvis"], "Download failed")

        status = manager.get_device_sync_status()

        assert status["synced_count"] == 1
        assert status["pending_count"] == 1
        assert status["all_synced"] is False
        assert len(status["devices"]) == 2

    @pytest.mark.unit
    def test_get_device_sync_status_single(self, manager):
        """Test getting sync status for single device"""
        manager.subscribe(MagicMock(), device_id="device-1", device_type="satellite")
        manager.handle_config_ack("device-1", True, ["alexa"])

        status = manager.get_device_sync_status("device-1")

        assert status["device_id"] == "device-1"
        assert status["synced"] is True

    @pytest.mark.unit
    def test_get_device_sync_status_not_found(self, manager):
        """Test getting sync status for non-existent device"""
        status = manager.get_device_sync_status("non-existent")

        assert "error" in status

    @pytest.mark.unit
    def test_get_device_by_websocket(self, manager):
        """Test getting device ID by websocket"""
        mock_ws = MagicMock()
        manager.subscribe(mock_ws, device_id="my-device", device_type="satellite")

        device_id = manager.get_device_by_websocket(mock_ws)

        assert device_id == "my-device"

    @pytest.mark.unit
    def test_get_device_by_websocket_not_found(self, manager):
        """Test getting device ID for unknown websocket"""
        mock_ws = MagicMock()
        device_id = manager.get_device_by_websocket(mock_ws)

        assert device_id is None


class TestWakeWordSettingsAPI:
    """Tests for Settings API endpoints"""

    @pytest.mark.integration
    async def test_get_wakeword_settings(self, async_client: AsyncClient):
        """Test GET /api/settings/wakeword"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()

        assert "keyword" in data
        assert "threshold" in data
        assert "cooldown_ms" in data
        assert "available_keywords" in data
        assert "enabled" in data

    @pytest.mark.integration
    async def test_get_wakeword_settings_available_keywords(self, async_client: AsyncClient):
        """Test that available keywords are returned"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()

        assert len(data["available_keywords"]) > 0
        for kw in data["available_keywords"]:
            assert "id" in kw
            assert "label" in kw

    @pytest.mark.integration
    async def test_put_wakeword_settings_unauthorized(
        self,
        async_client: AsyncClient,
        admin_user: User,
    ):
        """Test that PUT requires authentication when auth is enabled"""
        original = settings.auth_enabled
        settings.auth_enabled = True
        try:
            response = await async_client.put(
                "/api/settings/wakeword",
                json={"keyword": "hey_jarvis"}
            )

            # Should require authentication
            assert response.status_code in [401, 403]
        finally:
            settings.auth_enabled = original

    @pytest.mark.integration
    async def test_put_wakeword_settings_as_admin(
        self,
        async_client: AsyncClient,
        admin_auth_headers
    ):
        """Test PUT /api/settings/wakeword as admin"""
        original = settings.auth_enabled
        settings.auth_enabled = True
        try:
            response = await async_client.put(
                "/api/settings/wakeword",
                json={"keyword": "hey_jarvis", "threshold": 0.6},
                headers=admin_auth_headers
            )

            assert response.status_code == 200
            data = response.json()

            assert data["keyword"] == "hey_jarvis"
            assert data["threshold"] == 0.6
        finally:
            settings.auth_enabled = original

    @pytest.mark.integration
    async def test_put_wakeword_settings_forbidden(
        self,
        async_client: AsyncClient,
        user_auth_headers
    ):
        """Test that regular users cannot update settings"""
        original = settings.auth_enabled
        settings.auth_enabled = True
        try:
            response = await async_client.put(
                "/api/settings/wakeword",
                json={"keyword": "hey_jarvis"},
                headers=user_auth_headers
            )

            assert response.status_code == 403
        finally:
            settings.auth_enabled = original

    @pytest.mark.integration
    async def test_put_wakeword_settings_invalid_keyword(
        self,
        async_client: AsyncClient,
        admin_auth_headers
    ):
        """Test that invalid keywords are rejected"""
        original = settings.auth_enabled
        settings.auth_enabled = True
        try:
            response = await async_client.put(
                "/api/settings/wakeword",
                json={"keyword": "invalid_keyword"},
                headers=admin_auth_headers
            )

            assert response.status_code == 400
        finally:
            settings.auth_enabled = original

    @pytest.mark.integration
    async def test_put_wakeword_settings_invalid_threshold(
        self,
        async_client: AsyncClient,
        admin_auth_headers
    ):
        """Test that invalid thresholds are rejected"""
        original = settings.auth_enabled
        settings.auth_enabled = True
        try:
            response = await async_client.put(
                "/api/settings/wakeword",
                json={"threshold": 1.5},
                headers=admin_auth_headers
            )

            assert response.status_code == 422  # Pydantic validation
        finally:
            settings.auth_enabled = original

    @pytest.mark.integration
    async def test_put_wakeword_settings_empty_body(
        self,
        async_client: AsyncClient,
        admin_auth_headers
    ):
        """Test that empty body is rejected"""
        original = settings.auth_enabled
        settings.auth_enabled = True
        try:
            response = await async_client.put(
                "/api/settings/wakeword",
                json={},
                headers=admin_auth_headers
            )

            assert response.status_code == 400
            assert "At least one field" in response.json()["detail"]
        finally:
            settings.auth_enabled = original

    @pytest.mark.integration
    async def test_put_wakeword_settings_partial_update(
        self,
        async_client: AsyncClient,
        admin_auth_headers
    ):
        """Test that partial updates work"""
        original = settings.auth_enabled
        settings.auth_enabled = True
        try:
            # First get current settings
            get_response = await async_client.get("/api/settings/wakeword")
            original_threshold = get_response.json()["threshold"]

            # Update only keyword
            response = await async_client.put(
                "/api/settings/wakeword",
                json={"keyword": "hey_mycroft"},
                headers=admin_auth_headers
            )

            assert response.status_code == 200
            data = response.json()

            # Keyword should be updated
            assert data["keyword"] == "hey_mycroft"
        finally:
            settings.auth_enabled = original


class TestDeviceSyncStatusAPI:
    """Tests for device sync status API endpoints"""

    @pytest.mark.integration
    async def test_get_sync_status_empty(self, async_client: AsyncClient):
        """Test GET sync status when no devices connected"""
        response = await async_client.get("/api/settings/wakeword/sync-status")

        assert response.status_code == 200
        data = response.json()

        assert "devices" in data
        assert "config_version" in data
        assert "synced_count" in data
        assert "pending_count" in data

    @pytest.mark.integration
    async def test_get_single_device_sync_status_not_found(self, async_client: AsyncClient):
        """Test GET sync status for non-existent device"""
        response = await async_client.get("/api/settings/wakeword/sync-status/non-existent-device")

        assert response.status_code == 404


class TestModelDownloadAPI:
    """Tests for wake word model download endpoints"""

    @pytest.mark.integration
    async def test_list_available_models(self, async_client: AsyncClient):
        """Test GET /api/settings/wakeword/models"""
        response = await async_client.get("/api/settings/wakeword/models")

        assert response.status_code == 200
        data = response.json()

        assert "models" in data
        assert "base_url" in data

        # Check model structure
        for model in data["models"]:
            assert "model_id" in model
            assert "label" in model
            assert "available" in model

    @pytest.mark.integration
    async def test_download_model_invalid_id(self, async_client: AsyncClient):
        """Test downloading with invalid model ID"""
        response = await async_client.get("/api/settings/wakeword/models/invalid_model")

        assert response.status_code == 400
        assert "Invalid model_id" in response.json()["detail"]

    @pytest.mark.integration
    async def test_download_model_valid_id_not_found(self, async_client: AsyncClient):
        """Test downloading valid model ID but file not found"""
        # This may return 404 or 200 depending on whether models are installed
        response = await async_client.get("/api/settings/wakeword/models/alexa")

        # Either file is found or not found - both are valid responses
        assert response.status_code in [200, 404]


class TestWakeWordConfigSingleton:
    """Tests for singleton pattern"""

    @pytest.mark.unit
    def test_get_wakeword_config_manager_returns_same_instance(self):
        """Test that get_wakeword_config_manager returns singleton"""
        manager1 = get_wakeword_config_manager()
        manager2 = get_wakeword_config_manager()

        assert manager1 is manager2
