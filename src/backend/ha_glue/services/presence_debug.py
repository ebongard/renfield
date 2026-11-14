"""
Presence diagnostics — the raw BLE readings behind the room decision.

Two consumers, ONE payload builder:

  * ``GET /api/presence/debug/sightings`` (``ha_glue/api/routes/presence.py``)
  * the ADMIN-gated ``/ws/presence-debug`` socket
    (``ha_glue/api/websocket/presence_debug_handler.py``), which pushes the same
    payload: once on connect, then after BLE reports.

The push side is event-driven and costs nothing when nobody is watching:
``note_ble_report()`` is called by ``PresenceService.process_ble_report`` for
every satellite report and returns immediately unless a viewer is connected.
With a viewer, reports are coalesced to at most one snapshot per
``PUSH_MIN_INTERVAL_SECONDS`` (satellites report every ~3 s each, so a house
with six of them would otherwise push twice a second).

Privacy: the payload exposes movement traces of every tracked person, hence
ADMIN on both consumers. The per-device ``key`` is a redacted hash — never the
raw MAC, never the user-chosen IRK label.
"""

import asyncio
import hashlib
import time
from typing import Any

from fastapi import WebSocket
from loguru import logger

from ha_glue.utils.config import ha_glue_settings

# At most one pushed snapshot per this many seconds, however many satellites report.
PUSH_MIN_INTERVAL_SECONDS = 1.0

# Per-viewer send deadline: a stalled tab is dropped instead of holding up the others.
_SEND_TIMEOUT_SECONDS = 5.0

# Connected diagnostic viewers (admin tabs). Empty in normal operation.
_debug_clients: set[WebSocket] = set()

# Close code for a viewer the hub drops (send failed or stalled). The client
# reads any abnormal close as "reconnect and re-hydrate".
_DROP_CLOSE_CODE = 1011

_dirty = False
_flush_task: "asyncio.Task | None" = None
# Strong references to fire-and-forget close tasks (the loop keeps only weak ones).
_close_tasks: "set[asyncio.Task]" = set()
_last_push_monotonic = 0.0


def redact_device_key(key: str) -> str:
    """Stable, non-reversible short id for a device sighting key so the
    diagnostic can tell devices apart WITHOUT leaking the raw MAC or the
    user-chosen `irk:<label>` nickname (both are device-fingerprinting PII).
    Prefixed so an admin still sees whether it was MAC- or IRK-resolved."""
    prefix = "irk" if key.startswith("irk:") else "mac"
    digest = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"{prefix}:{digest}"


def _decision_settings() -> dict[str, Any]:
    """The numbers a viewer needs to interpret the readings."""
    return {
        "rssi_floor_dbm": ha_glue_settings.presence_rssi_threshold,
        "switch_enter_margin_db": ha_glue_settings.presence_switch_enter_margin_db,
        "stale_timeout_s": ha_glue_settings.presence_stale_timeout,
        # Voice hold: seconds a voice/auth sighting pins the room against BLE
        # (0 = off). Release margin: dB above the floor a room needs to take a
        # user out of a voice/auth room the phone was never heard in (0 = off).
        "voice_hold_s": ha_glue_settings.presence_voice_hold_seconds,
        "voice_release_margin_db": ha_glue_settings.presence_voice_release_margin_db,
        # False = legacy scan-count hysteresis: no filtered values, no margin.
        "rssi_filter_enabled": bool(ha_glue_settings.presence_rssi_filter_enabled),
    }


def _connected_satellites() -> list[dict[str, Any]]:
    """Roster of connected satellites (id + room), so a viewer can tell "did
    not hear the device" from "is not there". In-memory, no I/O. Best-effort:
    an unavailable manager yields an empty roster, never a failed payload."""
    try:
        from ha_glue.services.satellite_manager import get_satellite_manager

        return [
            {"satellite_id": sat_id, "room_id": sat.room_id, "room_name": sat.room}
            for sat_id, sat in get_satellite_manager().satellites.items()
        ]
    except Exception as e:
        logger.debug(f"presence debug: satellite roster unavailable: {e}")
        return []


def _voice_state(presence, cur, now: float) -> dict[str, Any]:
    """How a voice/auth sighting currently shapes the placement — without it the
    readings would seem to contradict the room the system reports.

    ``voice_hold_remaining_s``: seconds BLE still cannot move the user (null
    when no hold is running). ``voice_pinned``: the room rests on a voice/auth
    sighting BLE has not replaced yet. ``location_uncertain``: that room is a
    held guess (``PresenceService.is_location_uncertain``)."""
    if cur is None or cur.room_id is None:
        return {
            "voice_hold_remaining_s": None,
            "voice_pinned": False,
            "location_uncertain": False,
        }
    remaining = cur.voice_hold_until - now
    return {
        "voice_hold_remaining_s": round(remaining, 1) if remaining > 0 else None,
        "voice_pinned": bool(cur.voice_pinned),
        "location_uncertain": presence.is_location_uncertain(cur.user_id, now),
    }


def build_sightings_payload(presence) -> dict[str, Any]:
    """The diagnostic payload. ``presence`` is the ``PresenceService``, or
    ``None`` when presence detection is disabled (empty device list).

    Per tracked device: redacted key, user, the room the system currently
    places them in + confidence, the per-room filtered RSSI the switch margin
    compares, and every sighting still in the service's window. With the
    placement v2 shadow on, additionally ``placement_v2``: the shadow verdict
    as of this snapshot and its components (the key is absent with it off).
    While the clock is behind the shadow's last decided report the field
    carries ``held: true`` and the verdict of that report instead.
    """
    now = time.time()
    payload: dict[str, Any] = {
        "now": round(now, 3),
        "enabled": presence is not None,
        "settings": _decision_settings(),
        "satellites": [],
        "devices": [],
    }
    if presence is None:
        return payload

    payload["satellites"] = _connected_satellites()
    # Placement v2 shadow (None unless its flag is on): adds one field per
    # device below. Absent with the flag off, so the payload is unchanged.
    shadow = getattr(presence, "_placement_shadow", None)
    out = payload["devices"]
    for key, sightings in presence._sightings.items():
        uid = presence._user_for_key(key)
        cur = presence._presence.get(uid) if uid is not None else None
        filtered = cur.room_rssi_filtered if cur else {}
        entry = {
            "key": redact_device_key(key),
            "user_id": uid,
            "user_name": presence.get_user_name(uid) if uid is not None else None,
            "current_room_id": cur.room_id if cur else None,
            "current_room_name": cur.room_name if cur else None,
            "current_confidence": round(cur.confidence, 3) if cur else None,
            **_voice_state(presence, cur, now),
            "filtered_by_room": {
                (presence._room_names.get(rid, str(rid))): round(v, 2)
                for rid, v in filtered.items()
            },
            # Same values keyed by id, so a consumer can match the current room
            # without comparing display names.
            "filtered_rooms": [
                {
                    "room_id": rid,
                    "room_name": presence._room_names.get(rid),
                    "rssi": round(v, 2),
                }
                for rid, v in filtered.items()
            ],
            "sightings": [
                {
                    "satellite_id": s.satellite_id,
                    "room_id": s.room_id,
                    "room_name": (presence._room_names.get(s.room_id)
                                  if s.room_id is not None else None),
                    "rssi": s.rssi,
                    "timestamp": round(s.timestamp, 3),
                    "age_s": round(now - s.timestamp, 2),
                }
                for s in sorted(sightings, key=lambda x: x.timestamp)
            ],
        }
        if shadow is not None:
            # What the shadow decision says right now, with the numbers behind
            # it. Null when it could not be computed; nothing acts on it.
            entry["placement_v2"] = shadow.payload_for(presence, key, now)
        out.append(entry)
    return payload


def build_current_payload() -> dict[str, Any]:
    """``build_sightings_payload`` for the live service (or the disabled shape)."""
    if not ha_glue_settings.presence_enabled:
        return build_sightings_payload(None)
    from ha_glue.services.presence_service import get_presence_service

    return build_sightings_payload(get_presence_service())


# ---------------------------------------------------------------------------
# Push hub
# ---------------------------------------------------------------------------


def register_viewer(ws: WebSocket) -> None:
    _debug_clients.add(ws)


def unregister_viewer(ws: WebSocket) -> None:
    _debug_clients.discard(ws)


def viewer_count() -> int:
    return len(_debug_clients)


def note_ble_report() -> None:
    """A satellite BLE report was processed. No-op unless a viewer is connected
    (the normal case: one set lookup per report). Otherwise mark the state
    dirty and make sure the single flush task is running. Never raises — a
    diagnostic must not disturb presence tracking."""
    global _dirty, _flush_task
    if not _debug_clients:
        return
    try:
        _dirty = True
        if _flush_task is None or _flush_task.done():
            _flush_task = asyncio.get_running_loop().create_task(_flush())
    except Exception as e:
        logger.debug(f"presence debug: could not schedule push: {e}")


async def _close_quietly(ws: WebSocket) -> None:
    try:
        await asyncio.wait_for(ws.close(code=_DROP_CLOSE_CODE), timeout=_SEND_TIMEOUT_SECONDS)
    except Exception as e:
        logger.debug(f"presence debug: closing a dropped viewer failed: {e}")


def _drop_viewer(ws: WebSocket) -> None:
    """Take a viewer whose send failed or stalled out of the registry AND close
    its socket. Unregistering alone would leave the handler parked in
    ``receive_text`` and the browser on an open, silent socket — "Live" forever
    with frozen numbers. The close makes the client reconnect and re-hydrate.
    Fire-and-forget: a stalled peer must not hold up the flush loop."""
    _debug_clients.discard(ws)
    try:
        task = asyncio.get_running_loop().create_task(_close_quietly(ws))
        _close_tasks.add(task)
        task.add_done_callback(_close_tasks.discard)
    except Exception as e:
        logger.debug(f"presence debug: could not schedule viewer close: {e}")


async def _send_one(ws: WebSocket, message: dict[str, Any]) -> None:
    await asyncio.wait_for(ws.send_json(message), timeout=_SEND_TIMEOUT_SECONDS)


async def _flush() -> None:
    """Single sender: while reports keep arriving and someone is watching, push
    one snapshot per ``PUSH_MIN_INTERVAL_SECONDS``. The first report after a
    quiet second goes out at once; a burst collapses into the next slot. One
    task does all sends, so a socket never sees two concurrent ``send_json``."""
    global _dirty, _last_push_monotonic
    while _dirty and _debug_clients:
        wait = _last_push_monotonic + PUSH_MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _dirty = False
        clients = list(_debug_clients)
        if not clients:
            return
        try:
            message = {"type": "snapshot", **build_current_payload()}
            results = await asyncio.gather(
                *(_send_one(ws, message) for ws in clients),
                return_exceptions=True,
            )
            for ws, result in zip(clients, results, strict=True):
                if isinstance(result, Exception):
                    _drop_viewer(ws)
        except Exception as e:
            logger.debug(f"presence debug: push failed: {e}")
        _last_push_monotonic = time.monotonic()
