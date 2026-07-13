"""
Live Kiosk WebSocket endpoint (the /kiosk wall-display push hub).

Precedent: this is the second instance of the ``kg_live_handler`` shape — a
module-level client registry + a fire-and-forget ``broadcast_*`` called from
wherever the source event happens + a ``/ws/...`` endpoint that accepts,
registers, hydrates, and cleans up on disconnect. The only structural
differences from ``kg_live_handler`` are:

  * the registry is a plain ``set`` — kiosk content is **household-wide** by
    design (like the Command Center it replaces), so there is no per-owner
    scoping to carry; and
  * the connect gate requires ``Permission.ADMIN`` (mirroring ``<AdminRoute>``
    on the page), not merely authentication; and
  * on connect the server sends one ``snapshot`` message (hydrate) before the
    idle receive-loop (subscribe) — the standard hydrate-then-subscribe pattern
    so a fresh / reconnecting tab has all current state immediately.

Privacy bar (non-negotiable): every message this hub emits is CONTENT-FREE —
ids, names, counts, health, and state strings only. Never an utterance, never
an entity name, never a user id. See ``tasks/kiosk-active-subsystem-plan.md`` §5.

Protocol (hydrate-then-subscribe). On connect: one ``snapshot`` (all keys below,
one-time compute). Then small delta events as they happen — the frontend folds
each into the same reducer-held model. Every event is CONTENT-FREE.

  * ``snapshot`` — ``{type, at, satellites[], presence{rooms[],people_present,
    occupied_rooms}, mcp{enabled,total_tools,servers[]}, tool_health[], roles[],
    activity[], peers[], weather|null, now_playing[]}``.
  * ``satellite_state`` — ``{type, satellite_id, room, room_id, state}`` — a
    satellite's SESSION state changed (idle/listening/processing/speaking/error).
  * ``satellite_online`` / ``satellite_offline`` — ``{type, satellite_id, room,
    room_id, online}`` — a satellite REGISTERED (connect/reconnect) or DROPPED
    (disconnect / heartbeat-timeout). The liveness signal the frontend needs so a
    crashed satellite stops pinning the voice core and a resumed one reappears —
    distinct from ``satellite_state`` (which only carries the session state of an
    already-online satellite). Phase 2.
  * ``presence_changed`` — ``{type, rooms:[{room_id,room_name,occupants}],
    people_present, occupied_rooms}`` — fires ONLY on an actual room-occupant-set
    change (someone entered/left a room), never on a bare BLE RSSI tick. Same
    shape as the snapshot's ``presence`` section. Phase 2.
  * ``turn_activity`` — ``{type, role, subsystems[], ok, at}`` — one completed
    chat turn (role + the subsystems it touched).
  * ``now_playing_changed`` — ``{type, sessions:[…]}`` — the deduped per-room
    PLAYING media set changed (start/stop/track/room move). ``sessions`` has the
    same shape as the snapshot's ``now_playing``. Phase 2.
  * ``tool_health_changed`` — ``{type, server, connected}`` — an MCP server's
    connection flipped (reconnect healed it / it dropped). Phase 2.
  * ``weather_updated`` — ``{type, weather:{…}|null}`` — pushed when the
    backend-internal weather cache refreshes (never a client poll). Phase 2.
  * ``chat_activity`` — ``{type, active}`` — pushed on the 0↔1 edge of the
    web-chat turn counter, so the core shows "processing" while Renfield handles
    a TYPED turn (voice already drives the core via satellite_state). Snapshot
    carries ``chat_active``.
  * ``peer_status_changed`` — ``{type, peers:[…]}`` — pushed when federation-peer
    reachability changes. A backend timer recomputes it (no discrete federation
    event exists) and diff-pushes; the kiosk peer nodes go green/red live instead
    of only on reconnect. See ``kiosk_data.refresh_and_push_peer_status``.
"""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from models.permissions import Permission
from services.auth_service import get_user_by_id
from services.database import AsyncSessionLocal
from services.websocket_auth import WSAuthError, authenticate_websocket

router = APIRouter()

# Connected kiosk wall displays. No per-viewer scoping: the kiosk projection is
# household-wide (ADMIN-gated at connect), so every client sees every event.
_kiosk_clients: set[WebSocket] = set()

# How many web-chat turns are being processed right now (across all chat sockets
# on this process). Lets the kiosk core show "processing" while Renfield handles
# ANY turn — typed web-chat, not just satellite voice (rooms stay satellite-
# driven; a web-chat turn has no room). Counter (not a bool) so concurrent turns
# don't cancel each other; floored at 0.
_active_chat_turns = 0


async def note_chat_turn_active(active: bool) -> None:
    """Mark a web-chat turn as starting (``True``) / ending (``False``) and push a
    ``chat_activity`` delta only on the 0↔1 edge (so redundant concurrent-turn
    increments don't spam the hub). Fire-and-forget; a hub failure must never
    break the chat turn."""
    global _active_chat_turns
    was_active = _active_chat_turns > 0
    _active_chat_turns = max(0, _active_chat_turns + (1 if active else -1))
    now_active = _active_chat_turns > 0
    if now_active != was_active:
        try:
            await broadcast_kiosk_event({"type": "chat_activity", "active": now_active})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"kiosk chat_activity broadcast failed: {e}")


# Per-client send deadline. A backpressured wall display (tab asleep, WiFi
# dropped → its send buffer fills) is pruned after this instead of hanging the
# broadcast pipeline forever.
_SEND_TIMEOUT_SECONDS = 5.0

# Serialized broadcast pipeline: callers ENQUEUE (non-blocking) and a single
# consumer task drains it FIFO. Deliberately a queue+single-consumer, NOT a
# task-per-event — per-event tasks would (a) deliver deltas out of ORDER and
# (b) call send_json CONCURRENTLY on the same Starlette WebSocket (unsafe → a
# spurious error would prune a healthy display). The consumer handles one event
# at a time, so each socket is sent to sequentially; within one event the
# fan-out across DIFFERENT sockets is concurrent (safe). Created lazily in the
# running loop (so the queue binds to the app's loop, and each test's loop gets
# a fresh one) and revived if the consumer ever dies.
_event_queue: "asyncio.Queue[dict] | None" = None
_consumer_task: "asyncio.Task | None" = None
_consumer_loop = None  # the loop the queue+consumer are bound to


def _ensure_consumer() -> "asyncio.Queue[dict]":
    """Return the broadcast queue, (re)creating the queue + consumer bound to the
    CURRENTLY-running loop. Rebinding on a loop change matters because an
    ``asyncio.Queue``/``Task`` is tied to the loop it was made in: if the app (or
    a pytest-asyncio per-test) loop is ever replaced, a stale consumer left
    pending on the dead loop reads ``.done() == False`` and would otherwise
    silently starve the new loop's broadcasts."""
    global _event_queue, _consumer_task, _consumer_loop
    loop = asyncio.get_running_loop()
    if _event_queue is None or _consumer_loop is not loop:
        _event_queue = asyncio.Queue()
        _consumer_loop = loop
        _consumer_task = None
    if _consumer_task is None or _consumer_task.done():
        _consumer_task = asyncio.create_task(_broadcast_consumer())
    return _event_queue


async def broadcast_kiosk_event(event: dict) -> None:
    """Enqueue one content-free kiosk delta for delivery to every wall display.

    NON-BLOCKING: the caller — which may hold the ``SatelliteManager`` lock (a
    blocked send there would freeze voice house-wide) or be mid chat-turn before
    the ``done`` frame — enqueues and returns; the background consumer does the
    ordered fan-out. No-op when no client is connected (the common case —
    publish-point cost stays ~zero when nobody is watching the kiosk).
    """
    if not _kiosk_clients:
        return
    _ensure_consumer().put_nowait(event)


async def _send_one(ws: WebSocket, event: dict) -> None:
    await asyncio.wait_for(ws.send_json(event), timeout=_SEND_TIMEOUT_SECONDS)


async def _broadcast_consumer() -> None:
    """Drain the queue FIFO; fan each event out to all current clients
    concurrently (across distinct sockets), pruning any whose send fails or
    times out. One event at a time → no concurrent send on a single socket and
    strict delivery order. A bad event or a dead socket never kills the loop."""
    assert _event_queue is not None
    while True:
        event = await _event_queue.get()
        try:
            clients = list(_kiosk_clients)
            if clients:
                results = await asyncio.gather(
                    *(_send_one(ws, event) for ws in clients),
                    return_exceptions=True,
                )
                for ws, result in zip(clients, results, strict=True):
                    if isinstance(result, Exception):
                        _kiosk_clients.discard(ws)
        except Exception as e:  # never let one event kill the consumer
            logger.debug(f"kiosk broadcast consumer error: {e}")
        finally:
            _event_queue.task_done()


# ---------------------------------------------------------------------------
# Snapshot builder — the one-time hydrate compute sent on connect. Reuses the
# exact source calls the REST layer already reads; no new query logic.
# ---------------------------------------------------------------------------

# Tools whose per-(user,tool) success rate is below this are "degraded" in the
# aggregated, user-id-free kiosk health view.
_TOOL_HEALTH_DEGRADED_BELOW = 0.5

def build_presence_payload(presence) -> dict:
    """Content-free rooms→occupant-count rollup of the live presence map.

    The one shared shape behind both the connect ``snapshot``'s ``presence``
    section and the ``presence_changed`` delta (``presence_service`` calls this
    when an occupant set actually changes). Never emits user ids — only room
    ids/names and integer counts.
    """
    rooms: dict[int, dict] = {}
    for pres in presence.get_all_presence().values():
        key = pres.room_id
        if key is None:
            continue
        room = rooms.setdefault(
            key, {"room_id": key, "room_name": pres.room_name, "occupants": 0}
        )
        room["occupants"] += 1
    room_list = list(rooms.values())
    return {
        "rooms": room_list,
        "people_present": sum(r["occupants"] for r in room_list),
        "occupied_rooms": len(room_list),
    }


async def build_kiosk_snapshot(app) -> dict:
    """Compute the full current kiosk state ONCE, for the connect ``snapshot``.

    Every section is best-effort: a failing source degrades to an empty/None
    value rather than aborting the whole snapshot (a fresh kiosk tab must always
    hydrate). CONTENT-FREE by construction — see the module docstring.
    """
    snapshot: dict = {
        "type": "snapshot",
        "at": datetime.now(UTC).isoformat(),
        "satellites": [],
        "presence": {"rooms": [], "people_present": 0, "occupied_rooms": 0},
        "mcp": {"enabled": False, "total_tools": 0, "servers": []},
        "tool_health": [],
        "internal_health": [],
        "roles": [],
        "activity": [],
        "peers": [],
        "weather": None,
        "now_playing": [],
        # True while ≥1 web-chat turn is being processed → the core shows
        # "processing" even with no satellite active (reconnect hydrate).
        "chat_active": _active_chat_turns > 0,
    }

    # --- Satellites (roster + live state) --------------------------------
    try:
        from ha_glue.services.satellite_manager import get_satellite_manager

        snapshot["satellites"] = get_satellite_manager().get_all_satellites()
    except Exception as e:
        logger.debug(f"kiosk snapshot: satellites unavailable: {e}")

    # --- Presence (rooms → occupant counts; no user ids) -----------------
    try:
        from ha_glue.services.presence_service import get_presence_service

        snapshot["presence"] = build_presence_payload(get_presence_service())
    except Exception as e:
        logger.debug(f"kiosk snapshot: presence unavailable: {e}")

    # --- MCP connection status + tool counts -----------------------------
    mcp_manager = getattr(app.state, "mcp_manager", None)
    if mcp_manager is not None:
        try:
            snapshot["mcp"] = mcp_manager.get_status()
        except Exception as e:
            logger.debug(f"kiosk snapshot: mcp status unavailable: {e}")

    # --- Tool-health classification (aggregated, user-id-free) -----------
    try:
        from services.tool_outcome_service import ToolOutcomeService

        async with AsyncSessionLocal() as db:
            stats = await ToolOutcomeService(db).list_stats(limit=500)
        agg: dict[str, dict] = {}
        for st in stats:
            row = agg.setdefault(
                st.tool_name, {"tool_name": st.tool_name, "success": 0, "failure": 0}
            )
            row["success"] += st.success_count
            row["failure"] += st.failure_count
        tool_health: list[dict] = []
        for row in agg.values():
            total = row["success"] + row["failure"]
            rate = (row["success"] / total) if total else 1.0
            tool_health.append(
                {
                    "tool_name": row["tool_name"],
                    "total": total,
                    "success_rate": round(rate, 3),
                    "degraded": total > 0 and rate < _TOOL_HEALTH_DEGRADED_BELOW,
                }
            )
        snapshot["tool_health"] = tool_health
    except Exception as e:
        logger.debug(f"kiosk snapshot: tool health unavailable: {e}")

    # --- Internal-subsystem health (knowledge / presence / media) --------
    try:
        from api.websocket.kiosk_data import compute_internal_subsystem_health

        snapshot["internal_health"] = await compute_internal_subsystem_health()
    except Exception as e:
        logger.debug(f"kiosk snapshot: internal health unavailable: {e}")

    # --- Agent roles (availability-filtered, as the router sees them) ----
    try:
        agent_router = getattr(app.state, "agent_router", None)
        if agent_router is not None and getattr(agent_router, "roles", None):
            snapshot["roles"] = [
                {
                    "name": role.name,
                    "description": role.description,
                    "mcp_servers": role.mcp_servers,
                    "internal_tools": role.internal_tools,
                    "has_agent_loop": role.has_agent_loop,
                }
                for role in agent_router.roles.values()
            ]
    except Exception as e:
        logger.debug(f"kiosk snapshot: roles unavailable: {e}")

    # --- Recent role activations (content-free pulse history) ------------
    try:
        from api.websocket.kiosk_data import recent_role_activity_entries

        async with AsyncSessionLocal() as db:
            entries = await recent_role_activity_entries(db, limit=30)
        snapshot["activity"] = [
            {"role": e.role, "at": e.at.isoformat() if e.at else None, "ok": e.ok}
            for e in entries
        ]
    except Exception as e:
        logger.debug(f"kiosk snapshot: activity unavailable: {e}")

    # --- Federation peers (reachability, no message content) -------------
    # Shared with the peer_status_changed refresher so snapshot + delta agree.
    try:
        from api.websocket.kiosk_data import compute_peer_status

        snapshot["peers"] = await compute_peer_status()
    except Exception as e:
        logger.debug(f"kiosk snapshot: peers unavailable: {e}")

    # --- Weather tile (process-cached; None hides the tile) --------------
    try:
        from api.websocket.kiosk_data import compute_kiosk_weather

        weather = await compute_kiosk_weather(mcp_manager)
        if weather is not None:
            snapshot["weather"] = weather.model_dump()
    except Exception as e:
        logger.debug(f"kiosk snapshot: weather unavailable: {e}")

    # --- Now-playing tile (one per room, PLAYING only, no user ids) ------
    try:
        from ha_glue.utils.config import ha_glue_settings

        if ha_glue_settings.media_follow_enabled:
            from ha_glue.services.media_follow_service import get_media_follow_service

            snapshot["now_playing"] = get_media_follow_service().active_sessions()
    except Exception as e:
        logger.debug(f"kiosk snapshot: now-playing unavailable: {e}")

    return snapshot


@router.websocket("/ws/kiosk")
async def kiosk_live(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token"),
):
    """WebSocket endpoint for the live kiosk projection (ADMIN-gated).

    Gate: authenticate, then require ``Permission.ADMIN`` (mirroring
    ``<AdminRoute>``) UNLESS auth is disabled (single-user/household mode, where
    ``authenticate_websocket`` returns ``auth_skipped`` and the household is
    trusted). An unauthenticated or non-admin client must not open this socket
    and harvest the household-wide snapshot.
    """
    auth_result = await authenticate_websocket(websocket, token)
    if not auth_result:
        await websocket.close(
            code=WSAuthError.UNAUTHORIZED, reason="Authentication required"
        )
        return

    # Auth disabled → single-user/household mode, no per-user permission model.
    if not auth_result.get("auth_skipped"):
        user_id = auth_result.get("user_id") if isinstance(auth_result, dict) else None
        is_admin = False
        if user_id is not None:
            try:
                async with AsyncSessionLocal() as db:
                    user = await get_user_by_id(db, user_id)
                is_admin = bool(user and user.has_permission(Permission.ADMIN))
            except Exception as e:
                logger.warning(f"kiosk WS admin check failed: {e}")
                is_admin = False
        if not is_admin:
            await websocket.close(
                code=WSAuthError.UNAUTHORIZED, reason="Admin permission required"
            )
            return

    await websocket.accept()

    # Hydrate BEFORE registering. If we joined _kiosk_clients first, the
    # single-consumer could fan a delta out to this socket (a send_json) while
    # this coroutine is still awaiting/sending its own snapshot on the SAME
    # socket — a concurrent send (unsafe on a Starlette WebSocket), and the
    # client could also apply a delta before it has hydrated. Sending the
    # snapshot while unregistered closes both. Cost: a delta landing in the tiny
    # build+send window is missed by this socket — fine for an ambient display
    # (the next delta for that entity corrects it; a reconnect re-snapshots).
    try:
        snapshot = await build_kiosk_snapshot(websocket.app)
        await websocket.send_json(snapshot)
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.debug(f"Kiosk hydrate failed: {e}")
        return

    _kiosk_clients.add(websocket)  # now broadcast-eligible
    # Reset the internal-health diff-gate so the next refresher tick re-pushes the
    # current verdicts — the gate isn't advanced while no kiosk is connected, so
    # it can hold a stale pre-gap value that would otherwise suppress a real delta.
    from api.websocket.kiosk_data import reset_internal_health_gate, reset_peer_status_gate

    reset_internal_health_gate()
    reset_peer_status_gate()
    logger.info(f"🖥️ Kiosk display connected ({len(_kiosk_clients)} total)")

    try:
        # Push-only channel: idle on receive (ping/pong handled by framework).
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Kiosk display connection error: {e}")
    finally:
        _kiosk_clients.discard(websocket)
        logger.info(f"🖥️ Kiosk display disconnected ({len(_kiosk_clients)} total)")
