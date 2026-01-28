"""
MCP Admin API Routes — Status, tools, and manual refresh.
"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from services.auth_service import require_permission
from models.permissions import Permission

router = APIRouter()


def _get_mcp_manager():
    """Get MCP manager from app state."""
    from main import app
    return getattr(app.state, "mcp_manager", None)


@router.get("/status")
async def mcp_status(
    user=Depends(require_permission(Permission.ADMIN))
):
    """
    MCP server connection status.

    Returns connection state, tool counts, and errors for each server.
    Requires: admin permission (when auth is enabled)
    """
    manager = _get_mcp_manager()
    if not manager:
        return {"enabled": False, "total_tools": 0, "servers": []}

    return manager.get_status()


@router.get("/tools")
async def mcp_tools(
    user=Depends(require_permission(Permission.ADMIN))
):
    """
    List all discovered MCP tools with their schemas.

    Requires: admin permission (when auth is enabled)
    """
    manager = _get_mcp_manager()
    if not manager:
        return {"enabled": False, "tools": []}

    tools = manager.get_all_tools()
    return {
        "enabled": True,
        "total": len(tools),
        "tools": [
            {
                "name": t.namespaced_name,
                "server": t.server_name,
                "original_name": t.original_name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ],
    }


@router.post("/refresh")
async def mcp_refresh(
    user=Depends(require_permission(Permission.ADMIN))
):
    """
    Manually refresh tool lists from all MCP servers.

    Reconnects disconnected servers and re-discovers tools.
    Requires: admin permission (when auth is enabled)
    """
    manager = _get_mcp_manager()
    if not manager:
        raise HTTPException(status_code=400, detail="MCP is not enabled")

    try:
        await manager.refresh_tools()
        return manager.get_status()
    except Exception as e:
        logger.error(f"MCP refresh failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
