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

import unicodedata
import weakref

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PeerUser
from services.federation_instance_name import stored_instance_name
from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
)
from utils.config import settings

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
        description=_description_for(peer),
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


def _description_for(peer: PeerUser) -> str:
    """The tool description -- optionally with the INSTANCE named.

    Two variants. The named one ships only when BOTH hold: the operator of
    THIS instance switched it on
    (`FEDERATION_PEER_NAME_IN_TOOL_DESCRIPTION_ENABLED`, default off), and the
    far side has told us a name in a VERIFIED attachment
    (`peer_users.remote_instance_name`) of which something usable remains
    after `prompt_safe_instance_name`.

    🛑 Why a switch of its own: the name is text the FAR side chooses, and it
    can change it with any query or ping. A tool description is part of the
    agent's prompt. Without the switch a paired peer could rewrite a sentence
    of this household's prompt whenever it liked, with nobody here agreeing to
    it. With the switch off the description is the measured text, byte for
    byte, whatever name is stored.

      no name   the text measured on 2026-10-29, byte for byte.
      a name    the same text with only the OPENING changed, from "Ask
                <person>'s Renfield" to "Ask the Renfield instance „<name>“
                (paired by <person>)". Everything from the first dash on is
                identical, including the scanner sentence at the end.

    🛑 Measured, not guessed -- the NO-NAME variant (2026-10-29,
    `bin/measure-preselect.py` against the REAL prompt of the live turn,
    signature `aa62fdb7`): the version before it hit 27/39 (69 %), this one
    36/39 (92 %).

    The version before named only the PERSON. "frag <jemand>s Renfield" hit 3/3
    with it -- the model recognises the pattern, not the name, because a
    question naming a different person hit too, although that person is not
    the name. What hit 0/3 was every question naming the INSTANCE ("was liegt
    in x-idra?", "weiss die andere Instanz ...") or the PLURAL ("suche in allen
    Renfields"). Those three are now 3/3.

    🛑 The scanner sentence at the end is not decoration: "scanne nach x-idra"
    names a FILING TARGET, not an instance, and has to stay with the scanner.
    It does (3/3) -- but only as long as the sentence is not cut off, see the
    truncation of the names below.

    What that text did NOT achieve: "Welche Dokumente sind in der x-idra
    instanz ..." stayed 0/3, even with three different document phrasings
    (V3/V4 measured, both 92 %, no gain). For that the INSTANCE NAME has to
    appear here -- which is what the second variant does.

    🛑 The NAMED variant is NOT measured yet. It targets exactly that open 0/3,
    but it also puts the instance name next to "scanne nach <Ziel>", where the
    same word is a filing target. Whether the scanner sentence still wins must
    be established with `bin/measure-preselect.py`, not assumed.

    🛑 Only the verified name, never the endpoint-host fallback the UI shows: a
    host name is a label of last resort for a person reading a list, and nobody
    says it in a question. Keeping it out also keeps the measured text in place
    until a real name arrives.

    The name is text from the FAR SIDE inside a prompt, so the description
    never takes the display string: it takes an ALLOW-LIST form of it (letters,
    digits, underscore, space, dot, hyphen; at most 32 characters). Nothing
    that could close the quotes it is set in, start a new sentence with
    punctuation of its own, or carry an instruction of any length survives.
    """
    person = _short_name(peer.remote_display_name)
    instance = (
        prompt_safe_instance_name(stored_instance_name(peer))
        if settings.federation_peer_name_in_tool_description_enabled
        else ""
    )
    opening = (
        f"Ask the Renfield instance „{instance}“ (paired by {person})"
        if instance
        else f"Ask {person}'s Renfield"
    )
    return (
        f"{opening} — "
        f"another paired INSTANCE — for information its people chose "
        f"to share with you through circles. Use for „frag "
        f"<jemand>s Renfield“, „was liegt in <Instanz>“, "
        f"„die andere Instanz“ and „in allen Renfields "
        f"suchen“. NOT for scanning paper: in „scanne nach "
        f"<Ziel>“ <Ziel> is a filing target, not an instance."
    )


#: Longest instance name the description takes. Shorter than the 64 the
#: display allows: enough for a name, too little for a sentence.
PROMPT_NAME_MAX_CHARS = 32

#: Besides letters, digits and the underscore (`str.isalnum()` / "_"), the only
#: characters an instance name may bring into the prompt.
_PROMPT_NAME_EXTRA = frozenset(" .-_")


def prompt_safe_instance_name(name: str | None) -> str:
    """The allow-list form of an instance name, for the tool description.

    NFKC first, so a look-alike (fullwidth, ligature, compatibility form) is
    judged as the character it stands for. Then ONLY letters, digits,
    underscore, space, dot and hyphen are kept -- every quotation mark, colon,
    bracket, newline and symbol is dropped, not escaped. Runs of spaces
    collapse; the result is cut to `PROMPT_NAME_MAX_CHARS`.

    Returns "" when nothing nameable remains (no letter or digit at all): the
    caller then uses the unnamed description.
    """
    if not isinstance(name, str) or not name:
        return ""
    folded = unicodedata.normalize("NFKC", name)
    # Whitespace of any kind (a newline included) becomes ONE plain space.
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in folded
        if ch.isspace() or ch.isalnum() or ch in _PROMPT_NAME_EXTRA
    )
    cleaned = " ".join(kept.split())[:PROMPT_NAME_MAX_CHARS].strip(" .-_")
    if not any(ch.isalnum() for ch in cleaned):
        return ""
    return cleaned


#: How much of the display name may go into the description.
#: 🛑 The description is TRUNCATED at `_PRESELECT_DESC_CHARS` (480), and the
#: scanner-delimiting sentence stands at the END. A long display name pushes
#: exactly that sentence over the edge -- and then "scanne nach x-idra" becomes
#: a peer query again. The name comes from the FAR SIDE
#: (`offer.display_name`), so it is not under our own control. With this
#: truncation the description stays at 358 characters at most without an
#: instance name and at 424 with one (BOTH names at the cap), whatever they say.
_NAME_IN_DESC_CHARS = 40


def _short_name(name: str) -> str:
    """The display name, truncated -- see `_NAME_IN_DESC_CHARS`."""
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


#: The manager the registry last synced into. A weak reference: the registry
#: must not keep a manager alive, and holds no rows (see the module docstring).
_bound_manager: weakref.ReferenceType[MCPManager] | None = None


async def resync_bound_manager() -> bool:
    """Rebuild the peer entries of the manager `sync_peers` last ran against.

    For callers that change what a peer's tool description says but have no
    route to `app.state` -- today that is a far instance's name arriving in a
    verified attachment (`federation_instance_name.remember_remote_instance_name`).
    Opens its own session. Returns whether a manager was there to sync.
    """
    manager = _bound_manager() if _bound_manager is not None else None
    if manager is None:
        return False
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await sync_peers(manager, db)
    return True


async def sync_peers(manager: MCPManager, db: AsyncSession) -> None:
    """
    Rebuild the federation-transport entries in MCPManager from the
    current set of non-revoked PeerUser rows.

    Idempotent: can be called at startup AND after a pair/unpair event
    without accumulating duplicates. Any existing peer entries whose
    PeerUser is revoked or deleted are removed from the manager.
    """
    global _bound_manager
    _bound_manager = weakref.ref(manager)

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
