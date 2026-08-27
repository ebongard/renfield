"""Built-in scheduled tasks (#1137) — seed rows + their handlers.

Each handler **re-asserts its full runtime gate** (Review H4): the seed
``enabled`` flag only sets the row's initial state; a settings flag that gates
whether the work should happen must be re-checked inside the handler, because a
migrated service function may not carry that gate itself.

Phase 1 seeds three tasks to prove the engine end-to-end:
  * ``paperless_dedupe``      — the first real built-in; self-gates on the
                                runtime flag (Review M7) so a ConfigMap env-flip
                                activates it before the Phase-2 UI toggle exists.
  * ``federation_audit_cleanup`` and ``upload_cleanup`` — two trivial migrations
                                (their legacy ``_schedule_*`` are removed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

from models.database import SCHEDULE_KIND_INTERVAL
from services.scheduled_tasks.registry import register_handler
from utils.config import settings

if TYPE_CHECKING:
    from fastapi import FastAPI


@dataclass(frozen=True)
class TaskSeed:
    name: str
    handler_key: str
    interval_seconds: int | None = None
    cron_expr: str | None = None
    schedule_kind: str = SCHEDULE_KIND_INTERVAL
    run_at_boot: bool = False
    enabled: bool = True
    params: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _paperless_dedupe_handler(app: "FastAPI", params: dict) -> str | None:
    """Autonomously drain the Paperless duplicate backlog by calling
    ``mcp.paperless.dedupe_documents`` (recoverable trash, keep-lowest-id). The
    job re-runs each interval until ``remaining`` reaches 0, then idles (a clean
    archive returns 0 groups → no deletes)."""
    # Runtime self-gate (Review M7): the row is seeded enabled; this flag decides
    # whether the work actually runs, so a ConfigMap flip activates it in Phase 1.
    if not settings.paperless_dedupe_reconciler_enabled:
        return "skipped: paperless_dedupe_reconciler_enabled is off"

    mcp_manager = getattr(app.state, "mcp_manager", None)
    if mcp_manager is None:
        return "skipped: no mcp_manager"

    from services.folder_ingest_paperless import _parse_paperless_result
    from services.paperless_dedupe_tool import looks_like_dedupe_result

    max_delete = int(params.get("max_delete") or settings.paperless_dedupe_reconciler_max_delete)
    # truncate=False is essential — the dedupe response can be large (see the
    # 2026-08 truncate incident); a truncated payload would be unparseable.
    res = _parse_paperless_result(await mcp_manager.execute_tool(
        "mcp.paperless.dedupe_documents",
        {
            "dry_run": False,
            "max_delete": max_delete,
            "metadata_match": settings.paperless_dedupe_metadata_match_enabled,
        },
        truncate=False,
    ))
    if res.get("error"):
        raise RuntimeError(f"dedupe_documents error: {res['error']}")
    # Contract-marker guard (mirrors the interactive tool): MCPManager fuzzy-falls-
    # back an UNKNOWN tool to another paperless tool instead of erroring, so an old
    # MCP (< 1.12.0) would yield a foreign response with deleted/remaining absent →
    # this would report last_status='ok' + "deleted=0" forever while the backlog
    # never drains. RAISE instead so it surfaces as last_status='error'.
    if not looks_like_dedupe_result(res):
        raise RuntimeError(
            "dedupe_documents unavailable (Paperless MCP < 1.12.0 / fuzzy-fallback)"
        )
    deleted = res.get("deleted", 0)
    remaining = res.get("remaining", 0)
    complete = res.get("complete")
    return f"deleted={deleted} remaining={remaining} complete={complete}"


async def _federation_audit_cleanup_handler(app: "FastAPI", params: dict) -> str | None:
    """F4d — retention prune of the federation query audit log (no runtime gate;
    matches the legacy always-on scheduler)."""
    from services.federation_audit import prune_old_audit_rows

    await prune_old_audit_rows()
    return None


async def _upload_cleanup_handler(app: "FastAPI", params: dict) -> str | None:
    """Prune old non-indexed chat uploads. Re-asserts the runtime gate (Review
    H4) — the legacy scheduler simply didn't start when the flag was off."""
    if not settings.chat_upload_cleanup_enabled:
        return "skipped: chat_upload_cleanup_enabled is off"

    from api.routes.chat_upload import _cleanup_uploads
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db_session:
        deleted_count, deleted_files = await _cleanup_uploads(
            db_session, settings.chat_upload_retention_days
        )
    if deleted_count:
        logger.info(
            f"Upload cleanup: {deleted_count} uploads deleted ({deleted_files} files, "
            f"retention={settings.chat_upload_retention_days}d)"
        )
    return f"deleted={deleted_count}" if deleted_count else None


# --- Phase 3 Batch A: low-risk interval jobs (NONE / service-side gate) ------

# Last-seen daypart, moved verbatim from the legacy _schedule_daypart_watcher.
_daypart_watcher_state: dict[str, str | None] = {"last": None}


async def _daypart_watcher_handler(app: "FastAPI", params: dict) -> str | None:
    """Fire the ``daypart_changed`` hook on a day/evening/night transition (e.g.
    for satellite LED dimming). No runtime gate — always runs; stateless apart
    from the module-level last-seen daypart."""
    from services.daypart_service import get_daypart_info
    from utils.hooks import run_hooks

    info = get_daypart_info()
    current = info["daypart"]
    local_time = info["local_time"]
    previous = _daypart_watcher_state["last"]
    if current == previous:
        return None
    # run_hooks never raises — handler exceptions are logged inside.
    await run_hooks("daypart_changed", previous=previous, current=current, local_time=local_time)
    _daypart_watcher_state["last"] = current
    logger.info(f"🌓 Daypart transition: {previous} → {current} (local {local_time})")
    return f"{previous} -> {current}"


async def _paperless_finalize_reconciler_handler(app: "FastAPI", params: dict) -> str | None:
    """Restart-safe backstop for the interactive Paperless-commit finalize (#658):
    re-run finalizes still pending past the grace via a live mcp_manager. No gate
    — a cheap no-op when idle (empty query) or when mcp_manager is None."""
    from services.paperless_finalize_reconciler import reconcile_pending_finalizes

    await reconcile_pending_finalizes(getattr(app.state, "mcp_manager", None))
    return None


async def _mcp_health_monitor_handler(app: "FastAPI", params: dict) -> str | None:
    """MCP health self-detection: poll the MCP fleet + alert on a new degraded/down
    server. ``monitor_tick`` re-checks ``mcp_health_monitor_enabled`` internally,
    but re-assert it here too (H4 discipline) so the runtime flag fully gates the
    work and a ConfigMap flip activates it without a UI toggle (M7)."""
    if not settings.mcp_health_monitor_enabled:
        return "skipped: mcp_health_monitor_enabled is off"
    from services.mcp_health_monitor import monitor_tick

    await monitor_tick(app)
    return None


# ---------------------------------------------------------------------------
# Registration + seeds
# ---------------------------------------------------------------------------

def register_builtin_handlers() -> None:
    """Register every built-in handler. Idempotent — called once at lifespan."""
    register_handler("paperless_dedupe", _paperless_dedupe_handler)
    register_handler("federation_audit_cleanup", _federation_audit_cleanup_handler)
    register_handler("upload_cleanup", _upload_cleanup_handler)
    # Phase 3 Batch A
    register_handler("daypart_watcher", _daypart_watcher_handler)
    register_handler("paperless_finalize_reconciler", _paperless_finalize_reconciler_handler)
    register_handler("mcp_health_monitor", _mcp_health_monitor_handler)


def builtin_task_seeds() -> list[TaskSeed]:
    """The built-in task rows ensure_builtin_tasks() creates (ON CONFLICT DO
    NOTHING). ``enabled`` seeds the row's initial state from the settings flag
    at first create; the UI owns it thereafter."""
    return [
        TaskSeed(
            name="Paperless-Duplikate aufräumen",
            handler_key="paperless_dedupe",
            interval_seconds=settings.paperless_dedupe_reconciler_interval,
            # Seeded enabled; the handler self-gates on the runtime flag (M7).
            enabled=True,
        ),
        TaskSeed(
            name="Federation-Audit aufräumen",
            handler_key="federation_audit_cleanup",
            interval_seconds=3600,
            enabled=True,
        ),
        TaskSeed(
            name="Chat-Uploads aufräumen",
            handler_key="upload_cleanup",
            interval_seconds=3600,
            enabled=settings.chat_upload_cleanup_enabled,
        ),
        # --- Phase 3 Batch A (all run_at_boot, like their legacy schedulers) ---
        TaskSeed(
            name="Tageszeit-Wächter",
            handler_key="daypart_watcher",
            interval_seconds=300,
            run_at_boot=True,
            enabled=True,
        ),
        TaskSeed(
            name="Paperless-Finalisierung nachziehen",
            handler_key="paperless_finalize_reconciler",
            interval_seconds=settings.paperless_finalize_reconciler_interval,
            run_at_boot=True,
            enabled=True,
        ),
        TaskSeed(
            name="MCP-Gesundheitsmonitor",
            handler_key="mcp_health_monitor",
            interval_seconds=settings.mcp_health_monitor_interval,
            # Seeded enabled; the handler self-gates on mcp_health_monitor_enabled (M7).
            run_at_boot=True,
            enabled=True,
        ),
    ]
