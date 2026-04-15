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

        from ha_glue.services.intent_fallback import ha_intent_fallback

        register_hook("intent_fallback_resolve", ha_intent_fallback)
        register_hook("startup", ha_glue_on_startup)
        register_hook("shutdown", ha_glue_on_shutdown)
        logger.info(
            "ha_glue.bootstrap: registered intent_fallback_resolve + startup + shutdown handlers"
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
            from services.media_follow_service import get_media_follow_service
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

    from api.routes.paperless_audit import router as audit_router
    from services.database import AsyncSessionLocal
    from services.paperless_audit_service import PaperlessAuditService

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
        from integrations.homeassistant import HomeAssistantClient

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
    from services.presence_analytics import register_presence_analytics_hooks
    from services.presence_service import get_presence_service
    from services.presence_webhook import register_presence_webhooks

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
                from services.presence_analytics import PresenceAnalyticsService

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
    """Cancel every background task started during ha_glue_on_startup.

    Platform has its own `_cancel_startup_tasks` for tasks it owns; this
    handler is only responsible for ha_glue's own tasks so the lifecycle
    cleanly separates ownership.
    """
    if not _ha_glue_tasks:
        return
    for task in _ha_glue_tasks:
        if not task.done():
            task.cancel()
    # Wait briefly for cancellations to propagate. Never raise.
    for task in _ha_glue_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _ha_glue_tasks.clear()
    logger.info("ha_glue.bootstrap: background tasks cancelled")
