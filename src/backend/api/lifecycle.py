"""
Application lifecycle management for Renfield AI Assistant.

This module handles:
- Startup initialization (database, services)
- Background task management
- Graceful shutdown with device notification
"""

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from loguru import logger

from services.database import AsyncSessionLocal, init_db
from services.ollama_service import OllamaService
from services.task_queue import TaskQueue
from utils.config import settings

if TYPE_CHECKING:
    from fastapi import FastAPI

# Track background tasks for graceful shutdown
_startup_tasks: list[asyncio.Task] = []


async def _init_database():
    """Initialize database and run migrations."""
    await init_db()
    logger.info("✅ Datenbank initialisiert")


async def _init_auth():
    """Initialize authentication system with default roles and admin user."""
    try:
        from services.auth_service import ensure_admin_user, ensure_default_roles

        async with AsyncSessionLocal() as db_session:
            # Ensure default roles exist
            roles = await ensure_default_roles(db_session)
            logger.info(f"✅ Auth-Rollen initialisiert: {[r.name for r in roles]}")

            # Ensure default admin user exists (only if no users exist)
            admin = await ensure_admin_user(db_session)
            if admin:
                logger.warning(
                    f"⚠️  Standard-Admin erstellt: '{admin.username}' - "
                    f"BITTE PASSWORT SOFORT ÄNDERN!"
                )
    except Exception as e:
        logger.error(f"❌ Auth-Initialisierung fehlgeschlagen: {e}")
        import traceback
        logger.error(traceback.format_exc())


async def _init_ollama(app: "FastAPI") -> OllamaService:
    """Initialize Ollama service and ensure model is loaded."""
    ollama = OllamaService()
    await ollama.ensure_model_loaded()
    app.state.ollama = ollama
    logger.info("✅ Ollama Service bereit")
    return ollama


async def _init_task_queue(app: "FastAPI") -> TaskQueue:
    """Initialize the task queue."""
    task_queue = TaskQueue()
    app.state.task_queue = task_queue
    logger.info("✅ Task Queue bereit")
    return task_queue



def _schedule_whisper_preload():
    """Schedule Whisper model preloading in background."""
    try:
        from api.websocket import get_whisper_service

        async def preload_whisper():
            """Load Whisper model in background."""
            try:
                whisper_service = get_whisper_service()
                whisper_service.load_model()
                logger.info("✅ Whisper Service bereit (STT aktiviert)")
            except Exception as e:
                logger.warning(f"⚠️  Whisper konnte nicht vorgeladen werden: {e}")
                logger.warning("💡 Spracheingabe wird beim ersten Gebrauch geladen")

        task = asyncio.create_task(preload_whisper())
        _startup_tasks.append(task)
    except Exception as e:
        logger.warning(f"⚠️  Whisper-Preloading fehlgeschlagen: {e}")


def _schedule_notification_cleanup():
    """Schedule periodic cleanup of expired notifications."""
    if not settings.proactive_enabled:
        return

    async def cleanup_loop():
        """Cleanup expired notifications every hour."""
        while True:
            try:
                await asyncio.sleep(3600)  # 1 hour
                from services.notification_service import NotificationService
                async with AsyncSessionLocal() as db_session:
                    service = NotificationService(db_session)
                    await service.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"⚠️  Notification cleanup failed: {e}")

    task = asyncio.create_task(cleanup_loop())
    _startup_tasks.append(task)
    logger.info("✅ Notification Cleanup Scheduler gestartet (stündlich)")


def _schedule_reminder_checker():
    """Start the periodic reminder checker (Phase 3b)."""
    if not settings.proactive_reminders_enabled:
        return

    async def reminder_loop():
        """Check for due reminders periodically."""
        while True:
            try:
                await asyncio.sleep(settings.proactive_reminder_check_interval)
                from services.reminder_service import check_due_reminders

                await check_due_reminders()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"⚠️  Reminder check failed: {e}")

    task = asyncio.create_task(reminder_loop())
    _startup_tasks.append(task)
    logger.info(
        f"✅ Reminder Checker gestartet "
        f"(interval={settings.proactive_reminder_check_interval}s)"
    )


def _schedule_memory_cleanup():
    """Schedule periodic cleanup of expired/decayed memories."""
    if not settings.memory_enabled:
        return

    async def cleanup_loop():
        while True:
            try:
                await asyncio.sleep(settings.memory_cleanup_interval)
                from services.conversation_memory_service import ConversationMemoryService

                async with AsyncSessionLocal() as db_session:
                    service = ConversationMemoryService(db_session)
                    counts = await service.cleanup()
                    total = sum(counts.values())
                    if total > 0:
                        from utils.metrics import record_memory_cleanup

                        record_memory_cleanup(counts)

                # Episodic memory: cleanup + summarization
                if settings.memory_episodic_enabled:
                    from services.episodic_memory_service import EpisodicMemoryService
                    from sqlalchemy import select, func
                    from models.database import EpisodicMemory

                    async with AsyncSessionLocal() as db_session:
                        ep_svc = EpisodicMemoryService(db_session)
                        ep_counts = await ep_svc.cleanup()
                        ep_total = sum(ep_counts.values())
                        if ep_total > 0:
                            logger.info(f"Episodic cleanup: {ep_counts}")

                        # Summarize old episodes for users above threshold
                        result = await db_session.execute(
                            select(EpisodicMemory.user_id)
                            .where(EpisodicMemory.is_active == True)  # noqa: E712
                            .group_by(EpisodicMemory.user_id)
                            .having(func.count(EpisodicMemory.id) > settings.memory_episodic_summarize_threshold)
                        )
                        user_ids = [row[0] for row in result.fetchall() if row[0] is not None]
                        for uid in user_ids:
                            summarized = await ep_svc.summarize_old(uid)
                            if summarized > 0:
                                logger.info(f"Episodic summarization: {summarized} episodes for user {uid}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Memory cleanup failed: {e}")

    task = asyncio.create_task(cleanup_loop())
    _startup_tasks.append(task)
    logger.info(
        f"Memory Cleanup Scheduler gestartet "
        f"(interval={settings.memory_cleanup_interval}s)"
    )


def _schedule_upload_cleanup():
    """Schedule periodic cleanup of old non-indexed chat uploads."""
    if not settings.chat_upload_cleanup_enabled:
        return

    async def cleanup_loop():
        while True:
            try:
                await asyncio.sleep(3600)  # 1 hour
                from api.routes.chat_upload import _cleanup_uploads

                async with AsyncSessionLocal() as db_session:
                    deleted_count, deleted_files = await _cleanup_uploads(
                        db_session, settings.chat_upload_retention_days
                    )
                    if deleted_count > 0:
                        logger.info(
                            f"Upload cleanup: {deleted_count} uploads deleted "
                            f"({deleted_files} files, retention={settings.chat_upload_retention_days}d)"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Upload cleanup failed: {e}")

    task = asyncio.create_task(cleanup_loop())
    _startup_tasks.append(task)
    logger.info(
        f"Upload Cleanup Scheduler gestartet "
        f"(retention={settings.chat_upload_retention_days}d, stündlich)"
    )


def _schedule_notification_poller(app):
    """Start the MCP notification poller for servers with notifications enabled."""
    if not settings.notification_poller_enabled:
        return
    if not getattr(app.state, "mcp_manager", None):
        return

    async def poller_main():
        from services.notification_poller import NotificationPollerService

        poller = NotificationPollerService(app.state.mcp_manager)
        app.state.notification_poller = poller
        await poller.start()

    task = asyncio.create_task(poller_main())
    _startup_tasks.append(task)
    logger.info("Notification Poller scheduled")


async def _init_mcp(app: "FastAPI"):
    """Initialize MCP client connections to external tool servers."""
    if not settings.mcp_enabled:
        app.state.mcp_manager = None
        logger.info("MCP Client deaktiviert")
        return

    try:
        from services.intent_registry import intent_registry
        from services.mcp_client import MCPManager

        manager = MCPManager()
        manager.load_config(settings.mcp_config_path)
        await manager.connect_all()

        # Load DB-persisted tool overrides and re-filter servers
        async with AsyncSessionLocal() as db_session:
            await manager.load_tool_overrides(db_session)
        for server_name in manager._servers:
            manager._refilter_server(server_name)

        await manager.start_refresh_loop()
        app.state.mcp_manager = manager

        # Register MCP tools with IntentRegistry for visibility in admin UI
        mcp_tools = manager.get_all_tools()
        tool_dicts = [
            {
                "intent": tool.namespaced_name,
                "description": tool.description,
                "server": tool.server_name,
                "input_schema": tool.input_schema,
            }
            for tool in mcp_tools
        ]
        intent_registry.set_mcp_tools(tool_dicts)

        # Pass bilingual examples from YAML config to intent registry
        mcp_examples = manager.get_server_examples()
        intent_registry.set_mcp_examples(mcp_examples)

        # Pass prompt_tools filter from YAML config
        prompt_tools = manager.get_prompt_tools_config()
        intent_registry.set_mcp_prompt_tools(prompt_tools)

        logger.info(f"✅ MCP Client bereit: {len(mcp_tools)} Tools registriert")
    except Exception as e:
        logger.error(f"MCP Client konnte nicht initialisiert werden: {e}")
        import traceback
        logger.error(traceback.format_exc())
        app.state.mcp_manager = None


async def _init_agent_router(app: "FastAPI"):
    """Initialize the Agent Router with role definitions."""
    if not settings.agent_enabled:
        app.state.agent_router = None
        app.state.agent_roles_config = None
        logger.info("Agent Router deaktiviert (agent_enabled=false)")
        return

    try:
        from services.agent_router import AgentRouter, load_roles_config

        roles_config = load_roles_config(settings.agent_roles_path)
        if not roles_config:
            logger.warning(f"Agent roles config empty or not found: {settings.agent_roles_path}")
            app.state.agent_router = None
            app.state.agent_roles_config = None
            return

        mcp_manager = getattr(app.state, 'mcp_manager', None)
        router = AgentRouter(
            roles_config,
            mcp_manager=mcp_manager,
            classify_timeout=settings.agent_router_timeout,
        )
        app.state.agent_router = router
        app.state.agent_roles_config = roles_config
        logger.info(f"✅ Agent Router bereit: {len(router.roles)} Rollen")

        # Initialize Semantic Router for fast classification
        if settings.semantic_router_enabled if hasattr(settings, 'semantic_router_enabled') else True:
            try:
                from services.semantic_router import SemanticRouter
                sr = SemanticRouter(
                    threshold=getattr(settings, 'semantic_router_threshold', 0.75)
                )
                await sr.initialize(router.roles)
                router.set_semantic_router(sr)
            except Exception as e:
                logger.warning(f"SemanticRouter init failed (non-fatal): {e}")

        # Load entity patterns for context-aware routing
        try:
            from services.reference_resolver import compile_patterns, load_entity_patterns
            from utils.hooks import run_hooks

            base_patterns = load_entity_patterns()
            # Let plugins extend patterns
            hook_results = await run_hooks("load_entity_patterns")
            for plugin_patterns in (hook_results or []):
                if isinstance(plugin_patterns, dict):
                    for domain, cfg in plugin_patterns.items():
                        if domain in base_patterns:
                            existing = base_patterns[domain].get("patterns", [])
                            new = cfg.get("patterns", []) if isinstance(cfg, dict) else []
                            base_patterns[domain]["patterns"] = existing + new
                        else:
                            base_patterns[domain] = cfg
            compile_patterns(base_patterns)
        except Exception as e:
            logger.debug(f"Entity patterns not loaded (non-fatal): {e}")
    except Exception as e:
        logger.error(f"❌ Agent Router konnte nicht initialisiert werden: {e}")
        import traceback
        logger.error(traceback.format_exc())
        app.state.agent_router = None
        app.state.agent_roles_config = None


async def _cancel_startup_tasks():
    """Cancel any pending startup tasks."""
    for task in _startup_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# `_notify_devices_shutdown` moved to ha_glue/bootstrap.py; ha_glue's
# `shutdown` hook handler now broadcasts the server_shutdown message
# to connected devices.


async def _load_plugin_module():
    """Load the plugin module specified in settings.plugin_module.

    Format: "package.module:callable" — the callable receives no args
    and is expected to call register_hook() for the events it cares about.
    """
    spec = settings.plugin_module
    if not spec:
        return

    try:
        import importlib

        if ":" in spec:
            module_path, attr_name = spec.rsplit(":", 1)
        else:
            module_path, attr_name = spec, None

        mod = importlib.import_module(module_path)

        if attr_name:
            fn = getattr(mod, attr_name)
            result = fn()
            # Support async register functions
            if asyncio.iscoroutine(result):
                await result

        logger.info(f"Plugin module loaded: {spec}")
    except Exception:
        logger.opt(exception=True).error(f"Failed to load plugin module: {spec}")


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    """
    Application lifespan context manager.

    Handles startup and shutdown of all services:
    - Database initialization
    - Authentication system setup
    - Ollama LLM service
    - Task queue
    - Whisper STT (background)
    - Home Assistant keywords (background)
    - Zeroconf for satellite discovery
    """
    logger.info("🚀 Renfield startet...")

    # Block startup if auth is enabled but secret_key is still the default
    if settings.auth_enabled and settings.secret_key.get_secret_value() == "changeme-in-production-use-strong-random-key":
        logger.critical(
            "SECRET_KEY is still the default value! "
            "Set a strong random SECRET_KEY before enabling AUTH_ENABLED=true."
        )
        raise SystemExit(1)

    # Warn about insecure defaults when auth is enabled
    if settings.auth_enabled:
        if not settings.ws_auth_enabled:
            logger.warning(
                "⚠️  WS_AUTH_ENABLED=false — WebSocket connections are NOT authenticated. "
                "Set WS_AUTH_ENABLED=true in production."
            )
        if settings.cors_origins == "*":
            logger.warning(
                "⚠️  CORS_ORIGINS='*' — all origins allowed. "
                "Set CORS_ORIGINS to your frontend domain(s) in production."
            )

    # Stage 0: Bootstrap ha_glue. The ha_glue package itself is
    # side-effect-free (the legacy compat re-export in models/database.py
    # imports the package as part of attribute resolution, so any
    # registration via __init__.py would bypass the smart_home gate
    # below). Hook registration only happens when this explicit
    # `bootstrap.register()` call fires.
    #
    # Gated on the smart_home feature flag so `RENFIELD_EDITION=pro`
    # deployments don't activate HA behavior even though the package
    # ships in the same monorepo. Wrapped in a broad try/except so the
    # eventual X-idra/renfield platform-only deploy (no ha_glue
    # installed) AND any future broken handler degrades cleanly.
    #
    # This is the ONE structural platform -> ha_glue import line.
    # Phase 2/3 will move it to a PLUGIN_MODULE entry point and remove
    # it from this file.
    if settings.features["smart_home"]:
        try:
            from ha_glue.bootstrap import register as _ha_glue_register
            _ha_glue_register()
            logger.info("✅ ha_glue bootstrap loaded")
        except ImportError:
            logger.info("ha_glue not installed — running platform-only")
        except Exception:  # noqa: BLE001 — never break startup on plugin error
            logger.opt(exception=True).warning(
                "ha_glue bootstrap raised — HA fallback disabled, continuing startup"
            )

    # Stage 1: Sequential (auth depends on database)
    await _init_database()
    await _init_auth()

    # Stage 2: Independent services (parallel)
    await asyncio.gather(
        _init_ollama(app),
        _init_task_queue(app),
        _init_mcp(app),
    )

    # Stage 3: Depends on MCP
    await _init_agent_router(app)

    # Background preloading (platform-owned schedulers only — ha_glue's
    # HA keyword preloader and presence event cleanup scheduler are started
    # from ha_glue.bootstrap.ha_glue_on_startup via the `startup` hook).
    if settings.features["voice"]:
        _schedule_whisper_preload()
    _schedule_notification_cleanup()
    _schedule_reminder_checker()
    _schedule_notification_poller(app)
    _schedule_memory_cleanup()
    _schedule_upload_cleanup()

    # Presence / paperless audit / media follow / conversation handoff /
    # Zeroconf satellite discovery are bootstrapped by ha_glue via its
    # startup hook handler (fired below by `run_hooks("startup", ...)`).
    # Each subsystem gates itself on the relevant `ha_glue_settings.X`
    # flag internally. ha_glue also handles its own shutdown cleanup via
    # `shutdown` and `shutdown_finalize` hook handlers.

    # Knowledge Graph hooks
    if settings.knowledge_graph_enabled:
        from services.knowledge_graph_service import (
            kg_post_document_ingest_hook,
            kg_post_message_hook,
            kg_retrieve_context_hook,
        )
        from utils.hooks import register_hook

        register_hook("post_message", kg_post_message_hook)
        register_hook("retrieve_context", kg_retrieve_context_hook)
        register_hook("post_document_ingest", kg_post_document_ingest_hook)
        logger.info("✅ Knowledge Graph hooks registered")

    # Backend i18n
    from utils.i18n import load_translations
    load_translations()

    # MCP Response Compaction
    from services.mcp_compact import load_compact_config
    load_compact_config()

    # Context Variable Extraction
    from services.context_extractor import load_extraction_config
    load_extraction_config()

    # Plugin / Hook System
    await _load_plugin_module()
    from utils.hooks import run_hooks
    await run_hooks("startup", app=app)
    await run_hooks("register_routes", app=app)

    yield

    # Shutdown sequence
    logger.info("👋 Renfield wird heruntergefahren...")

    from utils.hooks import run_hooks
    await run_hooks("shutdown", app=app)

    await _cancel_startup_tasks()

    # Stop paperless audit before MCP shutdown
    if getattr(app.state, "paperless_audit", None):
        await app.state.paperless_audit.stop()

    # Stop notification poller before MCP shutdown
    if getattr(app.state, "notification_poller", None):
        await app.state.notification_poller.stop()

    # Device shutdown notification handled by ha_glue's shutdown hook
    # handler (see ha_glue/bootstrap.py::ha_glue_on_shutdown).

    # Shutdown MCP
    if getattr(app.state, "mcp_manager", None):
        await app.state.mcp_manager.shutdown()

    # Zeroconf is stopped by ha_glue's shutdown hook handler
    # (see ha_glue/bootstrap.py::ha_glue_on_shutdown).

    # Late-phase cleanup — fires AFTER everything platform owns has
    # shut down. Plugins register handlers here for resources that
    # were still in use during earlier teardown steps (e.g. HTTP
    # client singletons MCP was calling during its shutdown).
    await run_hooks("shutdown_finalize", app=app)

    logger.info("✅ Shutdown complete")
