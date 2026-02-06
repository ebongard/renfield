"""
Tests for MCP Admin API endpoints (/api/mcp/*).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.mcp_client import MCPToolInfo

# ============================================================================
# Helper: Simulate app.state.mcp_manager
# ============================================================================

def _mock_mcp_manager(connected=True, tools=None):
    """Create a mock MCPManager for API tests."""
    from services.mcp_client import MCPServerState
    manager = MagicMock()

    if tools is None:
        tools = [
            MCPToolInfo(
                server_name="test_server",
                original_name="test_tool",
                namespaced_name="mcp.test_server.test_tool",
                description="A test tool",
                input_schema={"properties": {"param": {"type": "string"}}, "required": ["param"]},
            )
        ]

    all_tools_with_status = [
        {
            "name": t.namespaced_name,
            "server": t.server_name,
            "original_name": t.original_name,
            "description": t.description,
            "input_schema": t.input_schema,
            "active": True,
        }
        for t in tools
    ]

    manager.get_all_tools.return_value = tools
    manager.get_all_tools_with_status.return_value = all_tools_with_status
    manager.get_status.return_value = {
        "enabled": True,
        "total_tools": len(tools),
        "servers": [
            {
                "name": "test_server",
                "transport": "streamable_http",
                "connected": connected,
                "tool_count": len(tools),
                "total_tool_count": len(tools),
                "last_error": None if connected else "Connection refused",
            }
        ],
    }
    manager.refresh_tools = AsyncMock()
    manager.set_tool_override = AsyncMock()
    manager._servers = {
        "test_server": MagicMock(spec=MCPServerState)
    }
    return manager


# ============================================================================
# Status Endpoint
# ============================================================================

class TestMCPStatusAPI:
    """Test GET /api/mcp/status."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_status_disabled(self, async_client):
        """When MCP is disabled, status should return enabled=False."""
        with patch("api.routes.mcp._get_mcp_manager", return_value=None):
            response = await async_client.get("/api/mcp/status")
            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is False
            assert data["total_tools"] == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_status_with_servers(self, async_client):
        """With connected servers, status should show tool counts."""
        mock_manager = _mock_mcp_manager(connected=True)

        with patch("api.routes.mcp._get_mcp_manager", return_value=mock_manager):
            response = await async_client.get("/api/mcp/status")
            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is True
            assert data["total_tools"] == 1
            assert len(data["servers"]) == 1
            assert data["servers"][0]["connected"] is True


# ============================================================================
# Tools Endpoint
# ============================================================================

class TestMCPToolsAPI:
    """Test GET /api/mcp/tools."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_tools_list(self, async_client):
        """Should list all discovered MCP tools with schemas and active flag."""
        mock_manager = _mock_mcp_manager()

        with patch("api.routes.mcp._get_mcp_manager", return_value=mock_manager):
            response = await async_client.get("/api/mcp/tools")
            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is True
            assert data["total"] == 1
            assert len(data["tools"]) == 1

            tool = data["tools"][0]
            assert tool["name"] == "mcp.test_server.test_tool"
            assert tool["server"] == "test_server"
            assert tool["original_name"] == "test_tool"
            assert tool["description"] == "A test tool"
            assert tool["active"] is True
            assert "param" in tool["input_schema"]["properties"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_tools_disabled(self, async_client):
        """When MCP is disabled, should return empty list."""
        with patch("api.routes.mcp._get_mcp_manager", return_value=None):
            response = await async_client.get("/api/mcp/tools")
            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is False
            assert data["tools"] == []


# ============================================================================
# Refresh Endpoint
# ============================================================================

class TestMCPRefreshAPI:
    """Test POST /api/mcp/refresh."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_refresh(self, async_client):
        """Manual refresh should call refresh_tools and return status."""
        mock_manager = _mock_mcp_manager()

        with patch("api.routes.mcp._get_mcp_manager", return_value=mock_manager):
            response = await async_client.post("/api/mcp/refresh")
            assert response.status_code == 200
            mock_manager.refresh_tools.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_refresh_disabled(self, async_client):
        """Refresh when MCP is disabled should return 400."""
        with patch("api.routes.mcp._get_mcp_manager", return_value=None):
            response = await async_client.post("/api/mcp/refresh")
            assert response.status_code == 400


# ============================================================================
# Update Server Tools Endpoint
# ============================================================================

class TestMCPUpdateToolsAPI:
    """Test PATCH /api/mcp/servers/{name}/tools."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_tools_activates(self, async_client):
        """PATCH should activate specified tools for a server."""
        mock_manager = _mock_mcp_manager()

        with patch("api.routes.mcp._get_mcp_manager", return_value=mock_manager):
            response = await async_client.patch(
                "/api/mcp/servers/test_server/tools",
                json={"active_tools": ["test_tool"]},
            )
            assert response.status_code == 200
            mock_manager.set_tool_override.assert_called_once()
            call_args = mock_manager.set_tool_override.call_args
            assert call_args[0][0] == "test_server"
            assert call_args[0][1] == ["test_tool"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_tools_reset_defaults(self, async_client):
        """PATCH with null active_tools should reset to YAML defaults."""
        mock_manager = _mock_mcp_manager()

        with patch("api.routes.mcp._get_mcp_manager", return_value=mock_manager):
            response = await async_client.patch(
                "/api/mcp/servers/test_server/tools",
                json={"active_tools": None},
            )
            assert response.status_code == 200
            mock_manager.set_tool_override.assert_called_once()
            call_args = mock_manager.set_tool_override.call_args
            assert call_args[0][0] == "test_server"
            assert call_args[0][1] is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_tools_unknown_server(self, async_client):
        """PATCH for unknown server should return 404."""
        mock_manager = _mock_mcp_manager()

        with patch("api.routes.mcp._get_mcp_manager", return_value=mock_manager):
            response = await async_client.patch(
                "/api/mcp/servers/nonexistent_server/tools",
                json={"active_tools": ["t1"]},
            )
            assert response.status_code == 404

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_tools_mcp_disabled(self, async_client):
        """PATCH when MCP is disabled should return 400."""
        with patch("api.routes.mcp._get_mcp_manager", return_value=None):
            response = await async_client.patch(
                "/api/mcp/servers/test_server/tools",
                json={"active_tools": ["t1"]},
            )
            assert response.status_code == 400
