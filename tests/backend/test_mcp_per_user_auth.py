"""Per-user MCP auth (per-user data scoping) — the opt-in per-user-session seam.

Verifies the fail-closed semantics of `MCPServerConfig.per_user_auth` and the
`set_user_auth_resolver` seam: a per-user server never rides the shared operator
credential, and a legacy (opt-out) server is byte-identical.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    _resolve_user_auth_headers,
    set_user_auth_resolver,
)


@pytest.fixture(autouse=True)
def _clear_resolver():
    """Each test starts and ends with no resolver registered."""
    set_user_auth_resolver(None)
    yield
    set_user_auth_resolver(None)


def _manager_with_per_user_tool(per_user_auth=True):
    manager = MCPManager()
    tool = MCPToolInfo("jira", "jira_search", "mcp.jira.jira_search", "Search")
    manager._tool_index["mcp.jira.jira_search"] = tool
    shared_session = AsyncMock()
    shared_result = MagicMock()
    shared_result.isError = False
    shared_result.content = [MagicMock(type="text", text="SHARED")]
    shared_session.call_tool = AsyncMock(return_value=shared_result)
    state = MCPServerState(
        config=MCPServerConfig(name="jira", per_user_auth=per_user_auth),
        connected=True,
        session=shared_session,
    )
    manager._servers["jira"] = state
    return manager, shared_session


# --- resolver seam ------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolver_default_none():
    assert await _resolve_user_auth_headers("jira", 1) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolver_set_and_clear():
    async def resolver(server, uid):
        return {"Authorization": f"Bearer tok-{server}-{uid}"}

    set_user_auth_resolver(resolver)
    assert await _resolve_user_auth_headers("jira", 7) == {
        "Authorization": "Bearer tok-jira-7"
    }
    set_user_auth_resolver(None)
    assert await _resolve_user_auth_headers("jira", 7) is None


@pytest.mark.unit
def test_config_per_user_auth_defaults_false():
    assert MCPServerConfig(name="x").per_user_auth is False


# --- fail-closed semantics ----------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_user_no_identity_denied():
    """per_user_auth server + user_id=None → deny, shared session untouched."""
    manager, shared = _manager_with_per_user_tool()
    result = await manager.execute_tool("mcp.jira.jira_search", {"jql": "x"}, user_id=None)
    assert result["success"] is False
    assert "Authentication required" in result["message"]
    shared.call_tool.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {}])
async def test_per_user_no_credential_denied(bad):
    """Resolver returns None OR empty dict (no credential) → deny, no fallback."""
    async def resolver(server, uid):
        return bad

    set_user_auth_resolver(resolver)
    manager, shared = _manager_with_per_user_tool()
    result = await manager.execute_tool("mcp.jira.jira_search", {"jql": "x"}, user_id=42)
    assert result["success"] is False
    assert "credential" in result["message"].lower()
    shared.call_tool.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_user_resolver_error_denied():
    """A throwing resolver must fail closed, not fall through to shared creds."""
    async def resolver(server, uid):
        raise RuntimeError("vault down")

    set_user_auth_resolver(resolver)
    manager, shared = _manager_with_per_user_tool()
    result = await manager.execute_tool("mcp.jira.jira_search", {"jql": "x"}, user_id=42)
    assert result["success"] is False
    shared.call_tool.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_user_credential_uses_per_user_session():
    """With a resolved header, the call runs on a per-user session, NOT shared."""
    async def resolver(server, uid):
        # Multi-header provider (mirrors mcp-atlassian: Bearer + cloud id)
        return {
            "Authorization": f"Bearer user-{uid}-token",
            "X-Atlassian-Cloud-Id": "cloud-123",
        }

    set_user_auth_resolver(resolver)
    manager, shared = _manager_with_per_user_tool()

    per_user_result = MagicMock()
    per_user_result.isError = False
    per_user_result.content = [MagicMock(type="text", text="PER_USER")]
    manager._call_tool_per_user_session = AsyncMock(return_value=per_user_result)

    result = await manager.execute_tool("mcp.jira.jira_search", {"jql": "x"}, user_id=42)
    assert result["success"] is True
    assert result["message"] == "PER_USER"
    shared.call_tool.assert_not_called()
    manager._call_tool_per_user_session.assert_awaited_once()
    args = manager._call_tool_per_user_session.await_args.args
    # (state, tool_name, arguments, auth_headers)
    assert args[1] == "jira_search"
    assert args[3] == {
        "Authorization": "Bearer user-42-token",
        "X-Atlassian-Cloud-Id": "cloud-123",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_server_uses_shared_session():
    """per_user_auth=False → byte-identical legacy path (shared session)."""
    manager, shared = _manager_with_per_user_tool(per_user_auth=False)
    manager._call_tool_per_user_session = AsyncMock()
    result = await manager.execute_tool("mcp.jira.jira_search", {"jql": "x"}, user_id=42)
    assert result["success"] is True
    assert result["message"] == "SHARED"
    shared.call_tool.assert_awaited_once()
    manager._call_tool_per_user_session.assert_not_called()
