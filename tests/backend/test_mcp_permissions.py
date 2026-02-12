"""
Tests for MCP Permission System — permission checks on MCP tool execution.

Covers:
- Convention-based permissions (mcp.<server_name>)
- YAML permissions (server-level + tool-level)
- Wildcard matching (mcp.*, mcp.calendar.*)
- Backwards compatibility (user_permissions=None → allow all)
- Permission denied responses
- Role validation accepts mcp.* permissions
- has_mcp_permission() wildcard logic
"""
import pytest

from models.permissions import (
    DEFAULT_ROLES,
    has_mcp_permission,
    has_permission,
)
from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
    TokenBucketRateLimiter,
)

# ============================================================================
# has_mcp_permission() — Wildcard Matching
# ============================================================================


class TestHasMcpPermission:
    """Test MCP permission wildcard matching."""

    @pytest.mark.unit
    def test_exact_match(self):
        assert has_mcp_permission(["mcp.weather"], "mcp.weather") is True

    @pytest.mark.unit
    def test_exact_match_with_tool(self):
        assert has_mcp_permission(["mcp.calendar.read"], "mcp.calendar.read") is True

    @pytest.mark.unit
    def test_no_match(self):
        assert has_mcp_permission(["mcp.weather"], "mcp.calendar") is False

    @pytest.mark.unit
    def test_admin_wildcard(self):
        """mcp.* grants access to everything."""
        assert has_mcp_permission(["mcp.*"], "mcp.weather") is True
        assert has_mcp_permission(["mcp.*"], "mcp.calendar.read") is True
        assert has_mcp_permission(["mcp.*"], "mcp.homeassistant.turn_on") is True

    @pytest.mark.unit
    def test_server_wildcard(self):
        """mcp.calendar.* grants access to all calendar tools."""
        assert has_mcp_permission(["mcp.calendar.*"], "mcp.calendar.read") is True
        assert has_mcp_permission(["mcp.calendar.*"], "mcp.calendar.manage") is True
        assert has_mcp_permission(["mcp.calendar.*"], "mcp.calendar") is True
        # But not other servers
        assert has_mcp_permission(["mcp.calendar.*"], "mcp.weather") is False

    @pytest.mark.unit
    def test_server_level_implies_tools(self):
        """mcp.calendar (without wildcard) grants access to mcp.calendar.read."""
        assert has_mcp_permission(["mcp.calendar"], "mcp.calendar.read") is True
        assert has_mcp_permission(["mcp.calendar"], "mcp.calendar") is True
        assert has_mcp_permission(["mcp.calendar"], "mcp.weather") is False

    @pytest.mark.unit
    def test_non_mcp_permissions_ignored(self):
        """Non-MCP permissions are ignored."""
        assert has_mcp_permission(["kb.all", "ha.full"], "mcp.weather") is False

    @pytest.mark.unit
    def test_empty_permissions(self):
        assert has_mcp_permission([], "mcp.weather") is False

    @pytest.mark.unit
    def test_mixed_permissions(self):
        """Mixed MCP and non-MCP permissions."""
        perms = ["kb.all", "mcp.weather", "ha.full"]
        assert has_mcp_permission(perms, "mcp.weather") is True
        assert has_mcp_permission(perms, "mcp.calendar") is False


# ============================================================================
# has_permission() — Dynamic MCP strings
# ============================================================================


class TestHasPermissionWithMcp:
    """Test has_permission() with dynamic MCP permission strings."""

    @pytest.mark.unit
    def test_mcp_string_permission(self):
        """has_permission() accepts string MCP permissions."""
        assert has_permission(["mcp.weather"], "mcp.weather") is True
        assert has_permission(["mcp.*"], "mcp.weather") is True

    @pytest.mark.unit
    def test_mcp_string_denied(self):
        assert has_permission(["mcp.calendar"], "mcp.weather") is False

    @pytest.mark.unit
    def test_core_enum_still_works(self):
        """Core Permission enum checks still work."""
        from models.permissions import Permission
        assert has_permission(["kb.all"], Permission.KB_ALL) is True
        assert has_permission(["kb.all"], Permission.KB_SHARED) is True
        assert has_permission(["kb.none"], Permission.KB_ALL) is False


# ============================================================================
# MCPManager._check_tool_permission()
# ============================================================================


def _make_manager_with_tool(
    server_name: str = "weather",
    tool_name: str = "get_forecast",
    permissions: list[str] | None = None,
    tool_permissions: dict[str, str] | None = None,
) -> tuple[MCPManager, MCPToolInfo]:
    """Helper: create a manager with one server and one tool."""
    manager = MCPManager()

    config = MCPServerConfig(
        name=server_name,
        url="http://localhost:9090/mcp",
        transport=MCPTransportType.STREAMABLE_HTTP,
        permissions=permissions or [],
        tool_permissions=tool_permissions or {},
    )

    tool_info = MCPToolInfo(
        server_name=server_name,
        original_name=tool_name,
        namespaced_name=f"mcp.{server_name}.{tool_name}",
        description="Test tool",
    )

    state = MCPServerState(
        config=config,
        connected=True,
        tools=[tool_info],
        rate_limiter=TokenBucketRateLimiter(),
    )
    manager._servers[server_name] = state
    manager._tool_index[tool_info.namespaced_name] = tool_info

    return manager, tool_info


class TestCheckToolPermission:
    """Test MCPManager._check_tool_permission() logic."""

    @pytest.mark.unit
    def test_none_permissions_allows_all(self):
        """user_permissions=None → allow (backwards-compatible, AUTH_ENABLED=false)."""
        manager, tool = _make_manager_with_tool()
        result = manager._check_tool_permission(tool, None)
        assert result is None

    @pytest.mark.unit
    def test_admin_wildcard_allows_all(self):
        """mcp.* in user_permissions → allow all MCP tools."""
        manager, tool = _make_manager_with_tool()
        result = manager._check_tool_permission(tool, ["mcp.*"])
        assert result is None

    @pytest.mark.unit
    def test_convention_based_permission(self):
        """Server 'weather' without YAML permissions → requires mcp.weather."""
        manager, tool = _make_manager_with_tool(server_name="weather")

        # User has mcp.weather → allowed
        result = manager._check_tool_permission(tool, ["mcp.weather"])
        assert result is None

        # User lacks it → denied
        result = manager._check_tool_permission(tool, ["mcp.calendar"])
        assert result is not None
        assert "Permission denied" in result

    @pytest.mark.unit
    def test_server_level_yaml_permissions(self):
        """Server with YAML permissions list → user needs at least one."""
        manager, tool = _make_manager_with_tool(
            server_name="calendar",
            permissions=["mcp.calendar.read", "mcp.calendar.manage"],
        )

        # User has one of them → allowed
        result = manager._check_tool_permission(tool, ["mcp.calendar.read"])
        assert result is None

        # User has neither → denied
        result = manager._check_tool_permission(tool, ["mcp.weather"])
        assert result is not None
        assert "Permission denied" in result

    @pytest.mark.unit
    def test_tool_level_yaml_permissions(self):
        """Tool-specific permission mapping → exact match required."""
        manager, tool = _make_manager_with_tool(
            server_name="calendar",
            tool_name="list_events",
            tool_permissions={"list_events": "mcp.calendar.read"},
        )

        # User has the exact tool permission → allowed
        result = manager._check_tool_permission(tool, ["mcp.calendar.read"])
        assert result is None

        # User has server-level but not specific tool permission → denied
        result = manager._check_tool_permission(tool, ["mcp.calendar.manage"])
        assert result is not None
        assert "mcp.calendar.read" in result

    @pytest.mark.unit
    def test_tool_permission_takes_priority(self):
        """tool_permissions mapping takes priority over server-level permissions."""
        manager, tool = _make_manager_with_tool(
            server_name="calendar",
            tool_name="list_events",
            permissions=["mcp.calendar.manage"],
            tool_permissions={"list_events": "mcp.calendar.read"},
        )

        # Tool requires .read but user only has .manage (server-level) → still allowed via tool mapping
        result = manager._check_tool_permission(tool, ["mcp.calendar.read"])
        assert result is None

        # User only has server perm but not tool-specific perm → denied
        result = manager._check_tool_permission(tool, ["mcp.calendar.manage"])
        assert result is not None

    @pytest.mark.unit
    def test_server_wildcard_permission(self):
        """mcp.calendar.* grants access to all calendar tools."""
        manager, tool = _make_manager_with_tool(
            server_name="calendar",
            tool_name="list_events",
            tool_permissions={"list_events": "mcp.calendar.read"},
        )
        result = manager._check_tool_permission(tool, ["mcp.calendar.*"])
        assert result is None

    @pytest.mark.unit
    def test_empty_permissions_denied(self):
        """Empty user_permissions list → denied."""
        manager, tool = _make_manager_with_tool()
        result = manager._check_tool_permission(tool, [])
        assert result is not None
        assert "Permission denied" in result


# ============================================================================
# MCPManager.execute_tool() — Permission integration
# ============================================================================


class TestExecuteToolPermissions:
    """Test that execute_tool() enforces permissions before execution."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_permission_denied_returns_error(self):
        """execute_tool() returns error dict when permission denied."""
        manager, _tool = _make_manager_with_tool()
        result = await manager.execute_tool(
            "mcp.weather.get_forecast",
            {"location": "Berlin"},
            user_permissions=["mcp.calendar"],  # Wrong server
        )
        assert result["success"] is False
        assert "Permission denied" in result["message"]
        assert result["data"] is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_none_permissions_skips_check(self):
        """execute_tool() with user_permissions=None skips permission check."""
        manager, _tool = _make_manager_with_tool()
        # Will fail at execution (no session), but should pass permission check
        result = await manager.execute_tool(
            "mcp.weather.get_forecast",
            {"location": "Berlin"},
            user_permissions=None,
        )
        # Should fail for "not connected" or similar, not permission denied
        assert "Permission denied" not in result.get("message", "")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unknown_tool_not_permission_error(self):
        """Unknown tool returns tool-not-found, not permission denied."""
        manager, _ = _make_manager_with_tool()
        result = await manager.execute_tool(
            "mcp.unknown.tool",
            {},
            user_permissions=["mcp.weather"],
        )
        assert result["success"] is False
        assert "Unknown MCP tool" in result["message"]


# ============================================================================
# DEFAULT_ROLES — MCP permissions included
# ============================================================================


class TestDefaultRoles:
    """Test that default roles include MCP permissions."""

    @pytest.mark.unit
    def test_admin_has_mcp_wildcard(self):
        admin = next(r for r in DEFAULT_ROLES if r["name"] == "Admin")
        assert "mcp.*" in admin["permissions"]

    @pytest.mark.unit
    def test_familie_has_mcp_wildcard(self):
        familie = next(r for r in DEFAULT_ROLES if r["name"] == "Familie")
        assert "mcp.*" in familie["permissions"]

    @pytest.mark.unit
    def test_gast_has_no_mcp(self):
        gast = next(r for r in DEFAULT_ROLES if r["name"] == "Gast")
        assert not any(p.startswith("mcp.") for p in gast["permissions"])


# ============================================================================
# ActionExecutor — user_permissions parameter
# ============================================================================


class TestActionExecutorPermissions:
    """Test ActionExecutor passes user_permissions to MCP manager."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_passes_permissions(self):
        """ActionExecutor.execute() passes user_permissions to mcp_manager."""
        from unittest.mock import AsyncMock, MagicMock

        from services.action_executor import ActionExecutor

        mock_manager = MagicMock()
        mock_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "ok", "data": None
        })

        executor = ActionExecutor(mcp_manager=mock_manager)
        await executor.execute(
            {"intent": "mcp.weather.get_forecast", "parameters": {"location": "Berlin"}, "confidence": 0.9},
            user_permissions=["mcp.weather"],
        )

        mock_manager.execute_tool.assert_called_once()
        call_kwargs = mock_manager.execute_tool.call_args
        assert call_kwargs.kwargs["user_permissions"] == ["mcp.weather"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_without_permissions(self):
        """ActionExecutor.execute() without user_permissions passes None."""
        from unittest.mock import AsyncMock, MagicMock

        from services.action_executor import ActionExecutor

        mock_manager = MagicMock()
        mock_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "ok", "data": None
        })

        executor = ActionExecutor(mcp_manager=mock_manager)
        await executor.execute(
            {"intent": "mcp.weather.get_forecast", "parameters": {}, "confidence": 0.9},
        )

        call_kwargs = mock_manager.execute_tool.call_args
        assert call_kwargs.kwargs["user_permissions"] is None


# ============================================================================
# MCPServerConfig — new fields
# ============================================================================


class TestMCPServerConfigPermFields:
    """Test MCPServerConfig accepts permission fields."""

    @pytest.mark.unit
    def test_default_empty_permissions(self):
        config = MCPServerConfig(name="test")
        assert config.permissions == []
        assert config.tool_permissions == {}

    @pytest.mark.unit
    def test_permissions_from_constructor(self):
        config = MCPServerConfig(
            name="calendar",
            permissions=["mcp.calendar.read", "mcp.calendar.manage"],
            tool_permissions={"list_events": "mcp.calendar.read"},
        )
        assert config.permissions == ["mcp.calendar.read", "mcp.calendar.manage"]
        assert config.tool_permissions["list_events"] == "mcp.calendar.read"


# ============================================================================
# get_mcp_permissions()
# ============================================================================


class TestGetMcpPermissions:
    """Test dynamic MCP permission discovery."""

    @pytest.mark.unit
    def test_without_manager(self):
        from models.permissions import get_mcp_permissions
        assert get_mcp_permissions(None) == []

    @pytest.mark.unit
    def test_with_manager(self):
        from models.permissions import get_mcp_permissions

        manager, _ = _make_manager_with_tool(server_name="weather")
        perms = get_mcp_permissions(manager)
        assert len(perms) >= 1
        values = [p["value"] for p in perms]
        assert "mcp.weather" in values

    @pytest.mark.unit
    def test_with_yaml_permissions(self):
        from models.permissions import get_mcp_permissions

        manager, _ = _make_manager_with_tool(
            server_name="calendar",
            permissions=["mcp.calendar.read", "mcp.calendar.manage"],
        )
        perms = get_mcp_permissions(manager)
        values = [p["value"] for p in perms]
        assert "mcp.calendar" in values
        assert "mcp.calendar.read" in values
        assert "mcp.calendar.manage" in values
