"""
``/ws/presence-debug`` — live BLE readings for the admin presence page.

Hydrate-then-subscribe, like ``/ws/kiosk``: one ``snapshot`` on connect, then a
fresh ``snapshot`` after satellite BLE reports (coalesced, see
``ha_glue.services.presence_debug``). Full snapshots, no deltas — the payload is
small and a viewer that missed one frame is corrected by the next.

Gate: ``Permission.ADMIN``, same as ``GET /api/presence/debug/sightings`` — the
payload is every tracked person's movement trace. Fails closed: no user, a
missing permission or a failed lookup all refuse the socket. With auth disabled
(single-user mode) there is no permission model and the socket opens, exactly
like the REST route.
"""

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from ha_glue.services import presence_debug
from models.permissions import Permission
from services.auth_service import get_user_by_id
from services.database import AsyncSessionLocal
from services.websocket_auth import WSAuthError, authenticate_websocket

router = APIRouter()


async def _is_admin(user_id: int | None) -> bool:
    """Whether ``user_id`` holds ADMIN. A lookup error denies."""
    if user_id is None:
        return False
    try:
        async with AsyncSessionLocal() as db:
            user = await get_user_by_id(db, user_id)
        return bool(user and user.has_permission(Permission.ADMIN))
    except Exception as e:
        logger.warning(f"presence-debug WS permission check failed: {e}")
        return False


@router.websocket("/ws/presence-debug")
async def presence_debug_live(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token"),
):
    auth_result = await authenticate_websocket(websocket, token)
    if not auth_result:
        await websocket.close(
            code=WSAuthError.UNAUTHORIZED, reason="Authentication required"
        )
        return

    if not auth_result.get("auth_skipped") and not await _is_admin(
        auth_result.get("user_id")
    ):
        await websocket.close(
            code=WSAuthError.UNAUTHORIZED, reason="admin permission required"
        )
        return

    await websocket.accept()

    # Hydrate BEFORE registering, so the flush task cannot send on this socket
    # while its own snapshot is still going out (no concurrent send_json).
    try:
        await websocket.send_json(
            {"type": "snapshot", **presence_debug.build_current_payload()}
        )
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.debug(f"presence-debug hydrate failed: {e}")
        return

    presence_debug.register_viewer(websocket)
    logger.info(f"Presence diagnostics viewer connected ({presence_debug.viewer_count()} total)")
    try:
        # Push-only: idle on receive; a disconnect ends the loop.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"presence-debug connection error: {e}")
    finally:
        presence_debug.unregister_viewer(websocket)
        logger.info(
            f"Presence diagnostics viewer disconnected ({presence_debug.viewer_count()} total)"
        )
