"""Restart-safe backstop for the async Paperless commit finalize (#658).

The interactive Paperless commit uploads async and finishes in a fire-and-forget
background task (``paperless_commit_tool._finalize_paperless_commit``): poll the
consume task → apply the deferred metadata PATCH (created_date/storage_path/
custom_fields) → write the PaperlessUploadTracking row. If the consume outlives
the poll window OR the pod restarts mid-poll, that in-memory task is lost and the
PATCH/tracking silently never land.

``paperless_commit_tool`` now persists the finalize intent as a
``paperless_pending_finalize`` row BEFORE spawning the task. This periodic scan
re-runs the finalize for rows still ``finalized_at IS NULL`` past a grace window
(so it doesn't race the live task), leasing each row so overlapping ticks / the
live task don't double-run it. ``_finalize_paperless_commit`` is idempotent (it
skips a tracking row that already exists and stamps ``finalized_at`` on a terminal
outcome), so a re-run that races a just-finished live task is safe. A still-
consuming ("pending") pass refunds its attempt so a healthy-but-slow Paperless
OCR backlog isn't abandoned with unapplied metadata; a real error burns one.
After ``max_attempts`` errors OR a wall-clock backstop (``giveup_hours``, for a
forever-pending row) a stuck row is closed and logged loudly (never silently
dropped). A DB-level UNIQUE on paperless_upload_tracking.chat_upload_id is the
concurrency backstop under the read-check idempotency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select

from models.database import PaperlessPendingFinalize
from services.redis_client import get_redis
from utils.config import settings

# One re-finalize in flight per row (SET NX EX) so a slow re-run isn't re-picked
# every tick and the live task + reconciler don't collide.
_FINALIZE_LEASE_KEY = "paperless:finalize:lease:{id}"


def _naive_utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def reconcile_pending_finalizes(mcp_manager: Any = None) -> None:
    """Re-run lost Paperless-commit finalizes. Needs a live ``mcp_manager`` (the
    deferred PATCH goes through ``mcp.paperless.update_document``); a None manager
    (MCP unavailable) is a no-op — the rows persist for the next tick."""
    if mcp_manager is None:
        return

    # Imported in-function (not at module top) so a test's monkeypatch of
    # services.database.AsyncSessionLocal is picked up at call time — the same
    # patchable pattern the route/commit paths use.
    from services.database import AsyncSessionLocal

    grace = timedelta(seconds=settings.paperless_finalize_reconciler_grace_seconds)
    now = _naive_utcnow()
    cutoff = now - grace
    giveup_cutoff = now - timedelta(
        hours=settings.paperless_finalize_reconciler_giveup_hours
    )
    batch = settings.paperless_finalize_reconciler_batch
    max_attempts = settings.paperless_finalize_reconciler_max_attempts

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(PaperlessPendingFinalize)
                .where(
                    PaperlessPendingFinalize.finalized_at.is_(None),
                    PaperlessPendingFinalize.created_at < cutoff,
                )
                .order_by(PaperlessPendingFinalize.created_at)
                .limit(batch)
            )
        ).scalars().all()
    if not rows:
        return

    # Snapshot the fields we need OUTSIDE the session (the objects detach on exit).
    pending = [
        {
            "id": r.id,
            "task_id": r.task_id,
            "chat_upload_id": r.chat_upload_id,
            "user_id": r.user_id,
            "session_id": r.session_id,
            "filename": r.filename,
            "deferred_patch": r.deferred_patch or {},
            "original_metadata": r.original_metadata or {},
            "created_note": r.created_note or "",
            "doc_text": r.doc_text,
            "attempts": r.attempts or 0,
            "created_at": r.created_at,
        }
        for r in rows
    ]

    redis = get_redis()
    lease_ttl = settings.paperless_finalize_reconciler_lease_seconds
    poll_timeout = settings.paperless_finalize_reconciler_poll_seconds

    from services.paperless_commit_tool import _finalize_paperless_commit

    reran = 0
    for row in pending:
        acquired = await redis.set(
            _FINALIZE_LEASE_KEY.format(id=row["id"]), "1", nx=True, ex=lease_ttl
        )
        if not acquired:
            continue

        # Give up when the ERROR budget is spent OR the row has been unfinalized
        # too long (the wall-clock backstop bounds a forever-"pending" row whose
        # attempts keep getting refunded below). Close it + log loudly — the
        # deferred PATCH may be unapplied; surface it, never retry forever or
        # drop it silently.
        too_old = row["created_at"] is not None and row["created_at"] < giveup_cutoff
        if row["attempts"] >= max_attempts or too_old:
            async with AsyncSessionLocal() as db2:
                pf = await db2.get(PaperlessPendingFinalize, row["id"])
                if pf is not None and pf.finalized_at is None:
                    pf.finalized_at = _naive_utcnow()
                    await db2.commit()
            logger.error(
                "paperless-finalize-reconciler: GIVING UP on finalize row {} "
                "(task={}, upload={}, file={!r}) after {} attempts ({}) — deferred "
                "metadata PATCH may be unapplied; check the Paperless document.",
                row["id"], row["task_id"], row["chat_upload_id"], row["filename"],
                row["attempts"], "aged out" if too_old else "max attempts",
            )
            continue

        # Count the attempt BEFORE re-running so a crash mid-finalize still burns
        # one (bounded retries even across pod restarts).
        async with AsyncSessionLocal() as db3:
            pf = await db3.get(PaperlessPendingFinalize, row["id"])
            if pf is None or pf.finalized_at is not None:
                continue  # a concurrent run finished it
            pf.attempts = (pf.attempts or 0) + 1
            await db3.commit()

        status: str | None = None
        try:
            status = await _finalize_paperless_commit(
                task_id=row["task_id"],
                deferred_patch=row["deferred_patch"],
                user_approved=row["original_metadata"],
                attachment_id=row["chat_upload_id"],
                user_id=row["user_id"],
                session_id=row["session_id"],
                filename=row["filename"],
                created_note=row["created_note"],
                doc_text=row["doc_text"],
                mcp_manager=mcp_manager,
                pending_finalize_id=row["id"],
                poll_timeout_s=poll_timeout,
                # The live task already announced a still-"pending" consume once;
                # a reconciler re-run must not re-persist that message/push every
                # tick (only a terminal completed/failed outcome re-announces).
                announce_pending=False,
            )
            reran += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "paperless-finalize-reconciler: re-finalize row {} failed: {}",
                row["id"], exc,
            )

        # Refund the attempt when the consume is merely still in progress — a
        # healthy-but-slow Paperless OCR backlog is not a failure and must not
        # burn the bounded error budget (the wall-clock backstop still bounds it).
        # A crash mid-finalize never reaches here, so the pre-counted attempt
        # stays burned (crash-safety preserved).
        if status == "pending":
            async with AsyncSessionLocal() as db4:
                pf = await db4.get(PaperlessPendingFinalize, row["id"])
                if pf is not None and pf.finalized_at is None and (pf.attempts or 0) > 0:
                    pf.attempts -= 1
                    await db4.commit()

    if reran:
        logger.info(
            "paperless-finalize-reconciler: re-ran {} lost finalize(s) "
            "(grace={:.0f}s, batch={})",
            reran, grace.total_seconds(), batch,
        )
