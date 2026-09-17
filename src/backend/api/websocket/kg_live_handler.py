"""
Live Knowledge Graph WebSocket endpoint.

Pushes new entities and relations to connected graph viewers in real-time
as KG extraction runs after conversations.

Security (review H3): the socket is authenticated and the broadcast is
OWNER-scoped. KG extraction belongs to exactly one user (the conversation /
document owner); a live update is only delivered to viewers authenticated as
that owner. This is intentionally conservative (fail-closed — public/tier-reach
sharing is NOT live-pushed; the page's REST queries are circle-filtered and
surface that on refresh), eliminating the prior leak where any anonymous viewer
received every household member's freshly-extracted private entity names. When
WS auth is disabled (single-user/household mode) the owner boundary doesn't
apply and updates fan to all viewers as before.
"""

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from services.websocket_auth import WSAuthError, authenticate_websocket
from utils.config import settings

router = APIRouter()

# Connected graph viewers: WebSocket -> authenticated user_id (int, or None for
# auth-skipped / device-token connections).
_viewers: dict[WebSocket, int | None] = {}


async def broadcast_kg_update(
    entities: list[dict],
    relations: list[dict],
    owner_user_id: int | None = None,
) -> None:
    """Broadcast new KG entities/relations to connected graph viewers.

    Owner-scoped when auth is enabled: only viewers authenticated as
    ``owner_user_id`` receive the update (fail-closed — an unknown owner or a
    None-user viewer gets nothing). Fire-and-forget: failures are logged but
    never propagate.
    """
    if not _viewers or (not entities and not relations):
        return

    message = {
        "type": "kg_update",
        "entities": entities,
        "relations": relations,
    }

    auth_on = settings.auth_enabled
    broken: list[WebSocket] = []
    for ws, viewer_user_id in _viewers.items():
        # Auth on → only the owner of the extracted data sees it live.
        if auth_on and not (
            owner_user_id is not None and viewer_user_id == owner_user_id
        ):
            continue
        try:
            await ws.send_json(message)
        except Exception:
            broken.append(ws)

    for ws in broken:
        _viewers.pop(ws, None)


@router.websocket("/ws/knowledge-graph")
async def knowledge_graph_live(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token"),
):
    """WebSocket endpoint for live KG graph updates (authenticated)."""
    auth_result = await authenticate_websocket(websocket, token)
    if not auth_result:
        await websocket.close(
            code=WSAuthError.UNAUTHORIZED, reason="Authentication required"
        )
        return

    await websocket.accept()
    viewer_user_id = (
        auth_result.get("user_id") if isinstance(auth_result, dict) else None
    )
    _viewers[websocket] = viewer_user_id
    logger.info(
        f"📊 KG graph viewer connected (user_id={viewer_user_id}, "
        f"{len(_viewers)} total)"
    )

    try:
        # Keep connection alive; we only push, never receive meaningful messages
        while True:
            # Wait for client messages (ping/pong handled by framework)
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"KG viewer connection error: {e}")
    finally:
        _viewers.pop(websocket, None)
        logger.info(f"📊 KG graph viewer disconnected ({len(_viewers)} total)")
