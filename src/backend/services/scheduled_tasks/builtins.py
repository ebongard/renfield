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
        # The single-call full-archive dedupe exceeds the default 30s on a large
        # archive — raise the per-call timeout (else the drain never progresses).
        call_timeout=settings.paperless_dedupe_call_timeout_s,
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


# --- Phase 3 Batch B: single wrapper-gated interval jobs ---------------------
# Each re-asserts its runtime gate in-handler (H4): the legacy _schedule_* gated
# in the wrapper but the service fn does not, so a naive call would run the work
# with the flag off. Seeded enabled + in-handler gate (M7) so a ConfigMap flag
# flip controls the work at runtime, preserving the legacy behavior.

async def _notification_cleanup_handler(app: "FastAPI", params: dict) -> str | None:
    if not settings.proactive_enabled:
        return "skipped: proactive_enabled is off"
    from services.database import AsyncSessionLocal
    from services.notification_service import NotificationService

    async with AsyncSessionLocal() as db_session:
        await NotificationService(db_session).cleanup_expired()
    return None


async def _memory_cleanup_handler(app: "FastAPI", params: dict) -> str | None:
    if not settings.memory_enabled:
        return "skipped: memory_enabled is off"
    from services.conversation_memory_service import ConversationMemoryService
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db_session:
        counts = await ConversationMemoryService(db_session).cleanup()
        if sum(counts.values()) > 0:
            from utils.metrics import record_memory_cleanup

            record_memory_cleanup(counts)

    # Episodic sub-branch (inline runtime sub-gate, preserved from the legacy tick).
    if settings.memory_episodic_enabled:
        from sqlalchemy import func, select

        from models.database import EpisodicMemory
        from services.episodic_memory_service import EpisodicMemoryService

        async with AsyncSessionLocal() as db_session:
            ep_svc = EpisodicMemoryService(db_session)
            ep_counts = await ep_svc.cleanup()
            if sum(ep_counts.values()) > 0:
                logger.info(f"Episodic cleanup: {ep_counts}")
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
    return None


async def _meeting_retention_handler(app: "FastAPI", params: dict) -> str | None:
    if not settings.meeting_transcription_enabled:
        return "skipped: meeting_transcription_enabled is off"
    from services.meeting_retention import cleanup_meetings

    audio_deleted, meetings_purged = await cleanup_meetings()
    if audio_deleted or meetings_purged:
        logger.info(f"Meeting retention: {audio_deleted} audio freed, {meetings_purged} purged")
        return f"audio={audio_deleted} purged={meetings_purged}"
    return None


async def _trajectory_cleanup_handler(app: "FastAPI", params: dict) -> str | None:
    if not settings.trajectory_capture_enabled:
        return "skipped: trajectory_capture_enabled is off"
    from services.database import AsyncSessionLocal
    from services.trajectory_service import TrajectoryService

    async with AsyncSessionLocal() as db_session:
        await TrajectoryService(db_session).purge_expired()
    return None


async def _kg_conflation_monitor_handler(app: "FastAPI", params: dict) -> str | None:
    if not settings.kg_conflation_monitor_enabled:
        return "skipped: kg_conflation_monitor_enabled is off"
    from services.database import AsyncSessionLocal
    from services.kg_conflation_monitor import KgConflationMonitor

    async with AsyncSessionLocal() as db_session:
        await KgConflationMonitor(db_session).scan_all()
    return None


async def _paperless_reconciler_handler(app: "FastAPI", params: dict) -> str | None:
    if not (settings.folder_ingest_to_paperless or settings.email_ingest_to_paperless):
        return "skipped: folder/email ingest-to-Paperless is off"
    from services.paperless_reconciler import reenqueue_pending_paperless

    await reenqueue_pending_paperless()
    return None


# --- Phase 3 Batch C: compound-gated / per-user / special --------------------

async def _obligation_deadline_notifier_handler(app: "FastAPI", params: dict) -> str | None:
    # Compound gate (H4): running with proactive off would CONSUME the ledger
    # (mark milestones sent) without delivering — elapsed reminders lost. Require BOTH.
    if not (settings.obligation_notifier_enabled and settings.proactive_enabled):
        return "skipped: obligation_notifier_enabled AND proactive_enabled required"
    from services.obligation_deadline_notifier import scan_all_users

    await scan_all_users()
    return None


async def _obligation_digest_handler(app: "FastAPI", params: dict) -> str | None:
    if not (settings.obligation_digest_enabled and settings.proactive_enabled):
        return "skipped: obligation_digest_enabled AND proactive_enabled required"
    from services.obligation_digest import scan_all_users

    await scan_all_users()
    return None


async def _obligation_calendar_sync_handler(app: "FastAPI", params: dict) -> str | None:
    if not settings.obligation_calendar_sync_enabled:
        return "skipped: obligation_calendar_sync_enabled is off"
    from services.obligation_calendar_sync import reconcile_all_users

    mgr = getattr(app.state, "mcp_manager", None)
    if mgr is None:
        return "skipped: mcp_manager not ready"
    await reconcile_all_users(mgr)
    return None


async def _speaker_vocab_rebuild_handler(app: "FastAPI", params: dict) -> str | None:
    # The legacy scheduler ran only under the OUTER lifespan gate features["voice"]
    # AND speaker_vocab_capture_enabled — re-assert BOTH (the voice flag is easy to miss).
    if not (settings.features.get("voice") and settings.speaker_vocab_capture_enabled):
        return "skipped: voice feature AND speaker_vocab_capture_enabled required"
    from services.database import AsyncSessionLocal
    from services.speaker_vocabulary_service import rebuild_vocabulary

    async with AsyncSessionLocal() as session:
        stats = await rebuild_vocabulary(db_session=session)
    logger.info(f"📚 Speaker vocab rebuilt: {stats}")
    return None


async def _skill_curator_handler(app: "FastAPI", params: dict) -> str | None:
    if not (settings.skills_enabled and settings.skill_curator_enabled):
        return "skipped: skills_enabled AND skill_curator_enabled required"
    from services.database import AsyncSessionLocal
    from services.skill_curator_service import SkillCuratorService

    # Enumerate user ids in one session (closed before iterating), then a fresh
    # per-user session so a failure/aborted txn doesn't leak between users.
    async with AsyncSessionLocal() as enum_session:
        user_ids = await SkillCuratorService(enum_session).list_active_user_ids()
    for uid in user_ids:
        try:
            async with AsyncSessionLocal() as per_user_db:
                await SkillCuratorService(per_user_db).run_for_user(uid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Skill curator failed for user {uid}: {e}")
    return f"users={len(user_ids)}" if user_ids else None


async def _kg_reconciler_handler(app: "FastAPI", params: dict) -> str | None:
    if not settings.kg_reconciler_enabled:
        return "skipped: kg_reconciler_enabled is off"
    from services.database import AsyncSessionLocal
    from services.kg_reconciler_service import KgReconcilerService

    # Per-user; the advisory lock (ns 0x4B47) lives INSIDE run_for_user on its own
    # dedicated connection — the handler must NOT re-wrap it.
    async with AsyncSessionLocal() as enum_session:
        user_ids = await KgReconcilerService(enum_session).list_active_user_ids()
    for uid in user_ids:
        try:
            async with AsyncSessionLocal() as per_user_db:
                await KgReconcilerService(per_user_db).run_for_user(uid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"KG reconciler failed for user {uid}: {e}")
    return f"users={len(user_ids)}" if user_ids else None


async def _document_dedupe_handler(app: "FastAPI", params: dict) -> str | None:
    """Autonomous KB near-duplicate DOCUMENT scan (#1170 P3). Per-user; the
    advisory lock (ns 0x4444) lives INSIDE run_for_user on its own dedicated
    connection, so the handler must NOT re-wrap it. Self-gates on the runtime flag
    (M7) so a ConfigMap flip activates it without a UI toggle."""
    if not settings.document_dedupe_enabled:
        return "skipped: document_dedupe_enabled is off"
    from services.database import AsyncSessionLocal
    from services.document_dedupe_service import DocumentDedupeService

    async with AsyncSessionLocal() as enum_session:
        user_ids = await DocumentDedupeService(enum_session).list_owner_ids()
    proposed = 0
    for uid in user_ids:
        try:
            async with AsyncSessionLocal() as per_user_db:
                report = await DocumentDedupeService(per_user_db).run_for_user(uid)
                proposed += report.proposed
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Document dedupe failed for user {uid}: {e}")
    return f"users={len(user_ids)} proposed={proposed}" if user_ids else None


async def _low_coverage_reindex_handler(app: "FastAPI", params: dict) -> str | None:
    """Autonomous self-healing sweep: re-enqueue completed LOW-COVERAGE docs so
    the ingest-time VLM coverage trigger recovers them on re-processing. Self-gates
    on the runtime flag (M7). Drains the pre-fix backlog + future stragglers, then
    idles (only initial-ingest low-coverage docs match; a re-attempted-still-bad
    doc is classified 'attempted' and skipped → no re-OCR loop)."""
    if not settings.low_coverage_reindex_enabled:
        return "skipped: low_coverage_reindex_enabled is off"
    from services.kb_maintenance_tool import sweep_low_coverage_reindex

    report = await sweep_low_coverage_reindex(cap=settings.low_coverage_reindex_cap)
    if not report.get("enqueued") and not report.get("skipped_attempted"):
        return None
    return f"enqueued={report['enqueued']} skipped_attempted={report['skipped_attempted']}"


async def _skill_shadow_log_cleanup_handler(app: "FastAPI", params: dict) -> str | None:
    if not (settings.skills_enabled and settings.skill_shadow_log_enabled):
        return "skipped: skills_enabled AND skill_shadow_log_enabled required"
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete

    from models.database import SkillWouldHaveInjectedLog
    from services.database import AsyncSessionLocal

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=settings.skill_shadow_log_retention_days
    )
    async with AsyncSessionLocal() as db_session:
        result = await db_session.execute(
            delete(SkillWouldHaveInjectedLog).where(SkillWouldHaveInjectedLog.created_at < cutoff)
        )
        deleted = int(result.rowcount or 0)
        if deleted:
            await db_session.commit()
            logger.info(
                f"🧹 Skill shadow log: pruned {deleted} row(s) older than "
                f"{settings.skill_shadow_log_retention_days}d"
            )
    return f"pruned={deleted}" if deleted else None


async def _paperless_ui_edit_sweep_handler(app: "FastAPI", params: dict) -> str | None:
    # No enabled flag; needs mcp_manager (skips until it's wired).
    mgr = getattr(app.state, "mcp_manager", None)
    if mgr is None:
        return "skipped: mcp_manager not ready"
    from services.paperless_ui_edit_sweeper import run_sweep_tick

    await run_sweep_tick(mcp_manager=mgr)
    return None


async def _paperless_abandoned_confirm_sweep_handler(app: "FastAPI", params: dict) -> str | None:
    # No gate; DB-only (drop stale pending_confirms).
    from services.paperless_ui_edit_sweeper import run_abandoned_confirm_sweep

    await run_abandoned_confirm_sweep()
    return None


async def _placeholder_atom_reaper_handler(app: "FastAPI", params: dict) -> str | None:
    # No gate; DB-only. Reaps orphaned __pending__ placeholder atoms left by a
    # crash between the placeholder INSERT and finalize_source_id (#446). Only
    # rows older than older_than_seconds are reaped so an in-flight create is safe.
    from services.atom_service import reap_orphan_placeholder_atoms
    from services.database import AsyncSessionLocal

    older = int(params.get("older_than_seconds", 3600))
    async with AsyncSessionLocal() as db_session:
        n = await reap_orphan_placeholder_atoms(db_session, older_than_seconds=older)
    if n:
        logger.info(f"Placeholder-atom reaper: deleted {n} orphaned __pending__ atom(s)")
        return f"reaped={n}"
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
    # Phase 3 Batch B
    register_handler("notification_cleanup", _notification_cleanup_handler)
    register_handler("memory_cleanup", _memory_cleanup_handler)
    register_handler("meeting_retention", _meeting_retention_handler)
    register_handler("trajectory_cleanup", _trajectory_cleanup_handler)
    register_handler("kg_conflation_monitor", _kg_conflation_monitor_handler)
    register_handler("paperless_reconciler", _paperless_reconciler_handler)
    # Phase 3 Batch C
    register_handler("obligation_deadline_notifier", _obligation_deadline_notifier_handler)
    register_handler("obligation_digest", _obligation_digest_handler)
    register_handler("obligation_calendar_sync", _obligation_calendar_sync_handler)
    register_handler("speaker_vocab_rebuild", _speaker_vocab_rebuild_handler)
    register_handler("skill_curator", _skill_curator_handler)
    register_handler("kg_reconciler", _kg_reconciler_handler)
    register_handler("document_dedupe", _document_dedupe_handler)
    register_handler("low_coverage_reindex", _low_coverage_reindex_handler)
    register_handler("skill_shadow_log_cleanup", _skill_shadow_log_cleanup_handler)
    register_handler("paperless_ui_edit_sweep", _paperless_ui_edit_sweep_handler)
    register_handler("paperless_abandoned_confirm_sweep", _paperless_abandoned_confirm_sweep_handler)
    register_handler("placeholder_atom_reaper", _placeholder_atom_reaper_handler)


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
        # --- Phase 3 Batch B (seeded enabled + in-handler gate → flag controls at runtime) ---
        TaskSeed(name="Benachrichtigungen aufräumen", handler_key="notification_cleanup", interval_seconds=3600, enabled=True),
        TaskSeed(name="Gedächtnis aufräumen", handler_key="memory_cleanup", interval_seconds=settings.memory_cleanup_interval, enabled=True),
        TaskSeed(name="Meeting-Aufbewahrung", handler_key="meeting_retention", interval_seconds=86400, run_at_boot=True, enabled=True),
        TaskSeed(name="Trajektorien aufräumen", handler_key="trajectory_cleanup", interval_seconds=settings.trajectory_cleanup_interval, run_at_boot=True, enabled=True),
        TaskSeed(name="KG-Konflations-Monitor", handler_key="kg_conflation_monitor", interval_seconds=settings.kg_conflation_monitor_interval, run_at_boot=True, enabled=True),
        TaskSeed(name="Paperless-Ablage nachziehen", handler_key="paperless_reconciler", interval_seconds=settings.paperless_reconciler_interval, run_at_boot=True, enabled=True),
        # --- Phase 3 Batch C ---
        TaskSeed(name="Fristen-Benachrichtigung", handler_key="obligation_deadline_notifier", interval_seconds=settings.obligation_notifier_interval, run_at_boot=True, enabled=True),
        TaskSeed(name="Fristen-Wochenübersicht", handler_key="obligation_digest", interval_seconds=settings.obligation_digest_interval, run_at_boot=True, enabled=True),
        TaskSeed(name="Fristen-Kalender-Sync", handler_key="obligation_calendar_sync", interval_seconds=settings.obligation_calendar_sync_interval, run_at_boot=True, enabled=True),
        TaskSeed(name="Sprecher-Vokabular neu aufbauen", handler_key="speaker_vocab_rebuild", interval_seconds=settings.speaker_vocab_rebuild_interval_seconds, run_at_boot=True, enabled=True),
        TaskSeed(name="Fähigkeiten-Kurator", handler_key="skill_curator", interval_seconds=settings.skill_curator_interval, run_at_boot=True, enabled=True),
        TaskSeed(name="KG-Reconciler", handler_key="kg_reconciler", interval_seconds=settings.kg_reconciler_interval, run_at_boot=True, enabled=True),
        # Seeded enabled; the handler self-gates on document_dedupe_enabled (M7) so it's
        # inert until the flag is flipped, then the ConfigMap-flip activates it.
        TaskSeed(name="Dokument-Dedupe", handler_key="document_dedupe", interval_seconds=settings.document_dedupe_interval, run_at_boot=True, enabled=True),
        # Self-gates on low_coverage_reindex_enabled (M7): re-enqueues low-coverage
        # docs so the ingest-time VLM coverage trigger recovers them; drains then idles.
        TaskSeed(name="Low-Coverage-Docs neu indexieren", handler_key="low_coverage_reindex", interval_seconds=settings.low_coverage_reindex_interval, run_at_boot=True, enabled=True),
        TaskSeed(name="Skill-Schattenlog aufräumen", handler_key="skill_shadow_log_cleanup", interval_seconds=settings.skill_shadow_log_cleanup_interval, run_at_boot=True, enabled=True),
        TaskSeed(name="Paperless UI-Änderungen auswerten", handler_key="paperless_ui_edit_sweep", interval_seconds=3600, run_at_boot=True, enabled=True),
        TaskSeed(name="Paperless verwaiste Bestätigungen aufräumen", handler_key="paperless_abandoned_confirm_sweep", interval_seconds=3600, run_at_boot=True, enabled=True),
        # #446: reap orphaned __pending__ placeholder atoms left by a crash mid
        # create_with_source. DB-only, no runtime gate; the reaper's age floor
        # protects in-flight creates.
        TaskSeed(name="Verwaiste Platzhalter-Atoms aufräumen", handler_key="placeholder_atom_reaper", interval_seconds=settings.placeholder_atom_reaper_interval, run_at_boot=True, enabled=True),
    ]
