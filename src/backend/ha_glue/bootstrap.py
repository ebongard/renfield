"""Explicit hook-registration entry point for ha_glue.

This module exists so that registering ha-glue hook handlers requires
an EXPLICIT call from platform startup code (currently
`api/lifecycle.py`). Importing the `ha_glue` package itself is
side-effect-free, which matters because the legacy compat re-export
in `models/database.py::__getattr__` does `from ha_glue.models import
database` to satisfy `from models.database import Room`. If hook
registration lived in `ha_glue/__init__.py` as an import side effect,
every platform service that touched an ha-glue model through the
compat shim would unconditionally register HA behavior — bypassing
the `settings.features["smart_home"]` gate that controls whether
RENFIELD_EDITION=pro deployments activate HA features.

The platform-side bootstrap is a single line:

    from ha_glue.bootstrap import register
    register()

wrapped in try/except so a missing or broken ha_glue package degrades
cleanly to "no HA fallback" without breaking Renfield startup.

## What `register()` wires up

- `intent_fallback_resolve` → `ha_glue.services.intent_fallback::ha_intent_fallback`
  (Day 4: fires when the platform intent classifier crashes on JSON)
- `startup` → `ha_glue_on_startup` (Day 5: async init for presence,
  paperless audit, HA keyword preload, media follow hooks, conversation
  handoff hook — everything that used to live inline in
  `api/lifecycle.py` behind `if ha_glue_settings.X` gates)
- `shutdown` → `ha_glue_on_shutdown` (Day 5: cancels background tasks
  owned by ha_glue — the presence event cleanup scheduler)

All ha_glue-owned asyncio tasks are tracked in `_ha_glue_tasks` below
so the shutdown handler can cancel them cleanly. Platform startup
tasks live in `api/lifecycle.py::_startup_tasks` and stay there.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger


# Asyncio tasks owned by ha_glue (background schedulers started during
# `ha_glue_on_startup`). The `shutdown` hook cancels each task so the
# pod can exit cleanly. Kept separate from platform's `_startup_tasks`
# list so ha_glue owns its own lifecycle.
_ha_glue_tasks: list[asyncio.Task] = []


def register() -> None:
    """Register all ha_glue hook handlers with the platform hook system.

    Idempotent at the system level — calling twice would register the
    same handlers twice, so the hook would fire twice with first-result
    semantics still picking the same answer. We trust the caller to
    only invoke this once during startup.

    Failures are logged but never propagate. A broken handler module
    must not break Renfield startup.
    """
    try:
        from utils.hooks import register_hook

        from ha_glue.services.intent_context import (
            ha_build_entity_context,
            ha_validate_classified_intent,
        )
        from ha_glue.services.intent_fallback import ha_intent_fallback
        from ha_glue.services.notification_privacy import (
            ha_should_play_tts_for_notification,
        )
        from ha_glue.services.sweep_handlers import (
            ha_chat_context_established,
            ha_resolve_user_current_room,
        )
        from ha_glue.services.chat_voice_handlers import (
            ha_fetch_tts_audio_cache,
            ha_resolve_room_context_by_ip,
            ha_route_chat_tts_to_device_output,
        )
        from ha_glue.services.device_handlers import (
            ha_deliver_notification,
            ha_get_connected_device_summary,
        )

        register_hook("intent_fallback_resolve", ha_intent_fallback)
        register_hook("build_entity_context", ha_build_entity_context)
        register_hook("validate_classified_intent", ha_validate_classified_intent)
        register_hook("chat_context_established", ha_chat_context_established)
        register_hook("should_play_tts_for_notification", ha_should_play_tts_for_notification)
        register_hook("resolve_user_current_room", ha_resolve_user_current_room)
        register_hook("route_chat_tts_to_device_output", ha_route_chat_tts_to_device_output)
        register_hook("resolve_room_context_by_ip", ha_resolve_room_context_by_ip)
        register_hook("fetch_tts_audio_cache", ha_fetch_tts_audio_cache)
        register_hook("get_connected_device_summary", ha_get_connected_device_summary)
        register_hook("deliver_notification", ha_deliver_notification)
        register_hook("register_tools", ha_glue_register_tools)
        register_hook("execute_tool", ha_glue_execute_tool)
        register_hook("startup", ha_glue_on_startup)
        register_hook("shutdown", ha_glue_on_shutdown)
        register_hook("shutdown_finalize", ha_glue_on_shutdown_finalize)
        register_hook("register_routes", ha_glue_register_routes)
        logger.info(
            "ha_glue.bootstrap: registered 17 handlers across 17 events"
        )
    except Exception:  # noqa: BLE001 — startup must never break on plugin error
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: hook registration failed — HA features disabled"
        )


# ---------------------------------------------------------------------------
# Startup handler — owns all async init that used to live inline in
# api/lifecycle.py behind `if ha_glue_settings.X:` gates.
# ---------------------------------------------------------------------------


async def ha_glue_on_startup(*, app: Any) -> None:
    """Bring up ha-glue subsystems that depend on the platform being ready.

    Called from `api/lifecycle.py::lifespan` via `run_hooks("startup", app=app)`,
    which fires after the platform has already initialized the database,
    auth, Ollama, task queue, MCP client, and agent router. At that point
    every resource ha_glue needs exists and can be opened.

    Each subsystem below is individually gated on its own
    `ha_glue_settings.X` flag, so partial configurations (e.g. presence
    but no media follow) work the same as they did when this logic lived
    inline in lifecycle.py.

    Exceptions here log and are swallowed — one broken subsystem must
    not prevent the others from initializing.
    """
    from ha_glue.utils.config import ha_glue_settings

    # --- Register presence intents with the platform IntentRegistry ---
    # The `PRESENCE_INTENTS` definition used to live inline in
    # `services/intent_registry.py` with an `is_enabled_func` lambda
    # reading `ha_glue_settings`. Moved to ha_glue and registered here
    # via the new `intent_registry.add_integration()` method. The
    # lambda still reads `ha_glue_settings.presence_enabled`, so the
    # intents only appear in the prompt when presence is actually on.
    try:
        from services.intent_registry import intent_registry

        from ha_glue.services.presence_intents import PRESENCE_INTENTS

        intent_registry.add_integration(PRESENCE_INTENTS)
        logger.info("✅ ha_glue: registered PRESENCE_INTENTS with intent_registry")
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: PRESENCE_INTENTS registration failed"
        )

    # --- Paperless audit ---
    try:
        await _init_paperless_audit(app)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: paperless audit init failed"
        )

    # --- HA keyword preload (background) ---
    try:
        _schedule_ha_keywords_preload()
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: HA keyword preload scheduling failed"
        )

    # --- Presence system (webhooks, analytics, BLE device registry, room cache) ---
    if ha_glue_settings.presence_enabled:
        try:
            await _init_presence(app)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                "ha_glue.bootstrap: presence init failed"
            )

    # --- Conversation handoff (requires presence for room-change detection) ---
    if ha_glue_settings.presence_enabled:
        try:
            from services.conversation_handoff import on_presence_enter_room
            from utils.hooks import register_hook

            register_hook("presence_enter_room", on_presence_enter_room)
            logger.info("✅ Conversation handoff hook registered")
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                "ha_glue.bootstrap: conversation handoff registration failed"
            )

    # --- Media Follow Me (requires both presence and media_follow enabled) ---
    if ha_glue_settings.media_follow_enabled and ha_glue_settings.presence_enabled:
        try:
            from ha_glue.services.media_follow_service import get_media_follow_service
            from utils.hooks import register_hook

            mf_service = get_media_follow_service()
            register_hook("presence_leave_room", mf_service.on_user_leave_room)
            register_hook("presence_enter_room", mf_service.on_user_enter_room)
            register_hook("presence_last_left", mf_service.on_last_left)
            logger.info("✅ Media Follow Me hooks registered")
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                "ha_glue.bootstrap: media follow registration failed"
            )

    # --- Zeroconf service discovery (for satellite auto-registration) ---
    try:
        await _init_zeroconf(app)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: Zeroconf init failed"
        )


async def _init_zeroconf(app: Any) -> None:
    """Start the Zeroconf mDNS service for satellite auto-discovery.

    Stores the service instance on `app.state.zeroconf_service` so the
    shutdown hook can stop it. Previously lived in platform
    `api/lifecycle.py::_init_zeroconf`; moved here because Zeroconf
    satellite discovery is a pure home-automation consumer feature.
    """
    from ha_glue.services.zeroconf_service import get_zeroconf_service
    zeroconf_service = get_zeroconf_service(port=8000)
    await zeroconf_service.start()
    app.state.zeroconf_service = zeroconf_service
    logger.info("✅ Zeroconf Service bereit")


async def _init_paperless_audit(app: Any) -> None:
    """Dynamically provision Paperless audit if MCP server is available.

    Routes, service, and imports only happen inside this function. If
    Paperless MCP is not configured, nothing is imported, no routes
    exist. Stores the running service on `app.state.paperless_audit`
    so the platform shutdown path can stop it.
    """
    from ha_glue.utils.config import ha_glue_settings

    if not ha_glue_settings.paperless_audit_enabled:
        return

    mcp_manager = getattr(app.state, "mcp_manager", None)
    if not mcp_manager or not mcp_manager.has_server("paperless"):
        logger.info("Paperless MCP not configured — audit disabled")
        return

    from ha_glue.api.routes.paperless_audit import router as audit_router
    from services.database import AsyncSessionLocal
    from ha_glue.services.paperless_audit_service import PaperlessAuditService

    app.include_router(audit_router)

    audit_service = PaperlessAuditService(
        mcp_manager=mcp_manager,
        db_factory=AsyncSessionLocal,
    )
    app.state.paperless_audit = audit_service
    await audit_service.start()
    logger.info("Paperless Audit: Routes mounted, service started")


def _schedule_ha_keywords_preload() -> None:
    """Schedule Home Assistant keywords preloading in background."""
    try:
        from ha_glue.integrations.homeassistant import HomeAssistantClient

        async def preload_keywords():
            try:
                ha_client = HomeAssistantClient()
                keywords = await ha_client.get_keywords()
                logger.info(
                    f"✅ Home Assistant Keywords vorgeladen: {len(keywords)} Keywords"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"⚠️  Keywords konnten nicht vorgeladen werden: {e}")

        task = asyncio.create_task(preload_keywords())
        _ha_glue_tasks.append(task)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"⚠️  Keyword-Preloading fehlgeschlagen: {e}")


async def _init_presence(app: Any) -> None:
    """Bring up the presence subsystem: webhooks, analytics, BLE service, cleanup."""
    from services.database import AsyncSessionLocal
    from ha_glue.services.presence_analytics import register_presence_analytics_hooks
    from ha_glue.services.presence_service import get_presence_service
    from ha_glue.services.presence_webhook import register_presence_webhooks

    register_presence_webhooks()
    register_presence_analytics_hooks()
    _schedule_presence_event_cleanup()

    presence_svc = get_presence_service()
    async with AsyncSessionLocal() as db_session:
        await presence_svc.load_device_registry(db_session)

    # Cache room names for presence display. `Room` imports through the
    # legacy compat shim — platform `models.database.__getattr__` forwards
    # to `ha_glue.models.database`. Same path the rest of ha_glue uses.
    from models.database import Room
    from sqlalchemy import select

    async with AsyncSessionLocal() as db_session:
        rooms = (await db_session.execute(select(Room))).scalars().all()
        for room in rooms:
            presence_svc.set_room_name(room.id, room.name)


def _schedule_presence_event_cleanup() -> None:
    """Schedule daily cleanup of old presence analytics events."""
    from ha_glue.utils.config import ha_glue_settings

    async def cleanup_loop():
        while True:
            try:
                await asyncio.sleep(86400)  # 24 hours
                from services.database import AsyncSessionLocal
                from ha_glue.services.presence_analytics import PresenceAnalyticsService

                async with AsyncSessionLocal() as db_session:
                    service = PresenceAnalyticsService(db_session)
                    await service.cleanup_old_events()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Presence event cleanup failed: {e}")

    task = asyncio.create_task(cleanup_loop())
    _ha_glue_tasks.append(task)
    logger.info(
        f"Presence Event Cleanup Scheduler gestartet "
        f"(retention={ha_glue_settings.presence_analytics_retention_days}d, täglich)"
    )


# ---------------------------------------------------------------------------
# Shutdown handler — cancel background tasks owned by ha_glue
# ---------------------------------------------------------------------------


async def ha_glue_on_shutdown(*, app: Any) -> None:
    """Cancel background tasks + stop Zeroconf.

    Fires EARLY in the shutdown sequence (before MCP teardown and
    device notification) so long-running loops and service advertisements
    exit gracefully before their dependencies go away.

    HTTP client singletons are NOT closed here — they live until the
    LATE phase (`ha_glue_on_shutdown_finalize`) because MCP may still
    invoke HA tools during its own teardown.

    Never raises. Platform startup is independently reliable.
    """
    # --- Cancel background tasks ---
    if _ha_glue_tasks:
        for task in _ha_glue_tasks:
            if not task.done():
                task.cancel()
        for task in _ha_glue_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        _ha_glue_tasks.clear()
        logger.info("ha_glue.bootstrap: background tasks cancelled")

    # --- Stop Zeroconf service advertisement ---
    zeroconf_svc = getattr(app.state, "zeroconf_service", None)
    if zeroconf_svc is not None:
        try:
            await zeroconf_svc.stop()
            logger.info("ha_glue.bootstrap: Zeroconf service stopped")
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                "ha_glue.bootstrap: Zeroconf stop failed"
            )

    # --- Notify connected devices about server shutdown ---
    # Previously in platform `api/lifecycle.py::_notify_devices_shutdown`.
    # Broadcasts a server_shutdown message to all active WebSocket
    # connections in the DeviceManager registry and closes them.
    try:
        from ha_glue.services.device_manager import get_device_manager  # moves in Phase B.3
        dm = get_device_manager()
        shutdown_msg = {"type": "server_shutdown", "message": "Server is shutting down"}
        for device in list(dm.devices.values()):
            try:
                await device.websocket.send_json(shutdown_msg)
                await device.websocket.close(code=1001, reason="Server shutdown")
            except Exception:  # noqa: BLE001
                pass
        logger.info(f"👋 ha_glue: notified {len(dm.devices)} devices about shutdown")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"⚠️  ha_glue device shutdown broadcast failed: {e}")


async def ha_glue_on_shutdown_finalize(*, app: Any) -> None:
    """Close HTTP client singletons AFTER everything else has shut down.

    Fires in the LATE shutdown phase (`shutdown_finalize` hook), which
    runs after MCP teardown and zeroconf stop. MCP may still invoke HA
    tool calls during its shutdown (cleanup notifications, final state
    sync), so the HA/Frigate HTTP clients must stay alive until then.

    Each singleton closer is guarded so a broken one doesn't block the
    others. Never raises — late-shutdown must not block pod exit.
    """
    try:
        from ha_glue.integrations.homeassistant import close_ha_client
        await close_ha_client()
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: close_ha_client failed"
        )
    try:
        from ha_glue.integrations.frigate import close_frigate_client
        await close_frigate_client()
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: close_frigate_client failed"
        )


# ---------------------------------------------------------------------------
# register_routes handler — mount ha_glue-owned FastAPI routers
# ---------------------------------------------------------------------------


async def ha_glue_register_tools(*, registry: Any, **_: Any) -> None:
    """Register ha_glue internal tools with the platform agent tool registry.

    Fires on `register_tools`. Iterates `InternalToolService.TOOLS` and
    adds each tool definition to the registry, respecting the registry's
    `internal_filter` attribute (same semantics as the platform's built-in
    internal tool registration used to have).

    Platform-only deploys (no ha_glue loaded) never hit this handler and
    the agent loop never sees these tools — which is correct. The
    platform's own `knowledge_tool.py` registers `internal.knowledge_search`
    unconditionally; everything else in `internal.*` is owned by ha_glue.
    """
    from ha_glue.services.internal_tools import InternalToolService
    from services.agent_tools import ToolDefinition

    internal_filter = getattr(registry, "internal_filter", None)

    added = 0
    for name, definition in InternalToolService.TOOLS.items():
        if internal_filter is not None and name not in internal_filter:
            continue
        params = {
            param_name: param_desc
            for param_name, param_desc in definition.get("parameters", {}).items()
        }
        tool = ToolDefinition(
            name=name,
            description=definition["description"],
            parameters=params,
        )
        registry._tools[tool.name] = tool
        added += 1
    logger.debug(f"ha_glue: registered {added} internal.* tools with agent registry")


async def ha_glue_execute_tool(
    *,
    intent: str,
    parameters: dict,
    **_: Any,
) -> dict | None:
    """Dispatch `internal.*` intents to the ha_glue InternalToolService.

    Fires on `execute_tool`. Returns a result dict if the intent is
    ha_glue-owned, or None to let other handlers / the default dispatch
    deal with it.

    The platform's `internal.knowledge_search` is NOT handled here — it
    is dispatched directly in `action_executor.py` before the hook runs,
    so platform-only deploys don't need ha_glue to answer RAG queries.
    """
    if not intent.startswith("internal."):
        return None
    if intent == "internal.knowledge_search":
        # Platform owns this tool — let the direct dispatch handle it.
        return None

    from ha_glue.services.internal_tools import InternalToolService

    service = InternalToolService()
    return await service.execute(intent, parameters)


async def ha_glue_register_routes(*, app: Any) -> None:
    """Mount ha_glue-owned FastAPI routers on the platform app.

    After Phase 1 W2 Phase C, all HA-flavored REST endpoints (camera,
    homeassistant, paperless audit, presence, rooms, satellites,
    /admin/refresh-keywords) and WebSockets (/ws/device, /ws/satellite)
    live under ha_glue and are mounted here. Pro deploys (no ha_glue
    loaded) never call this handler, so all HA endpoints cleanly return
    404 — which is correct behavior.

    Each router is individually guarded so a broken module doesn't
    block the others. Feature-flag gating (smart_home / cameras /
    satellites) matches the previous platform-side gates in main.py
    so individual subsystems can still be toggled via the platform
    Settings `features` dict.
    """
    from utils.config import settings as _platform_settings

    # --- /admin/refresh-keywords ---
    try:
        from ha_glue.api.admin import router as admin_router
        app.include_router(admin_router)
        logger.info("✅ ha_glue: mounted /admin/refresh-keywords")
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: admin router mount failed"
        )

    # --- Camera REST router (Frigate NVR) ---
    if _platform_settings.features.get("cameras"):
        try:
            from ha_glue.api.routes.camera import router as camera_router
            app.include_router(camera_router, prefix="/api/camera", tags=["Camera"])
            logger.info("✅ ha_glue: mounted /api/camera")
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                "ha_glue.bootstrap: camera router mount failed"
            )

    # --- Home Assistant REST router ---
    if _platform_settings.features.get("smart_home"):
        try:
            from ha_glue.api.routes.homeassistant import router as ha_router
            app.include_router(
                ha_router, prefix="/api/homeassistant", tags=["Home Assistant"]
            )
            logger.info("✅ ha_glue: mounted /api/homeassistant")
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                "ha_glue.bootstrap: homeassistant router mount failed"
            )

    # --- Satellites REST router (registration, status, OTA) ---
    if _platform_settings.features.get("satellites"):
        try:
            from ha_glue.api.routes.satellites import router as satellites_router
            app.include_router(
                satellites_router, prefix="/api/satellites", tags=["Satellites"]
            )
            logger.info("✅ ha_glue: mounted /api/satellites")
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                "ha_glue.bootstrap: satellites router mount failed"
            )

    # --- Rooms REST router ---
    try:
        from ha_glue.api.routes.rooms import router as rooms_router
        app.include_router(rooms_router, prefix="/api/rooms", tags=["Rooms"])
        logger.info("✅ ha_glue: mounted /api/rooms")
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: rooms router mount failed"
        )

    # --- Presence REST router (BLE + user location queries) ---
    try:
        from ha_glue.api.routes.presence import router as presence_router
        app.include_router(presence_router, tags=["Presence"])
        logger.info("✅ ha_glue: mounted presence router")
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: presence router mount failed"
        )

    # --- Paperless audit REST router ---
    try:
        from ha_glue.api.routes.paperless_audit import router as paperless_router
        app.include_router(paperless_router)
        logger.info("✅ ha_glue: mounted paperless_audit router")
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: paperless_audit router mount failed"
        )

    # --- Device WebSocket (/ws/device) — satellites + Renfield web panels ---
    try:
        from ha_glue.api.websocket.device_handler import router as device_router
        app.include_router(device_router, tags=["WebSocket Device"])
        logger.info("✅ ha_glue: mounted /ws/device")
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: device_router mount failed"
        )

    # --- Satellite WebSocket (/ws/satellite) — Pi Zero audio stream ---
    if _platform_settings.features.get("satellites"):
        try:
            from ha_glue.api.websocket.satellite_handler import router as satellite_router
            app.include_router(satellite_router, tags=["WebSocket Satellite"])
            logger.info("✅ ha_glue: mounted /ws/satellite")
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning(
                "ha_glue.bootstrap: satellite_router mount failed"
            )
