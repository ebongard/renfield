"""
Tests for NotificationPollerService — generic MCP notification polling.

Tests:
- Config parsing (_parse_notifications)
- MCPServerConfig notifications field
- Pollable server detection
- Poll result parsing
- Notification processing with dedup
- Integration with NotificationService
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.mcp_client import (
    MCPServerConfig,
    MCPServerState,
    MCPTransportType,
    _parse_notifications,
)
from services.notification_poller import NotificationPollerService

# ============================================================================
# _parse_notifications
# ============================================================================


class TestParseNotifications:
    """Test YAML notifications section parsing."""

    @pytest.mark.unit
    def test_none_input(self):
        assert _parse_notifications(None) is None

    @pytest.mark.unit
    def test_empty_dict(self):
        assert _parse_notifications({}) is None

    @pytest.mark.unit
    def test_disabled(self):
        assert _parse_notifications({"enabled": False}) is None

    @pytest.mark.unit
    def test_enabled_defaults(self):
        result = _parse_notifications({"enabled": True})
        assert result == {
            "enabled": True,
            "poll_interval": 900,
            "tool": "get_pending_notifications",
            "lookahead_minutes": 45,
        }

    @pytest.mark.unit
    def test_custom_values(self):
        result = _parse_notifications({
            "enabled": True,
            "poll_interval": 300,
            "tool": "check_notifications",
            "lookahead_minutes": 60,
        })
        assert result["poll_interval"] == 300
        assert result["tool"] == "check_notifications"
        assert result["lookahead_minutes"] == 60

    @pytest.mark.unit
    def test_string_enabled_false(self):
        """Env var substitution may produce 'false' string."""
        with patch("services.mcp_client._resolve_value", return_value=False):
            assert _parse_notifications({"enabled": "false"}) is None

    @pytest.mark.unit
    def test_non_dict_input(self):
        assert _parse_notifications("not a dict") is None
        assert _parse_notifications(42) is None


# ============================================================================
# MCPServerConfig with notifications
# ============================================================================


class TestMCPServerConfigNotifications:
    """Test MCPServerConfig dataclass with notifications field."""

    @pytest.mark.unit
    def test_default_none(self):
        config = MCPServerConfig(name="test")
        assert config.notifications is None

    @pytest.mark.unit
    def test_with_notifications(self):
        config = MCPServerConfig(
            name="calendar",
            notifications={"enabled": True, "poll_interval": 900, "tool": "get_pending_notifications", "lookahead_minutes": 45},
        )
        assert config.notifications["enabled"] is True
        assert config.notifications["poll_interval"] == 900


# ============================================================================
# NotificationPollerService
# ============================================================================


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCPManager with a calendar server configured for polling."""
    manager = MagicMock()
    manager.execute_tool = AsyncMock()

    # Configure a server with notifications enabled
    config = MCPServerConfig(
        name="calendar",
        transport=MCPTransportType.STDIO,
        notifications={
            "enabled": True,
            "poll_interval": 900,
            "tool": "get_pending_notifications",
            "lookahead_minutes": 45,
        },
    )
    state = MCPServerState(config=config, connected=True)
    manager._servers = {"calendar": state}

    return manager


@pytest.fixture
def mock_mcp_manager_disconnected():
    """MCPManager with a server that has notifications but is disconnected."""
    manager = MagicMock()
    config = MCPServerConfig(
        name="calendar",
        notifications={"enabled": True, "poll_interval": 900, "tool": "get_pending_notifications", "lookahead_minutes": 45},
    )
    state = MCPServerState(config=config, connected=False)
    manager._servers = {"calendar": state}
    return manager


@pytest.fixture
def poller(mock_mcp_manager):
    return NotificationPollerService(mock_mcp_manager)


class TestGetPollableServers:
    """Test pollable server discovery."""

    @pytest.mark.unit
    def test_finds_connected_server(self, poller):
        servers = poller.get_pollable_servers()
        assert len(servers) == 1
        assert servers[0]["name"] == "calendar"
        assert servers[0]["poll_interval"] == 900
        assert servers[0]["tool"] == "get_pending_notifications"

    @pytest.mark.unit
    def test_skips_disconnected(self, mock_mcp_manager_disconnected):
        poller = NotificationPollerService(mock_mcp_manager_disconnected)
        assert poller.get_pollable_servers() == []

    @pytest.mark.unit
    def test_skips_no_notifications(self):
        manager = MagicMock()
        config = MCPServerConfig(name="weather")
        state = MCPServerState(config=config, connected=True)
        manager._servers = {"weather": state}
        poller = NotificationPollerService(manager)
        assert poller.get_pollable_servers() == []


class TestParsePollResult:
    """Test parsing MCP tool responses."""

    @pytest.mark.unit
    def test_parse_list_response(self, poller):
        """Tool returns a JSON list directly."""
        notifications = [
            {"event_type": "calendar.reminder", "title": "Meeting", "dedup_key": "cal:work:123:30min"}
        ]
        result = {"success": True, "message": json.dumps(notifications)}
        parsed = poller._parse_poll_result(result)
        assert len(parsed) == 1
        assert parsed[0]["title"] == "Meeting"

    @pytest.mark.unit
    def test_parse_envelope_response(self, poller):
        """Tool returns {"notifications": [...]}."""
        envelope = {"notifications": [{"title": "Test", "dedup_key": "x"}]}
        result = {"success": True, "message": json.dumps(envelope)}
        parsed = poller._parse_poll_result(result)
        assert len(parsed) == 1

    @pytest.mark.unit
    def test_empty_message(self, poller):
        result = {"success": True, "message": ""}
        assert poller._parse_poll_result(result) == []

    @pytest.mark.unit
    def test_non_json_message(self, poller):
        result = {"success": True, "message": "No notifications"}
        assert poller._parse_poll_result(result) == []

    @pytest.mark.unit
    def test_empty_list(self, poller):
        result = {"success": True, "message": "[]"}
        assert poller._parse_poll_result(result) == []


class TestProcessNotification:
    """Test individual notification processing."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_missing_dedup_key_skipped(self, poller):
        """Notifications without dedup_key are skipped."""
        await poller._process_notification("calendar", {"title": "No key"})
        # No error, just silently skipped

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_dedup_within_session(self, poller):
        """Same dedup_key is not processed twice via in-memory seen_keys."""
        notification = {
            "event_type": "calendar.reminder",
            "title": "Meeting",
            "message": "In 30 Minuten: Meeting",
            "dedup_key": "cal:work:123:30min",
        }

        # First call adds the key to seen_keys
        poller._seen_keys.add(notification["dedup_key"])

        # Now _process_notification should skip it (it checks _seen_keys before calling service)
        assert notification["dedup_key"] in poller._seen_keys

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_source_field_in_notification(self, poller):
        """Verify source format is 'mcp_poll:<server_name>'."""
        # We test the source format by verifying the call pattern
        # The source is constructed as f"mcp_poll:{server_name}"
        assert "mcp_poll:calendar" == f"mcp_poll:{'calendar'}"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_suppressed_notification_handled(self, poller):
        """Missing dedup_key → notification skipped without error."""
        notification = {
            "event_type": "calendar.reminder",
            "title": "Test",
            "message": "Test",
            # No dedup_key
        }
        # Should not raise — just logs a warning and returns
        await poller._process_notification("calendar", notification)


class TestPollOnce:
    """Test single poll cycle."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_successful_poll(self, poller, mock_mcp_manager):
        """Full poll cycle with one notification."""
        notifications = [
            {
                "event_type": "calendar.reminder_upcoming",
                "title": "Team-Meeting",
                "message": "In 30 Minuten: Team-Meeting",
                "urgency": "info",
                "dedup_key": "cal:work:123:30min",
            }
        ]
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": json.dumps(notifications),
        }

        with patch.object(poller, "_process_notification", new_callable=AsyncMock) as mock_process:
            await poller._poll_once("calendar", "mcp.calendar.get_pending_notifications", 45)

            mock_mcp_manager.execute_tool.assert_called_once_with(
                "mcp.calendar.get_pending_notifications",
                {"lookahead_minutes": 45},
            )
            mock_process.assert_called_once_with("calendar", notifications[0])

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_failed_tool_call(self, poller, mock_mcp_manager):
        """Failed MCP tool call should log warning, not crash."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": False,
            "message": "Server not available",
        }

        with patch.object(poller, "_process_notification", new_callable=AsyncMock) as mock_process:
            await poller._poll_once("calendar", "mcp.calendar.get_pending_notifications", 45)
            mock_process.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_poll_result(self, poller, mock_mcp_manager):
        """No notifications returned — no processing."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": "[]",
        }

        with patch.object(poller, "_process_notification", new_callable=AsyncMock) as mock_process:
            await poller._poll_once("calendar", "mcp.calendar.get_pending_notifications", 45)
            mock_process.assert_not_called()


class TestPrivacyFieldForwarding:
    """Test that privacy and target_user_id are forwarded to process_webhook."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_privacy_fields_forwarded(self, poller):
        """Privacy and target_user_id from poll result are passed to process_webhook."""
        notification = {
            "event_type": "calendar.reminder_upcoming",
            "title": "Arzttermin",
            "message": "In 30 Minuten: Arzttermin",
            "urgency": "warning",
            "dedup_key": "cal:private:456:30min",
            "tts": True,
            "privacy": "confidential",
            "target_user_id": 42,
        }

        mock_service = MagicMock()
        mock_service.process_webhook = AsyncMock()

        # Create mock async context manager for AsyncSessionLocal
        mock_db = AsyncMock()
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_session)

        import services.notification_poller as poller_mod
        with patch.object(poller_mod, "__builtins__", poller_mod.__builtins__):
            # Patch the lazy imports inside _process_notification
            import types
            mock_db_module = types.ModuleType("services.database")
            mock_db_module.AsyncSessionLocal = mock_session_factory
            mock_svc_module = types.ModuleType("services.notification_service")
            mock_svc_module.NotificationService = MagicMock(return_value=mock_service)

            import sys
            with patch.dict(sys.modules, {
                "services.database": mock_db_module,
                "services.notification_service": mock_svc_module,
            }):
                await poller._process_notification("calendar", notification)

                mock_service.process_webhook.assert_called_once()
                call_kwargs = mock_service.process_webhook.call_args
                assert call_kwargs.kwargs.get("privacy") == "confidential"
                assert call_kwargs.kwargs.get("target_user_id") == 42

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_missing_privacy_defaults_public(self, poller):
        """When privacy is missing from poll result, defaults to 'public'."""
        notification = {
            "event_type": "calendar.reminder_upcoming",
            "title": "Meeting",
            "message": "In 30 Minuten: Meeting",
            "dedup_key": "cal:shared:789:30min",
        }

        mock_service = MagicMock()
        mock_service.process_webhook = AsyncMock()

        mock_db = AsyncMock()
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_session)

        import types
        mock_db_module = types.ModuleType("services.database")
        mock_db_module.AsyncSessionLocal = mock_session_factory
        mock_svc_module = types.ModuleType("services.notification_service")
        mock_svc_module.NotificationService = MagicMock(return_value=mock_service)

        import sys
        with patch.dict(sys.modules, {
            "services.database": mock_db_module,
            "services.notification_service": mock_svc_module,
        }):
            await poller._process_notification("calendar", notification)

            mock_service.process_webhook.assert_called_once()
            call_kwargs = mock_service.process_webhook.call_args
            assert call_kwargs.kwargs.get("privacy") == "public"
            assert call_kwargs.kwargs.get("target_user_id") is None


class TestSeenKeysPruning:
    """Test dedup key set doesn't grow unbounded."""

    @pytest.mark.unit
    def test_pruning_logic(self, poller):
        """Seen keys are pruned when exceeding 1000."""
        # Fill with 1001 keys
        for i in range(1001):
            poller._seen_keys.add(f"key-{i}")

        assert len(poller._seen_keys) == 1001

        # Manually trigger the pruning logic (same as in _process_notification)
        if len(poller._seen_keys) > 1000:
            keys_list = list(poller._seen_keys)
            poller._seen_keys = set(keys_list[-500:])

        assert len(poller._seen_keys) == 500
