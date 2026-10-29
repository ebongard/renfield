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
            # 🛑 Gemessen, nicht geraten (2026-10-29, `bin/measure-preselect.py`
            # gegen den ECHTEN Prompt des Live-Zuges, Signatur `aa62fdb7`):
            # die alte Fassung traf 27/39 (69 %), diese 36/39 (92 %).
            #
            # Die alte nannte nur die PERSON. „frag <jemand>s Renfield" traf
            # damit 3/3 — das Modell erkennt das Muster, nicht den Namen, denn
            # auch „frag Mamas renfield" traf, obwohl Mama nicht der Name ist.
            # Was 0/3 traf, war jede Frage, die die INSTANZ nennt („was liegt
            # in x-idra?", „weiss die andere Instanz …") oder die MEHRZAHL
            # („suche in allen Renfields"). Diese drei sind jetzt 3/3.
            #
            # 🛑 Der Scanner-Satz am Ende ist kein Beiwerk: „scanne nach
            # x-idra" nennt ein ABLAGEZIEL, keine Instanz, und muss beim
            # Scanner bleiben. Er tut es (3/3) — aber nur, solange der Satz
            # nicht abgeschnitten wird, siehe die Kappung des Namens unten.
            #
            # Was Text NICHT schafft: „Welche Dokumente sind in der x-idra
            # instanz …" bleibt 0/3, auch mit drei verschiedenen
            # Dokumentformulierungen (V3/V4 gemessen, beide 92 %, kein
            # Zugewinn). Dafuer muesste der INSTANZNAME hier stehen — er ist
            # nicht da: `peer_users` traegt keine entfernte `INSTANCE_ID`, und
            # das Paarungsangebot fuehrt sie nicht mit. Eigener Posten.
            f"Ask {_kurzer_name(peer.remote_display_name)}'s Renfield \u2014 "
            f"another paired INSTANCE \u2014 for information its people chose "
            f"to share with you through circles. Use for \u201efrag "
            f"<jemand>s Renfield\u201c, \u201ewas liegt in <Instanz>\u201c, "
            f"\u201edie andere Instanz\u201c and \u201ein allen Renfields "
            f"suchen\u201c. NOT for scanning paper: in \u201escanne nach "
            f"<Ziel>\u201c <Ziel> is a filing target, not an instance."
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


#: Wie viel vom Anzeigenamen in die Beschreibung darf.
#: 🛑 Die Beschreibung wird bei `_PRESELECT_DESC_CHARS` (480) GEKAPPT, und der
#: Scanner-Abgrenzungssatz steht am ENDE. Ein langer Anzeigename schiebt genau
#: ihn ueber die Kante — und dann wird aus „scanne nach x-idra" wieder eine
#: Peer-Anfrage. Der Name kommt von der GEGENSEITE (`offer.display_name`), ist
#: also nicht unter eigener Kontrolle. Mit dieser Kappung bleibt die Zeile
#: unter 400 Zeichen, egal was dort steht.
_NAME_IN_DESC_CHARS = 40


def _kurzer_name(name: str) -> str:
    """Der Anzeigename, gekappt — siehe `_NAME_IN_DESC_CHARS`."""
    return name[:_NAME_IN_DESC_CHARS]


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
