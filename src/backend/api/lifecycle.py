"""
Application lifecycle management for Renfield AI Assistant.

This module handles:
- Startup initialization (database, services)
- Background task management
- Graceful shutdown with device notification
"""

import asyncio
from collections.abc import Awaitable, Callable
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


def _spawn_periodic_task(
    *,
    name: str,
    interval: int,
    work: Callable[[], Awaitable[None]],
    started_msg: str,
    run_at_boot: bool = False,
) -> None:
    """Spawn a fire-and-forget background task that runs ``work`` every
    ``interval`` seconds.

    Contract:
      - ``work`` is a no-arg async callable invoked once per tick.
      - ``CancelledError`` terminates the loop cleanly on shutdown.
      - Any other exception is logged at WARNING and the loop continues
        — transient DB hiccups must not kill the scheduler permanently.
      - The created task is appended to ``_startup_tasks`` so the
        graceful-shutdown path can cancel it.
      - ``started_msg`` is logged at INFO on spawn so log aggregation
        sees the same "X scheduler started" line as before this helper
        existed.
      - ``run_at_boot`` (opt-in): run one tick promptly after spawn,
        BEFORE the first ``sleep(interval)``, then fall into the normal
        cadence. Required for schedulers whose ``interval`` is on the
        order of (or longer than) the pod's lifetime: the plain
        sleep-then-work loop fires its first tick ``interval`` seconds
        after boot and the timer resets on every restart, so a pod that
        recycles more often than ``interval`` NEVER runs the work (#678).
        Leave False for short-interval schedulers — they reach their
        first real tick well within a normal pod lifetime.

    Gates (``settings.X_enabled``) are the caller's responsibility — they
    decide whether the scheduler runs AT ALL, distinct from the loop
    body which decides what work happens per tick.
    """
    async def _loop() -> None:
        if run_at_boot:
            try:
                await work()
            # CancelledError (BaseException, not Exception) is intentionally
            # NOT caught here: a shutdown cancel during the boot tick should
            # propagate so the task settles as cancelled, same as the loop
            # below. Only a genuine work() failure is logged and swallowed,
            # so a transient boot-run error still falls into the interval
            # loop instead of disabling the scheduler.
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{name} failed (boot run): {e}")
        while True:
            try:
                await asyncio.sleep(interval)
                await work()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{name} failed: {e}")

    task = asyncio.create_task(_loop())
    _startup_tasks.append(task)
    logger.info(started_msg)


async def _init_database():
    """Initialize database and run migrations."""
    await init_db()
    logger.info("✅ Datenbank initialisiert")


async def _reconcile_credentials_boot():
    """Self-heal DB-stored integration tokens from their authoritative Secret
    source at boot (services/credential_reconciler). No-op unless a token diverged
    (e.g. after a DB wipe). Never blocks startup."""
    try:
        from services.credential_reconciler import reconcile_credentials

        await reconcile_credentials()
    except Exception as e:  # noqa: BLE001 - never block startup
        logger.warning(f"credential-reconciler boot pass failed: {e}")


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


def _schedule_reminder_checker():
    """Start the periodic reminder checker (Phase 3b)."""
    if not settings.proactive_reminders_enabled:
        return

    async def _tick():
        from services.reminder_service import check_due_reminders
        await check_due_reminders()

    _spawn_periodic_task(
        name="Reminder check",
        interval=settings.proactive_reminder_check_interval,
        work=_tick,
        started_msg=(
            f"✅ Reminder Checker gestartet "
            f"(interval={settings.proactive_reminder_check_interval}s)"
        ),
    )


def _schedule_kiosk_weather_refresh(app):
    """Backend-internal weather refresher → PUSHES a ``weather_updated`` delta to
    the kiosk hub when the reading changes.

    This is the §1.6-sanctioned exception to the no-polling rule: Open-Meteo has
    no push, so a backend timer refreshes the external cache at the TTL cadence
    and streams new values to the wall displays — the kiosk's browser never polls
    our API for weather. Diff-gated in ``refresh_and_push_kiosk_weather`` so a
    tick that produces the same reading stays silent. Gated on weather being on
    AND a kiosk location configured; a no-op otherwise.
    """
    location = (settings.kiosk_weather_location or "").strip()
    if not settings.weather_enabled or not location:
        return

    from api.websocket.kiosk_data import _WEATHER_TTL_SECONDS

    async def _tick():
        from api.websocket.kiosk_data import refresh_and_push_kiosk_weather
        from api.websocket.kiosk_handler import _kiosk_clients

        # No wall display connected → skip the external MCP round-trip nobody
        # would consume (the broadcast would no-op anyway).
        if not _kiosk_clients:
            return
        mgr = getattr(app.state, "mcp_manager", None)
        if mgr is None:
            return
        await refresh_and_push_kiosk_weather(mgr)

    _spawn_periodic_task(
        name="Kiosk weather refresh",
        interval=_WEATHER_TTL_SECONDS,
        work=_tick,
        started_msg=(
            f"Kiosk weather refresher gestartet "
            f"(interval={_WEATHER_TTL_SECONDS}s, location={location})"
        ),
    )


def _schedule_kiosk_internal_health_refresh(app):
    """Backend-internal refresher → PUSHES an ``internal_health_changed`` delta to
    the kiosk hub when a knowledge/presence/media verdict changes.

    Same no-poll model as the weather refresher: the backing state (enrollment/
    auth, ingest worker liveness, Redis queue depth) has no push of its own, so a
    backend timer recomputes it and streams only actual changes to the wall
    displays — the kiosk browser never polls. Diff-gated in
    ``refresh_and_push_internal_health``; skipped when no wall display is
    connected so it does no work nobody would consume.
    """
    from api.websocket.kiosk_data import _INTERNAL_HEALTH_REFRESH_SECONDS

    async def _tick():
        from api.websocket.kiosk_data import refresh_and_push_internal_health
        from api.websocket.kiosk_handler import _kiosk_clients

        if not _kiosk_clients:
            return
        await refresh_and_push_internal_health()

    _spawn_periodic_task(
        name="Kiosk internal-health refresh",
        interval=_INTERNAL_HEALTH_REFRESH_SECONDS,
        work=_tick,
        started_msg=(
            f"Kiosk internal-health refresher gestartet "
            f"(interval={_INTERNAL_HEALTH_REFRESH_SECONDS}s)"
        ),
    )


def _schedule_kiosk_peer_status_refresh(app):
    """Backend-internal refresher → PUSHES a ``peer_status_changed`` delta to the
    kiosk hub when federation-peer reachability changes.

    Same no-poll model as the weather + internal-health refreshers: peer
    ``last_seen_at`` has no push of its own, so a backend timer recomputes
    reachability and streams only actual changes to the wall displays. Diff-gated
    in ``refresh_and_push_peer_status``; skipped when no wall display is connected.
    """
    from api.websocket.kiosk_data import _PEER_STATUS_REFRESH_SECONDS

    async def _tick():
        from api.websocket.kiosk_data import refresh_and_push_peer_status
        from api.websocket.kiosk_handler import _kiosk_clients

        if not _kiosk_clients:
            return
        await refresh_and_push_peer_status()

    _spawn_periodic_task(
        name="Kiosk peer-status refresh",
        interval=_PEER_STATUS_REFRESH_SECONDS,
        work=_tick,
        started_msg=(
            f"Kiosk peer-status refresher gestartet "
            f"(interval={_PEER_STATUS_REFRESH_SECONDS}s)"
        ),
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

        # Federation peer registry (F3c) — register paired PeerUsers as
        # virtual FEDERATION-transport MCP servers so the agent loop sees
        # `mcp.peer_<id>.query_brain` alongside every other MCP tool.
        # Non-fatal on failure: an empty peer list or schema-migration-
        # not-yet-applied DB should not block backend startup.
        try:
            from services.peer_mcp_registry import sync_peers
            async with AsyncSessionLocal() as peer_session:
                await sync_peers(manager, peer_session)
        except Exception as peer_error:
            logger.warning(
                f"Federation peer registry sync skipped at startup: {peer_error}"
            )
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


# Startup-plugin load outcomes, keyed by spec ("module:callable"). Populated by
# _load_one_plugin so health surfaces (the kiosk) can tell a plugin that FAILED
# to load apart from one that simply registered nothing — the two were
# indistinguishable before (a failed plugin just silently never called
# register_hook). Read via get_plugin_status()/failed_plugins().
_plugin_status: dict[str, dict[str, object]] = {}


def get_plugin_status() -> dict[str, dict[str, object]]:
    """Snapshot of startup-plugin load outcomes: spec → {ok, error}."""
    return dict(_plugin_status)


def failed_plugins() -> list[str]:
    """Specs whose module failed to load (ok=False)."""
    return [spec for spec, st in _plugin_status.items() if not st.get("ok")]


async def _load_one_plugin(spec: str):
    """Load and invoke a single plugin spec.

    Format: "package.module:callable" — the callable receives no args
    and is expected to call register_hook() for the events it cares about.
    A failing plugin is logged and swallowed so it cannot crash startup, but the
    outcome is recorded in _plugin_status so health surfaces can flag it.
    """
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

        _plugin_status[spec] = {"ok": True, "error": None}
        logger.info(f"Plugin module loaded: {spec}")
    except Exception as e:
        _plugin_status[spec] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        logger.opt(exception=True).error(f"Failed to load plugin module: {spec}")


async def _load_plugin_module():
    """Load all configured startup plugins.

    Sources, in order: settings.plugin_module (singular, backward-compat) then
    settings.plugin_modules (a comma-separated list of "module:callable"
    entries). Entries are deduped so the same spec is invoked only once even if
    it appears in both. Each is loaded independently — one failing plugin is
    logged and skipped, never crashing startup.
    """
    specs: list[str] = []
    seen: set[str] = set()
    for raw in [settings.plugin_module, *settings.plugin_modules.split(",")]:
        spec = raw.strip()
        if spec and spec not in seen:
            seen.add(spec)
            specs.append(spec)

    for spec in specs:
        await _load_one_plugin(spec)


async def _init_paperless_audit(app: "FastAPI") -> None:
    """Mount the Paperless audit REST router + start its service, from PLATFORM CORE.

    The audit code lives under ``ha_glue/`` for historical packaging reasons, but it
    needs no Home Assistant — only the Paperless MCP + ``paperless_audit_enabled``.
    Mounting it here (not inside the ha_glue plugin) makes it available on HA-less
    instances (e.g. the business/xidra deploy that never loads ha_glue), which is
    where it previously 404'd. This is the SINGLE owner of the mount — the ha_glue
    plugin no longer mounts it, so there is no double-include on HA deploys.

    Gated: no-op unless ``paperless_audit_enabled`` AND the Paperless MCP server is
    configured. Stores the service on ``app.state.paperless_audit`` for the shutdown
    path. Best-effort — a failure here must not break startup.
    """
    try:
        from ha_glue.utils.config import ha_glue_settings

        if not ha_glue_settings.paperless_audit_enabled:
            return
        mcp_manager = getattr(app.state, "mcp_manager", None)
        if not mcp_manager or not mcp_manager.has_server("paperless"):
            logger.info("Paperless MCP not configured — audit disabled")
            return

        from ha_glue.api.routes.paperless_audit import router as audit_router
        from ha_glue.services.paperless_audit_service import PaperlessAuditService
        from services.database import AsyncSessionLocal

        app.include_router(audit_router)
        audit_service = PaperlessAuditService(
            mcp_manager=mcp_manager, db_factory=AsyncSessionLocal
        )
        app.state.paperless_audit = audit_service
        await audit_service.start()
        logger.info("✅ Paperless Audit: routes mounted + service started (platform-core)")
    except Exception:  # noqa: BLE001 — never break startup on the audit mount
        logger.opt(exception=True).warning("Paperless audit init failed")


async def _setup_task_engine(app):
    """Start the Scheduled Tasks engine (#1137, docs/design/scheduled-tasks.md).

    One-time boot work (register handlers, seed built-ins ON CONFLICT DO NOTHING,
    boot-force run_at_boot tasks — the #678 fix) then the periodic engine tick.
    The engine always runs; built-ins carry their existing enabled-defaults and
    the paperless-dedupe job self-gates on its runtime flag, so this is inert
    until a task is activated. Failures log at ERROR (a dead engine stops every
    scheduled task) but never break startup."""
    from services.scheduled_tasks.builtins import register_builtin_handlers
    from services.scheduled_tasks.engine import (
        ensure_builtin_tasks,
        force_run_at_boot_tasks,
        run_engine_tick,
    )

    # Handler registration is pure in-process dict inserts and must succeed for
    # tasks to resolve; if it somehow fails, don't start a tick loop that can only
    # skip everything.
    try:
        register_builtin_handlers()
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).error(
            "Scheduled Tasks handler registration failed — engine not started"
        )
        return

    # One-time seed + boot-force is BEST-EFFORT and DECOUPLED from the tick loop:
    # run_engine_tick only SELECTs due rows (already present from a prior boot), so
    # a transient DB error here must not stop the resilient periodic tick for the
    # pod's whole lifetime. Log at ERROR (new built-ins may be missing this boot).
    try:
        await ensure_builtin_tasks()
        await force_run_at_boot_tasks()
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).error(
            "Scheduled Tasks seeding/boot-force failed — starting the engine anyway "
            "(existing rows still run; new built-ins may be missing until next boot)"
        )

    async def _tick():
        await run_engine_tick(app)

    try:
        _spawn_periodic_task(
            name="Scheduled tasks engine",
            interval=settings.scheduled_tasks_engine_tick_seconds,
            work=_tick,
            started_msg=(
                f"Scheduled Tasks Engine gestartet "
                f"(tick={settings.scheduled_tasks_engine_tick_seconds}s, "
                f"max_concurrent={settings.scheduled_tasks_max_concurrent})"
            ),
            run_at_boot=True,
        )
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).error("Scheduled Tasks engine tick loop failed to start")


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

    # Block startup if federation requires a persisted identity but the key was
    # generated ephemerally (operator forgot to provision the secret). Loud boot
    # failure beats silently-broken pairings a deploy later. No-op unless
    # federation_require_persistent_identity is set.
    from services.federation_identity import enforce_persistent_identity
    try:
        enforce_persistent_identity()
    except (RuntimeError, ValueError) as e:
        # RuntimeError = ephemeral-key-when-required; ValueError = the provisioned
        # persisted key is malformed (e.g. 33-byte trailing-newline). Both are a
        # clean, deliberate boot failure — never a silent ephemeral fallback that
        # would break pairings.
        logger.critical(f"Federation identity check failed: {e}")
        raise SystemExit(1) from e

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
    # Self-heal DB-stored integration tokens from their authoritative Secret source
    # so a DB wipe doesn't leave folder/email-ingest pushes 403ing (2026-07 incident).
    await _reconcile_credentials_boot()

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
    _schedule_reminder_checker()
    _schedule_notification_poller(app)
    _schedule_kiosk_weather_refresh(app)
    _schedule_kiosk_internal_health_refresh(app)
    _schedule_kiosk_peer_status_refresh(app)
    await _setup_task_engine(app)

    # Self-learning Phase 1: load bundled seed skills into the database.
    # Idempotent — seeds with a matching title are skipped, so re-running
    # on every boot is safe. Gated on skills_enabled + skill_seed_load_on_boot.
    if settings.skills_enabled and settings.skill_seed_load_on_boot:
        try:
            from services.skill_seed_loader import load_all_seeds
            async with AsyncSessionLocal() as db_session:
                loaded = await load_all_seeds(db_session)
                if loaded:
                    logger.info(f"🌱 Skill seeds loaded: {loaded}")
        except Exception as e:
            logger.warning(f"⚠️  Skill seed loading failed: {e}")

    # Presence / paperless audit / media follow / conversation handoff /
    # Zeroconf satellite discovery are bootstrapped by ha_glue via its
    # startup hook handler (fired below by `run_hooks("startup", ...)`).
    # Each subsystem gates itself on the relevant `ha_glue_settings.X`
    # flag internally. ha_glue also handles its own shutdown cleanup via
    # `shutdown` and `shutdown_finalize` hook handlers.

    # Knowledge Graph message/context hooks (chat path, API-pod only).
    if settings.knowledge_graph_enabled:
        from services.knowledge_graph_service import (
            kg_post_message_hook,
            kg_retrieve_context_hook,
        )
        from utils.hooks import register_hook

        register_hook("post_message", kg_post_message_hook)
        register_hook("retrieve_context", kg_retrieve_context_hook)
        logger.info("✅ Knowledge Graph message/context hooks registered")

    # post_document_ingest consumers (KG + Schicht A field extractor). Shared
    # with the document-worker via services/document_ingest_hooks.py — the
    # worker is the primary ingestion path and registers these in its own
    # startup, so the registration logic lives in one place to avoid drift.
    from services.document_ingest_hooks import register_document_ingest_hooks

    register_document_ingest_hooks()

    # Whisper prompt cache invalidation — listen on household_graph_changed.
    from services.whisper_prompt_builder import whisper_prompt_household_changed
    from utils.hooks import register_hook as _register_hook

    _register_hook("household_graph_changed", whisper_prompt_household_changed)
    logger.info("✅ Whisper prompt-cache invalidation hook registered")

    # Speaker vocabulary handler. Registered BEFORE the platform default has a
    # chance to run (no platform default registers on this hook — the default
    # is the inline fallback inside WhisperPromptBuilder._resolve, only used
    # when all hook handlers return None). The vocab handler returns None on
    # cold-start (no rows yet) so the platform default kicks in transparently.
    if settings.speaker_vocab_capture_enabled:
        from services.speaker_vocabulary_service import vocab_initial_prompt_handler

        _register_hook("build_whisper_initial_prompt", vocab_initial_prompt_handler)
        logger.info("✅ Speaker vocabulary STT-bias hook registered")

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

    # Paperless audit — mounted from platform core (not the ha_glue plugin) so it
    # works on HA-less instances too. Runs after register_routes so it's the single
    # mount owner regardless of whether ha_glue is loaded.
    await _init_paperless_audit(app)

    yield

    # Shutdown sequence
    logger.info("👋 Renfield wird heruntergefahren...")

    from utils.hooks import run_hooks
    await run_hooks("shutdown", app=app)

    await _cancel_startup_tasks()

    # Drain in-flight Scheduled Tasks runs (the tick loop is cancelled above, but
    # the per-task runs it spawned are tracked separately) — BEFORE MCP shutdown
    # so a mid-flight handler using the MCP (e.g. paperless-dedupe) unwinds cleanly.
    try:
        from services.scheduled_tasks.engine import drain_running_tasks
        await drain_running_tasks()
    except Exception:  # noqa: BLE001 — shutdown drain must never break teardown
        logger.opt(exception=True).warning("Scheduled Tasks drain failed")

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

    # Close the shared Redis client used by the knowledge routes (#388).
    try:
        from services.redis_client import close_redis
        await close_redis()
    except Exception as e:  # pragma: no cover — defensive cleanup
        logger.warning(f"redis close failed: {e}")

    logger.info("✅ Shutdown complete")
