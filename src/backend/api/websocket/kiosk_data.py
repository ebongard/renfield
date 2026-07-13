"""
Kiosk data helpers — the read-only, content-free reads that back the `/ws/kiosk`
snapshot + push deltas (`kiosk_handler`).

This is kiosk-OWNED code: it moved here wholesale from the now-decommissioned
`api/routes/command_center.py` when the admin Command Center was removed and the
kiosk became the surviving wall-display surface (the kiosk sources everything
over the WS hub — no REST poll). It holds two pieces of shared logic:

  * recent_role_activity_entries — newest-first role activations for the pulse
    trail, content-free by construction (role + timestamp + ok only).
  * compute_kiosk_weather / refresh_and_push_kiosk_weather — the process-cached
    home-location weather reading + the backend-internal refresher that PUSHES a
    ``weather_updated`` delta on change (NOT a client poll — the timer refreshes
    an external cache Open-Meteo doesn't push).

Both degrade to an empty/None payload (never an error) when the feature is off or
the source is unavailable, so the kiosk simply hides the tile.
"""

import time
from datetime import datetime

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Message
from utils.config import settings


class RoleActivityEntry(BaseModel):
    role: str
    at: datetime
    ok: bool | None


# How many recent assistant messages to scan for role entries. Shortcut paths
# persist agent_role=None, so the window is larger than the returned limit.
_ACTIVITY_SCAN_WINDOW = 400


async def recent_role_activity_entries(
    db: AsyncSession, limit: int = 30
) -> list[RoleActivityEntry]:
    """Newest-first role activations, content-free by construction: only the
    role name, timestamp, and the turn's action_success. Feeds the kiosk WS
    snapshot (``kiosk_handler``).

    The agent_role extraction happens in Python over a bounded recent window
    (JSON, not JSONB, column — portable across the test sqlite shim and prod
    Postgres without dialect-specific JSON operators).
    """
    # Order by the PK, not the unindexed timestamp column: id order is insert
    # order (≈ chronological), and the PK index turns the scan into a bounded
    # backward index scan instead of a full-table sort.
    result = await db.execute(
        select(Message.timestamp, Message.message_metadata)
        .where(Message.role == "assistant")
        .order_by(Message.id.desc())
        .limit(_ACTIVITY_SCAN_WINDOW)
    )
    entries: list[RoleActivityEntry] = []
    for timestamp, metadata in result.all():
        if not isinstance(metadata, dict):
            continue
        role = metadata.get("agent_role")
        if not role or not isinstance(role, str):
            continue
        ok = metadata.get("action_success")
        entries.append(
            RoleActivityEntry(
                role=role,
                at=timestamp,
                ok=ok if isinstance(ok, bool) else None,
            )
        )
        if len(entries) >= limit:
            break
    return entries


# ---------------------------------------------------------------------------
# Active-subsystem pulse — which subsystem(s) a completed turn touched. Shared by
# BOTH the web-chat path (chat_handler) AND the voice path (satellite_handler) so
# a spoken "turn off the light" lights the same kiosk node a typed one does. It
# lived in chat_handler until the voice path was found to never pulse (the household
# talks to satellites, not the web chat) — moved here so both can emit it.
# ---------------------------------------------------------------------------

# Maps the platform-core / ha_glue ``internal.*`` tools (which have no MCP server,
# hence no natural ring node) onto a kiosk subsystem id. Unknown internal tools
# are intentionally skipped (no pulse). ``homeassistant`` and ``weather`` are REAL
# MCP servers (they already render as tool-ring nodes); ``knowledge`` / ``presence``
# / ``media`` are INTERNAL-ONLY subsystems with no MCP server — the kiosk renders
# synthetic pulse-only nodes for exactly those three, so this map's internal-only
# value set MUST stay in sync with the frontend ``INTERNAL_SUBSYSTEM_NODES``
# (components/kiosk/useKioskModel.ts). Pure Gen-UI formatting tools (render_table /
# render_list) touch no subsystem → omitted.
INTERNAL_SUBSYSTEM_LABELS: dict[str, str] = {
    # knowledge / second brain — RAG, memory, document ingest + maintenance
    "internal.knowledge_search": "knowledge",
    "internal.list_my_memories": "knowledge",
    "internal.forward_attachment_to_paperless": "knowledge",
    "internal.paperless_commit_upload": "knowledge",
    "internal.ingest_file": "knowledge",
    "internal.ingest_status": "knowledge",
    "internal.reindex_documents": "knowledge",
    "internal.list_chunkless_documents": "knowledge",
    # presence
    "internal.presence_map": "presence",
    "internal.presence_history": "presence",
    "internal.get_all_presence": "presence",
    "internal.get_user_location": "presence",
    "internal.bluetooth_scan": "presence",
    # home assistant — device control + spoken announcements via HA speakers
    "internal.device_action": "homeassistant",
    "internal.device_controls": "homeassistant",
    "internal.announce_in_room": "homeassistant",
    "internal.broadcast_announcement": "homeassistant",
    # weather (wraps the weather MCP)
    "internal.weather_widget": "weather",
    # media — DLNA / radio / server playback orchestration
    "internal.media_control": "media",
    "internal.play_radio": "media",
    "internal.play_in_room": "media",
    "internal.play_from_server": "media",
    "internal.play_album_on_dlna": "media",
    "internal.play_video_on_dlna": "media",
    "internal.list_radio_favorites": "media",
    "internal.save_radio_favorite": "media",
    "internal.remove_radio_favorite": "media",
    "internal.resolve_room_player": "media",
}

# A single turn rarely touches many subsystems; cap the pushed/persisted list so
# an orchestrated fan-out can't bloat the event or the row.
_MAX_SUBSYSTEMS_PER_TURN = 5


def extract_subsystems_used(tool_results: list) -> list[str]:
    """Derive the content-free subsystem ids a turn touched, for the kiosk pulse.

    Each entry is a ``(tool_name, data)`` pair (only ``tool_name`` is read).
    ``mcp.<server>.<tool>`` → ``<server>``; ``internal.<tool>`` → the static
    ``INTERNAL_SUBSYSTEM_LABELS`` allowlist (unknown internal tools skipped).
    Deduped, order-preserved, capped at ``_MAX_SUBSYSTEMS_PER_TURN``. Empty when a
    turn ran no tool (direct-LLM / ``general.conversation`` / shortcut paths).
    """
    subsystems: list[str] = []
    seen: set[str] = set()
    for entry in tool_results:
        tool_name = entry[0] if isinstance(entry, (tuple, list)) and entry else None
        if not isinstance(tool_name, str) or not tool_name:
            continue
        if tool_name.startswith("mcp."):
            parts = tool_name.split(".")
            sub = parts[1] if len(parts) >= 3 and parts[1] else None
        elif tool_name.startswith("internal."):
            sub = INTERNAL_SUBSYSTEM_LABELS.get(tool_name)
        else:
            sub = None
        if not sub or sub in seen:
            continue
        seen.add(sub)
        subsystems.append(sub)
        if len(subsystems) >= _MAX_SUBSYSTEMS_PER_TURN:
            break
    return subsystems


async def broadcast_turn_activity(
    role: str | None, subsystems: list[str], ok: bool | None
) -> None:
    """Push ONE content-free ``turn_activity`` pulse to the kiosk hub (role +
    which subsystems this turn touched). No-op when there's nothing to show (no
    role AND no subsystems), so a plain conversation turn pushes nothing.
    Fire-and-forget: a hub failure must never break the turn."""
    if not role and not subsystems:
        return
    try:
        from datetime import UTC, datetime

        from api.websocket.kiosk_handler import broadcast_kiosk_event

        await broadcast_kiosk_event(
            {
                "type": "turn_activity",
                "role": role,
                "subsystems": subsystems,
                "ok": ok,
                "at": datetime.now(UTC).isoformat(),
            }
        )
    except Exception as e:  # noqa: BLE001 — never break a turn on a kiosk push
        logger.debug(f"kiosk turn_activity broadcast failed: {e}")


# ---------------------------------------------------------------------------
# Internal-subsystem health (knowledge / presence / media). The kiosk draws three
# synthetic pseudo-nodes for the platform-core `internal.*` subsystems that have
# no MCP server (INTERNAL_SUBSYSTEM_NODES, useKioskModel.ts). They were permanent
# gray "unknown" placeholders — pulse-only, no status — which on a wall board read
# as broken. This gives each a REAL healthy/degraded/down verdict from live
# backend state so an impaired subsystem is visible instead of a silent gray
# diamond. Each verdict carries an optional machine `impaired_code` the frontend
# localizes (i18n rule), mirroring the MCP-server `impaired_code` contract.
#
# Grounded signals only — no invented metrics:
#   * presence  — presence_enabled + the satellite fleet's ability to deliver BLE
#     reports. An enrolled-but-unauthenticated satellite receives no IRK push and
#     silently reports nothing (the exact failure that mislocated a phone on
#     2026-07-09), so that reads as degraded.
#   * knowledge — the ingest worker liveness + Redis queue depth (the same probe
#     `internal.ingest_status` surfaces).
#   * media     — media-follow enabled/disabled. No cheap output-target
#     reachability probe exists, so media is availability-only (healthy/down); a
#     "no reachable target" degraded signal is a follow-up, deliberately NOT faked.
# ---------------------------------------------------------------------------

# The subsystem ids the kiosk draws as internal pseudo-nodes. MUST stay in sync
# with the frontend INTERNAL_SUBSYSTEM_NODES (components/kiosk/useKioskModel.ts).
INTERNAL_SUBSYSTEM_IDS = ("knowledge", "presence", "media")

# A knowledge ingest backlog deeper than this reads as degraded (the worker is up
# but not keeping pace). Named, not magic — mirrors the other module thresholds.
_KNOWLEDGE_QUEUE_DEGRADED_ABOVE = 100


class InternalSubsystemHealth(BaseModel):
    id: str
    health: str  # "healthy" | "degraded" | "down"
    impaired_code: str | None = None


async def _presence_health() -> "InternalSubsystemHealth":
    from ha_glue.utils.config import ha_glue_settings

    if not ha_glue_settings.presence_enabled:
        # 'off' (disabled by config), NOT 'down' — a switched-off subsystem must
        # not paint the same red/"failed" as a real outage on the wall board.
        return InternalSubsystemHealth(
            id="presence", health="off", impaired_code="presence_disabled"
        )
    try:
        from ha_glue.services.satellite_manager import get_satellite_manager

        sats = list(get_satellite_manager().satellites.values())
    except Exception:
        sats = []
    if not sats:
        return InternalSubsystemHealth(
            id="presence", health="degraded", impaired_code="presence_no_satellite"
        )
    # When per-satellite enrollment is on, a connected-but-unauthenticated
    # satellite gets no IRK push, so it can't resolve rotating-RPA phones and
    # contributes nothing to room arbitration — silently. Surface it as degraded.
    if settings.satellite_enrollment_enabled and any(
        not getattr(s, "authenticated", False) for s in sats
    ):
        return InternalSubsystemHealth(
            id="presence",
            health="degraded",
            impaired_code="presence_satellite_unauthenticated",
        )
    return InternalSubsystemHealth(id="presence", health="healthy")


async def _knowledge_health() -> "InternalSubsystemHealth":
    try:
        # Shared probe with `internal.ingest_status` (single source of truth) —
        # `backlog` is the LIVE pending count (XPENDING), which drains, not the
        # ever-growing cumulative stream length.
        from services.kb_maintenance_tool import ingest_worker_and_backlog

        worker_alive, backlog = await ingest_worker_and_backlog()
    except Exception as e:  # noqa: BLE001 — a failed probe IS a degraded signal
        logger.debug(f"kiosk internal health: knowledge probe failed: {e}")
        return InternalSubsystemHealth(
            id="knowledge", health="degraded", impaired_code="knowledge_worker_down"
        )
    if worker_alive is False:
        return InternalSubsystemHealth(
            id="knowledge", health="degraded", impaired_code="knowledge_worker_down"
        )
    if isinstance(backlog, int) and backlog > _KNOWLEDGE_QUEUE_DEGRADED_ABOVE:
        return InternalSubsystemHealth(
            id="knowledge", health="degraded", impaired_code="knowledge_queue_backed_up"
        )
    return InternalSubsystemHealth(id="knowledge", health="healthy")


async def _media_health() -> "InternalSubsystemHealth":
    from ha_glue.utils.config import ha_glue_settings

    if not ha_glue_settings.media_follow_enabled:
        # 'off' (disabled by config), not 'down' — see _presence_health.
        return InternalSubsystemHealth(
            id="media", health="off", impaired_code="media_disabled"
        )
    return InternalSubsystemHealth(id="media", health="healthy")


async def compute_internal_subsystem_health() -> list[dict]:
    """Health verdicts for the kiosk's three internal pseudo-nodes.

    Best-effort per subsystem: a probe that raises degrades THAT subsystem's
    readout rather than aborting the whole list. Content-free (ids + verdicts)."""
    out: list[dict] = []
    for compute in (_presence_health, _knowledge_health, _media_health):
        try:
            out.append((await compute()).model_dump())
        except Exception as e:  # noqa: BLE001 — never abort the whole readout
            logger.debug(f"kiosk internal health: {compute.__name__} failed: {e}")
    return out


# How often the backend recomputes internal-subsystem health for the push
# refresher. Fast enough that a wall board reflects an enrollment/worker fault
# within a glance, cheap enough to run continuously (in-memory + one Redis probe).
_INTERNAL_HEALTH_REFRESH_SECONDS = 30

# Last internal-health list PUSHED to the kiosk hub, so the refresher only
# broadcasts on an actual change (diff-gate). None until the first push.
_internal_health_last_pushed: list[dict] | None = None


def reset_internal_health_gate() -> None:
    """Force the next refresher tick to re-push the current verdicts.

    The diff-gate is a module global that is NOT advanced while no kiosk is
    connected (the scheduler skips the refresh then). So across a no-client gap
    it can hold a pre-gap verdict; a kiosk that connects during the gap hydrates
    from the fresh snapshot, but a later reversion to the stale gate value would
    be suppressed and leave that kiosk stuck. Resetting on connect makes the next
    tick re-emit the truth, reconciling every listener."""
    global _internal_health_last_pushed
    _internal_health_last_pushed = None


async def refresh_and_push_internal_health() -> None:
    """Backend-internal internal-subsystem-health refresh → PUSH on change.

    The backing state (enrollment/auth, ingest worker liveness, Redis backlog)
    has no push of its own, so a timer recomputes and streams an
    ``internal_health_changed`` delta only when a verdict changes — the same
    diff-gated, fire-and-forget pattern as the weather tile."""
    global _internal_health_last_pushed
    health = await compute_internal_subsystem_health()
    if health == _internal_health_last_pushed:
        return
    try:
        from api.websocket.kiosk_handler import broadcast_kiosk_event

        await broadcast_kiosk_event(
            {"type": "internal_health_changed", "subsystems": health}
        )
    except Exception as e:
        # Do NOT advance the gate on a failed broadcast — otherwise the delta is
        # lost permanently (the next tick would see no change and stay silent).
        logger.debug(f"kiosk internal_health_changed broadcast failed: {e}")
        return
    _internal_health_last_pushed = health


# ---------------------------------------------------------------------------
# Federation-peer reachability. The peer set has no push of its own (last_seen_at
# is written only when a remote peer queries us), so a backend timer recomputes
# reachability and streams a ``peer_status_changed`` delta on change — closing
# the deferred kiosk peer-liveness gap so the wall board's peer nodes go
# green/red live instead of only on kiosk reconnect + a frontend wall-clock decay.
# ---------------------------------------------------------------------------

# A peer is "reachable" if it queried THIS instance within this many seconds.
_PEER_REACHABLE_WITHIN_SECONDS = 300

# How often the backend recomputes peer reachability for the push refresher. The
# reachability window is 5 min; a 60s tick surfaces a peer going quiet within a
# glance without hammering the DB.
_PEER_STATUS_REFRESH_SECONDS = 60

# Last peer list PUSHED to the kiosk hub (diff-gate). None until the first push.
_peer_status_last_pushed: list[dict] | None = None


def reset_peer_status_gate() -> None:
    """Force the next refresher tick to re-push peer status (same no-client-gap
    rationale as reset_internal_health_gate)."""
    global _peer_status_last_pushed
    _peer_status_last_pushed = None


async def compute_peer_status() -> list[dict]:
    """Content-free federation-peer reachability for the kiosk.

    One node per remote identity (dedup by pubkey); ``reachable`` = the peer
    queried THIS instance within :data:`_PEER_REACHABLE_WITHIN_SECONDS`. No
    pubkey, no message content. Shared by the snapshot builder and the refresher.
    """
    from datetime import UTC

    from models.database import PeerUser
    from services.database import AsyncSessionLocal

    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(select(PeerUser).where(PeerUser.revoked_at.is_(None)))
        ).scalars().all()
    peers: list[dict] = []
    seen: set = set()
    for peer in rows:
        # Different circle owners can pair with the same remote node; the kiosk
        # shows one node per remote identity.
        if peer.remote_pubkey in seen:
            continue
        seen.add(peer.remote_pubkey)
        last_seen = peer.last_seen_at
        reachable = False
        if last_seen is not None:
            ls = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=UTC)
            reachable = (now - ls).total_seconds() < _PEER_REACHABLE_WITHIN_SECONDS
        peers.append(
            {
                "id": peer.id,
                "name": peer.remote_display_name,
                "last_seen_at": last_seen.isoformat() if last_seen else None,
                "reachable": reachable,
            }
        )
    return peers


async def refresh_and_push_peer_status() -> None:
    """Recompute peer reachability → PUSH a ``peer_status_changed`` delta on
    change. Diff-gated, fire-and-forget — same pattern as the internal-health and
    weather refreshers."""
    global _peer_status_last_pushed
    peers = await compute_peer_status()
    if peers == _peer_status_last_pushed:
        return
    try:
        from api.websocket.kiosk_handler import broadcast_kiosk_event

        await broadcast_kiosk_event({"type": "peer_status_changed", "peers": peers})
    except Exception as e:
        # Do NOT advance the gate on a failed broadcast (see internal-health).
        logger.debug(f"kiosk peer_status_changed broadcast failed: {e}")
        return
    _peer_status_last_pushed = peers


# ---------------------------------------------------------------------------
# Ambient kiosk weather tile. Read-only, degrades to None (never an error) when
# the feature is off or the source is unavailable, so the kiosk hides the tile.
# ---------------------------------------------------------------------------


class KioskWeather(BaseModel):
    location: str
    temp: float
    unit: str
    code: int
    condition: str
    high: float | None = None
    low: float | None = None


# Weather barely moves; serve a process-local cached reading so the refresher
# never hammers the Open-Meteo MCP.
_WEATHER_TTL_SECONDS = 600
_weather_cache: dict[str, object] = {"at": 0.0, "value": None}


async def compute_kiosk_weather(mcp_manager, force: bool = False) -> "KioskWeather | None":
    """Current conditions for the configured home location (process-cached).

    Feeds the kiosk WS snapshot + ``weather_updated`` delta. ``None`` (never an
    error) when weather is disabled, no location is configured, or the MCP can't
    answer — the tile hides itself.

    ``force=True`` bypasses the TTL cache READ (used by the periodic refresher so
    a tick genuinely re-fetches even if a client snapshot just warmed the cache
    mid-cycle); it still writes the cache on success.
    """
    location = (settings.kiosk_weather_location or "").strip()
    if not settings.weather_enabled or not location:
        return None

    now = time.monotonic()
    if (
        not force
        and _weather_cache["value"] is not None
        and now - float(_weather_cache["at"]) < _WEATHER_TTL_SECONDS
    ):
        return _weather_cache["value"]

    if mcp_manager is None:
        return _weather_cache["value"]  # last good reading, or None

    try:
        from services.widget_tools import _extract_mcp_payload

        res = await mcp_manager.execute_tool(
            "mcp.weather.get_weather",
            {"location": location, "days": 1, "temperature_unit": "celsius"},
        )
        if isinstance(res, dict) and not res.get("success", True):
            return _weather_cache["value"]
        raw = _extract_mcp_payload(res) if isinstance(res, dict) else {}
        cur = raw.get("current") if isinstance(raw, dict) else None
        if not isinstance(cur, dict) or cur.get("temperature") is None:
            return _weather_cache["value"]
        daily = raw.get("daily") if isinstance(raw.get("daily"), list) else []
        today = daily[0] if daily and isinstance(daily[0], dict) else {}
        loc = raw.get("location") if isinstance(raw.get("location"), dict) else {}
        weather = KioskWeather(
            location=loc.get("name") or location,
            temp=float(cur["temperature"]),
            unit="°C",
            code=int(cur.get("weather_code", 0)),
            condition=cur.get("weather_description", ""),
            high=today.get("temp_max"),
            low=today.get("temp_min"),
        )
    except Exception as e:  # noqa: BLE001 — a flaky MCP must never break the tile
        logger.warning(f"kiosk_weather: {e}")
        return _weather_cache["value"]

    _weather_cache["at"] = now
    _weather_cache["value"] = weather
    return weather


# Last weather value PUSHED to the kiosk hub, so the periodic refresher only
# broadcasts on an actual change (diff-gate). None until the first push.
_weather_last_pushed: dict | None = None


async def refresh_and_push_kiosk_weather(mcp_manager) -> None:
    """Backend-internal weather refresh → PUSH to the kiosk hub on change.

    NOT a client poll (plan §1.6): the timer refreshes an EXTERNAL cache
    (Open-Meteo has no push of its own), and the moment the reading changes it
    streams a ``weather_updated`` delta to the connected wall displays instead of
    waiting for the next connect/snapshot. Runs at ``_WEATHER_TTL_SECONDS`` so
    each tick actually re-fetches. Diff-gated + fire-and-forget.
    """
    global _weather_last_pushed
    weather = await compute_kiosk_weather(mcp_manager, force=True)
    payload = weather.model_dump() if weather is not None else None
    if payload == _weather_last_pushed:
        return
    _weather_last_pushed = payload
    try:
        from api.websocket.kiosk_handler import broadcast_kiosk_event

        await broadcast_kiosk_event({"type": "weather_updated", "weather": payload})
    except Exception as e:
        logger.debug(f"kiosk weather_updated broadcast failed: {e}")
