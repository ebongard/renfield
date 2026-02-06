"""
Application lifecycle management for Renfield AI Assistant.

This module handles:
- Startup initialization (database, services, plugins)
- Background task management
- Graceful shutdown with device notification
"""

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from loguru import logger

from services.database import AsyncSessionLocal, init_db
from services.device_manager import get_device_manager
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


async def _init_plugins(app: "FastAPI"):
    """Initialize the plugin system."""
    if settings.plugins_enabled:
        try:
            from integrations.core.plugin_loader import PluginLoader
            from integrations.core.plugin_registry import PluginRegistry

            loader = PluginLoader(settings.plugins_dir)
            plugins = loader.load_all_plugins()

            plugin_registry = PluginRegistry()
            plugin_registry.register_plugins(plugins)

            app.state.plugin_registry = plugin_registry
            logger.info(f"✅ Plugin System bereit: {len(plugins)} plugins geladen")
        except Exception as e:
            logger.error(f"❌ Plugin System konnte nicht geladen werden: {e}")
            app.state.plugin_registry = None
    else:
        app.state.plugin_registry = None
        logger.info("⏭️  Plugin System deaktiviert")


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


def _schedule_ha_keywords_preload():
    """Schedule Home Assistant keywords preloading in background."""
    try:
        from integrations.homeassistant import HomeAssistantClient

        async def preload_keywords():
            """Load HA keywords in background."""
            try:
                ha_client = HomeAssistantClient()
                keywords = await ha_client.get_keywords()
                logger.info(f"✅ Home Assistant Keywords vorgeladen: {len(keywords)} Keywords")
            except Exception as e:
                logger.warning(f"⚠️  Keywords konnten nicht vorgeladen werden: {e}")

        task = asyncio.create_task(preload_keywords())
        _startup_tasks.append(task)
    except Exception as e:
        logger.warning(f"⚠️  Keyword-Preloading fehlgeschlagen: {e}")


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
    except Exception as e:
        logger.error(f"❌ Agent Router konnte nicht initialisiert werden: {e}")
        import traceback
        logger.error(traceback.format_exc())
        app.state.agent_router = None
        app.state.agent_roles_config = None


async def _init_zeroconf(app: "FastAPI"):
    """Initialize Zeroconf service for satellite auto-discovery."""
    zeroconf_service = None
    try:
        from services.zeroconf_service import get_zeroconf_service
        zeroconf_service = get_zeroconf_service(port=8000)
        await zeroconf_service.start()
        app.state.zeroconf_service = zeroconf_service
    except Exception as e:
        logger.warning(f"⚠️  Zeroconf Service konnte nicht gestartet werden: {e}")
    return zeroconf_service


async def _cancel_startup_tasks():
    """Cancel any pending startup tasks."""
    for task in _startup_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _notify_devices_shutdown():
    """Notify all connected devices about server shutdown."""
    try:
        device_manager = get_device_manager()
        shutdown_msg = {"type": "server_shutdown", "message": "Server is shutting down"}
        for device in list(device_manager.devices.values()):
            try:
                await device.websocket.send_json(shutdown_msg)
                await device.websocket.close(code=1001, reason="Server shutdown")
            except Exception:
                pass
        logger.info(f"👋 Notified {len(device_manager.devices)} devices about shutdown")
    except Exception as e:
        logger.warning(f"⚠️ Error notifying devices: {e}")


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    """
    Application lifespan context manager.

    Handles startup and shutdown of all services:
    - Database initialization
    - Authentication system setup
    - Ollama LLM service
    - Task queue
    - Plugin system
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

    # Startup sequence
    await _init_database()
    await _init_auth()
    await _init_ollama(app)
    await _init_task_queue(app)
    await _init_plugins(app)
    await _init_mcp(app)
    await _init_agent_router(app)

    # Background preloading
    _schedule_whisper_preload()
    _schedule_ha_keywords_preload()
    _schedule_notification_cleanup()
    _schedule_reminder_checker()
    _schedule_memory_cleanup()

    # Zeroconf for satellite discovery
    zeroconf_service = await _init_zeroconf(app)

    yield

    # Shutdown sequence
    logger.info("👋 Renfield wird heruntergefahren...")

    await _cancel_startup_tasks()
    await _notify_devices_shutdown()

    # Shutdown MCP
    if getattr(app.state, "mcp_manager", None):
        await app.state.mcp_manager.shutdown()

    if zeroconf_service:
        await zeroconf_service.stop()

    # Close HTTP client singletons
    from integrations.frigate import close_frigate_client
    from integrations.homeassistant import close_ha_client
    await close_ha_client()
    await close_frigate_client()

    logger.info("✅ Shutdown complete")
