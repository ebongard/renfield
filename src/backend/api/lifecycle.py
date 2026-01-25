"""
Application lifecycle management for Renfield AI Assistant.

This module handles:
- Startup initialization (database, services, plugins)
- Background task management
- Graceful shutdown with device notification
"""

from contextlib import asynccontextmanager
from typing import List, TYPE_CHECKING
import asyncio

from loguru import logger

from services.database import init_db, AsyncSessionLocal
from services.ollama_service import OllamaService
from services.task_queue import TaskQueue
from services.device_manager import get_device_manager
from utils.config import settings

if TYPE_CHECKING:
    from fastapi import FastAPI

# Track background tasks for graceful shutdown
_startup_tasks: List[asyncio.Task] = []


async def _init_database():
    """Initialize database and run migrations."""
    await init_db()
    logger.info("✅ Datenbank initialisiert")


async def _init_auth():
    """Initialize authentication system with default roles and admin user."""
    try:
        from services.auth_service import ensure_default_roles, ensure_admin_user

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

    # Startup sequence
    await _init_database()
    await _init_auth()
    await _init_ollama(app)
    await _init_task_queue(app)
    await _init_plugins(app)

    # Background preloading
    _schedule_whisper_preload()
    _schedule_ha_keywords_preload()

    # Zeroconf for satellite discovery
    zeroconf_service = await _init_zeroconf(app)

    yield

    # Shutdown sequence
    logger.info("👋 Renfield wird heruntergefahren...")

    await _cancel_startup_tasks()
    await _notify_devices_shutdown()

    if zeroconf_service:
        await zeroconf_service.stop()

    logger.info("✅ Shutdown complete")
