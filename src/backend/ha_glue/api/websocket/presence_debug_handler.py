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

Two things differ from ``/ws/kiosk`` because this stream is personal data, not
a content-free projection:

  * A refusal is sent as an application close code AFTER accepting the
    handshake. Closing before ``accept`` answers the upgrade with HTTP 403,
    which a browser reports as close code 1006 — indistinguishable from a
    network failure, so the page would "reconnect" forever instead of saying
    "access denied". Nothing is sent on a refused socket before the close.
    Both refusals deny; they differ in what the client may do next:

      - ``WSAuthError.FORBIDDEN`` (4403): the user was loaded and does NOT
        hold ADMIN. Confirmed, final — the client shows "no access" and stops.
      - ``WSAuthError.UNAUTHORIZED`` (4001): the server could not establish
        who is asking — no/expired token, no user behind it, or the permission
        lookup failed. Still refused (fail closed), but possibly temporary
        (rollout, token refresh), so the client retries with backoff.
  * The socket has a maximum lifetime. The ADMIN check runs at the handshake
    only, so a revoked role or an ended session would otherwise keep reading
    for as long as the tab stays open. After ``MAX_SESSION_SECONDS`` the server
    closes normally (1000); the client reconnects at once and the handshake
    checks again.
"""

import asyncio

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from ha_glue.services import presence_debug
from models.permissions import Permission
from services.auth_service import get_user_by_id
from services.database import AsyncSessionLocal
from services.websocket_auth import WSAuthError, authenticate_websocket

router = APIRouter()

# Upper bound on one socket's life: the longest a revoked ADMIN role or an
# ended login can keep receiving movement traces. Short enough to bound that,
# long enough that the re-handshake (one token fetch + one snapshot) is noise.
MAX_SESSION_SECONDS = 15 * 60

# Normal closure — tells the client "planned, reconnect right away".
SESSION_RENEWAL_CLOSE_CODE = 1000


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Refuse with a close code the browser can read (see module docstring)."""
    try:
        await websocket.accept()
        await websocket.close(code=code, reason=reason)
    except Exception as e:
        logger.debug(f"presence-debug refusal could not be delivered: {e}")


async def _admin_verdict(user_id: int | None) -> bool | None:
    """``True`` = holds ADMIN. ``False`` = the user was loaded and does not —
    a confirmed refusal. ``None`` = could not tell (no user behind the token,
    unknown user, lookup error). Only ``True`` opens the socket; ``None`` and
    ``False`` differ solely in the close code (see module docstring)."""
    if user_id is None:
        return None
    # The whole check sits in the try: an error anywhere in it — loading the
    # user OR evaluating the permission — is "could not tell", never a crash
    # before the handshake is answered.
    try:
        async with AsyncSessionLocal() as db:
            user = await get_user_by_id(db, user_id)
        if user is None:
            return None
        return bool(user.has_permission(Permission.ADMIN))
    except Exception as e:
        logger.warning(f"presence-debug WS permission check failed: {e}")
        return None


@router.websocket("/ws/presence-debug")
async def presence_debug_live(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token"),
):
    auth_result = await authenticate_websocket(websocket, token)
    if not auth_result:
        await _refuse(websocket, WSAuthError.UNAUTHORIZED, "Authentication required")
        return

    if not auth_result.get("auth_skipped"):
        verdict = await _admin_verdict(auth_result.get("user_id"))
        if verdict is None:
            await _refuse(
                websocket, WSAuthError.UNAUTHORIZED, "could not verify permission"
            )
            return
        if not verdict:
            await _refuse(websocket, WSAuthError.FORBIDDEN, "admin permission required")
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
        async with asyncio.timeout(MAX_SESSION_SECONDS):
            while True:
                await websocket.receive_text()
    except TimeoutError:
        # Lifetime reached: leave the registry FIRST (so the flush task cannot
        # send into the closing socket), then close normally.
        presence_debug.unregister_viewer(websocket)
        try:
            await websocket.close(
                code=SESSION_RENEWAL_CLOSE_CODE, reason="session renewal"
            )
        except Exception as e:
            logger.debug(f"presence-debug renewal close failed: {e}")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"presence-debug connection error: {e}")
    finally:
        presence_debug.unregister_viewer(websocket)
        logger.info(
            f"Presence diagnostics viewer disconnected ({presence_debug.viewer_count()} total)"
        )
