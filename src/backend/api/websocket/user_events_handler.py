"""``/ws/user`` — the per-user server→browser event socket.

A browser opens ONE of these app-wide (any authenticated page). It carries no
inbound protocol beyond client heartbeat pings (which the receive loop reads and
ignores to keep the ingress connection warm); everything meaningful flows
server→client as content-free events fanned out by the Redis subscriber
(:mod:`services.user_events`).

Modeled on ``api/websocket/kiosk_handler.py``'s connection lifecycle, but keyed by
``user_id`` instead of a single admin set. Origin (CSWSH) protection is inherited
for free — :func:`authenticate_websocket` runs ``_ws_origin_allowed`` first.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from services.user_events import ALL, get_registry
from services.websocket_auth import WSAuthError, authenticate_websocket

router = APIRouter()


async def _is_admin(user_id: int) -> bool:
    """Whether ``user_id`` currently holds ADMIN — admins also join the ``ALL``
    bucket so unattributable (``owner=None``) changes reach them. Best-effort: a
    lookup failure just means "not admin" (no ALL membership)."""
    try:
        from services.auth_service import active_admin_ids
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return user_id in await active_admin_ids(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"user-events: admin check failed for {user_id}: {exc}")
        return False


@router.websocket("/ws/user")
async def user_events_ws(websocket: WebSocket, token: str = Query(None)) -> None:
    auth = await authenticate_websocket(websocket, token)
    if not auth:
        await websocket.close(code=WSAuthError.UNAUTHORIZED, reason="Authentication required")
        return

    registry = get_registry()
    keys: list[int | str] = []

    if auth.get("auth_skipped"):
        # Auth-off single-user household — one broadcast bucket, no per-user id.
        keys = [ALL]
    else:
        user_id = auth.get("user_id")
        if not isinstance(user_id, int):
            # A device/voice token with no user has no per-user inbox here.
            await websocket.close(code=WSAuthError.UNAUTHORIZED, reason="No user context")
            return
        keys = [user_id]
        if await _is_admin(user_id):
            keys.append(ALL)

    await websocket.accept()
    for key in keys:
        registry.register(key, websocket)
    logger.debug(f"user-events: client connected (keys={keys}, total={registry.client_count()})")
    try:
        # Push-only: idle on receive. Client heartbeat pings arrive here and are
        # ignored — they exist only to keep the connection warm past ingress idle
        # timeouts. A disconnect breaks the loop.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — any receive error ends the connection cleanly
        logger.debug(f"user-events: receive loop ended: {exc}")
    finally:
        registry.unregister(websocket)
        logger.debug(f"user-events: client disconnected (total={registry.client_count()})")
