"""
Tests for MCP Admin API endpoints (/api/mcp/*).
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from services.mcp_client import MCPToolInfo


# ============================================================================
# Helper: Simulate app.state.mcp_manager
# ============================================================================

def _mock_mcp_manager(connected=True, tools=None):
    """Create a mock MCPManager for API tests."""
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

    manager.get_all_tools.return_value = tools
    manager.get_status.return_value = {
        "enabled": True,
        "total_tools": len(tools),
        "servers": [
            {
                "name": "test_server",
                "transport": "streamable_http",
                "connected": connected,
                "tool_count": len(tools),
                "last_error": None if connected else "Connection refused",
            }
        ],
    }
    manager.refresh_tools = AsyncMock()
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
        """Should list all discovered MCP tools with schemas."""
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
