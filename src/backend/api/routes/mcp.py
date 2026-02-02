"""
MCP Admin API Routes — Status, tools, and manual refresh.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from services.auth_service import require_permission
from services.database import get_db
from models.permissions import Permission

router = APIRouter()


class UpdateToolsRequest(BaseModel):
    """Request body for PATCH /servers/{name}/tools."""
    active_tools: Optional[List[str]] = None  # None = reset to YAML defaults


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
    List all discovered MCP tools with their schemas and active status.

    Requires: admin permission (when auth is enabled)
    """
    manager = _get_mcp_manager()
    if not manager:
        return {"enabled": False, "tools": []}

    tools = manager.get_all_tools_with_status()
    return {
        "enabled": True,
        "total": len(tools),
        "tools": tools,
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


@router.patch("/servers/{name}/tools")
async def update_server_tools(
    name: str,
    body: UpdateToolsRequest,
    user=Depends(require_permission(Permission.ADMIN)),
    db: AsyncSession = Depends(get_db),
):
    """
    Update active tools for an MCP server.

    Pass active_tools as a list of tool base names to activate only those.
    Pass null to reset to YAML defaults (prompt_tools).
    Requires: admin permission (when auth is enabled)
    """
    manager = _get_mcp_manager()
    if not manager:
        raise HTTPException(status_code=400, detail="MCP is not enabled")

    if name not in manager._servers:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        await manager.set_tool_override(name, body.active_tools, db)
        return manager.get_status()
    except Exception as e:
        logger.error(f"Failed to update tools for '{name}': {e}")
        raise HTTPException(status_code=500, detail=str(e))
