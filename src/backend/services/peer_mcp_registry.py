"""
PeerMCPRegistry — bridges paired PeerUser rows into the MCPManager.

Each non-revoked peer becomes a "virtual" MCP server entry (transport
= FEDERATION) exposing exactly one tool: `query_brain`. The agent loop
then sees peers alongside every other MCP server and can route a
query to `mcp.peer_<id>.query_brain` like any other tool. The actual
transport (sign + HTTP initiate/retrieve + signature verify + peer-
anchor binding) is handled by the F3c.1 federation branch in
`MCPManager.execute_tool_streaming`.

Sync lifecycle:

    sync_peers(manager, db)          ← called at app startup
        │
        ├─ SELECT peer_users WHERE revoked_at IS NULL
        │
        ├─ For each peer: upsert MCPServerState in manager._servers
        │                  + register query_brain in manager._tool_index
        │
        └─ Remove peer-entries whose PeerUser is gone/revoked

F5 hardening will add event-driven refresh (on pair / unpair / revoke)
instead of requiring a manual resync. For v1 we call this at startup
only; `PairingService.complete_handshake` can opt to call it post-pair
to make new peers immediately queryable without a restart.

What we DON'T do here:
  - Store PeerUser references. The registry keeps only `peer_user_id`
    in the MCPServerConfig; the actual row is loaded per-request so
    revocation propagates immediately (see F3c.1 branch).
  - Touch the rate limiter or any other runtime state. Registry only
    manages discovery shape.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PeerUser
from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
)


# Namespace prefix for federation server entries. Using `peer_{id}` keeps
# the name stable across display-name changes — the agent loop references
# tools by namespace, not by display_name.
FEDERATION_SERVER_PREFIX = "peer_"
QUERY_BRAIN_TOOL_NAME = "query_brain"


def _server_name_for(peer_user_id: int) -> str:
    return f"{FEDERATION_SERVER_PREFIX}{peer_user_id}"


def _namespaced_query_brain(peer_user_id: int) -> str:
    return f"mcp.{_server_name_for(peer_user_id)}.{QUERY_BRAIN_TOOL_NAME}"


def _build_query_brain_tool(peer: PeerUser) -> MCPToolInfo:
    """
    Build the MCPToolInfo for a peer's query_brain. Description is
    personalised with the peer's display_name so the agent loop has a
    natural-language signal ("ask Mom's brain") when selecting tools.
    """
    server_name = _server_name_for(peer.id)
    return MCPToolInfo(
        server_name=server_name,
        original_name=QUERY_BRAIN_TOOL_NAME,
        namespaced_name=f"mcp.{server_name}.{QUERY_BRAIN_TOOL_NAME}",
        description=(
            f"Ask {peer.remote_display_name}'s Renfield for information "
            f"they've chosen to share with you through circles. "
            f"Use when the user's question is about something "
            f"{peer.remote_display_name} would know but you don't."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language question to forward to the peer's Renfield. "
                        "The peer's LLM will synthesize an answer from its own atoms."
                    ),
                },
            },
            "required": ["query"],
        },
    )


def _build_federation_config(peer: PeerUser) -> MCPServerConfig:
    """Build an MCPServerConfig flagging FEDERATION transport +
    streaming so execute_tool_streaming takes the F3c.1 branch."""
    return MCPServerConfig(
        name=_server_name_for(peer.id),
        transport=MCPTransportType.FEDERATION,
        enabled=True,
        streaming=True,
        peer_user_id=peer.id,
    )


async def sync_peers(manager: MCPManager, db: AsyncSession) -> None:
    """
    Rebuild the federation-transport entries in MCPManager from the
    current set of non-revoked PeerUser rows.

    Idempotent: can be called at startup AND after a pair/unpair event
    without accumulating duplicates. Any existing peer entries whose
    PeerUser is revoked or deleted are removed from the manager.
    """
    rows = (await db.execute(
        select(PeerUser).where(PeerUser.revoked_at.is_(None))
    )).scalars().all()

    wanted: dict[str, PeerUser] = {_server_name_for(p.id): p for p in rows}

    # 1. Register/update current peers.
    for server_name, peer in wanted.items():
        state = MCPServerState(config=_build_federation_config(peer))
        # `connected=True` is a lie — there's no MCP session — but
        # execute_tool_streaming's federation branch doesn't touch the
        # session at all, it goes straight to FederationQueryAsker.
        # We set it so `get_connected_server_names()` reports peers
        # as discoverable.
        state.connected = True

        tool_info = _build_query_brain_tool(peer)
        manager._tool_index[tool_info.namespaced_name] = tool_info
        # `tools` drives admin surfaces (`get_status()` tool_count);
        # `all_discovered_tools` drives fuzzy fallback in execute_tool.
        # Set both so dashboards report the peer correctly AND the
        # agent-loop's tool-lookup path can find it by namespace or
        # short name.
        state.tools = [tool_info]
        state.all_discovered_tools = [tool_info]
        manager._servers[server_name] = state

    # 2. Drop entries for peers that have since been revoked or deleted.
    stale_server_names = [
        name for name in list(manager._servers.keys())
        if name.startswith(FEDERATION_SERVER_PREFIX) and name not in wanted
    ]
    for server_name in stale_server_names:
        manager._servers.pop(server_name, None)
        stale_namespaced = f"mcp.{server_name}.{QUERY_BRAIN_TOOL_NAME}"
        manager._tool_index.pop(stale_namespaced, None)

    logger.info(
        f"🔗 Federation peer registry synced: "
        f"{len(wanted)} peer(s) active, {len(stale_server_names)} removed"
    )
