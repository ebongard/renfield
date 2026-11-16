"""
DocumentProcessingHistory service — audit trail of every ingestion attempt.

Owns reads/writes against ``document_processing_history``. The application
layer never writes to this table directly; it always goes through the
``track()`` async context manager so the open/close pattern stays uniform
across all ingestion call sites (RAGService.ingest_document,
RAGService.reindex_document, future async worker from #388, etc.).

State machine
=============

::

                ┌────────────┐
                │   (none)   │
                └─────┬──────┘
                      │ history.open(doc_id, force_ocr, trigger)
                      ▼
                ┌─────────────┐
   ┌────────────│ processing  │────────────┐
   │            └─────────────┘            │
   │ caller exits async with               │ exception inside async with
   │ → close_success(metrics)              │ → close_failure(error)
   │                                       │
   ▼                                       ▼
┌────────────┐                       ┌────────────┐
│ completed  │                       │   failed   │
└────────────┘                       └────────────┘

Zombie rows
===========

A process crash between ``open()`` and the context manager's close call
leaves ``status='processing'`` indefinitely. The (deferred) startup sweep
PR scans for these and re-enqueues affected docs. Until that ships,
``has_force_ocr_succeeded(doc_id)`` filters on ``status='completed'`` and
correctly ignores zombies — so the cleanup script is safe.

Transaction model
=================

History writes use SHORT-LIVED, SEPARATE transactions from the ingest body
they wrap. A history-write failure must NOT destroy a successful ingest —
the chunks are the real product, the audit is fuzzy. Cost of this choice:
zombie ``processing`` rows from crashes (see above).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import DocumentProcessingHistory
from utils.log_safety import error_text_without_content, frames_without_content

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Naive UTC datetime for ``finished_at`` writes. Set Python-side rather
    than via raw-SQL ``now()`` so sqlite-shimmed test runs work — sqlite
    has no ``now()`` function."""
    return datetime.now(UTC).replace(tzinfo=None)


class ProcessingStatus(str, Enum):
    """Mirrors the ``chk_dph_status`` CHECK constraint in pc20260530."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessingTrigger(str, Enum):
    """Mirrors the ``chk_dph_trigger`` CHECK constraint in pc20260530.

    Adding a value here requires a coordinated alembic migration that
    updates the CHECK constraint to include the new value.
    """
    INITIAL_INGEST = "initial_ingest"
    USER_REINDEX = "user_reindex"
    SCRIPT_PURGE = "script_purge"
    STARTUP_SWEEP = "startup_sweep"


@dataclass
class HistoryRow:
    """Handle yielded from ``track()``. The caller assigns metrics on this
    object; the context manager UPDATEs the row once on clean exit using
    whatever was assigned. Single-writer pattern — caller owns metrics,
    manager owns row state transitions."""
    hid: int
    chunks_produced: int | None = None
    chunks_dropped: int | None = None
    ocr_engine: str | None = None


class DocumentProcessingHistoryService:
    """
    Read/write helpers for ``document_processing_history``.

    The session this service binds to is the CALLER's session. Each method
    runs its own short-lived commit so a write failure does not roll back
    the caller's outer work. Callers must not pass a session that is
    inside their own transaction expecting atomicity with the history row.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Mutating
    # ------------------------------------------------------------------

    async def open(
        self,
        document_id: int,
        force_ocr: bool,
        trigger: ProcessingTrigger,
    ) -> int:
        """INSERT a fresh history row in ``processing`` state. Own transaction.

        ``initial_ingest`` is **idempotent** (the partial unique index
        ``uq_dph_initial_ingest_per_doc`` guarantees at most one per doc — the
        migration calls this "re-runnable"). A second attempt — e.g. the Redis
        Streams reclaim re-delivering a stale PEL entry after a worker restart —
        UPSERTs the existing row back to ``processing`` and returns its id
        instead of raising ``UniqueViolation``. Before this, that violation
        crashed ``_process_entry``, which never ACKed, so the entry stayed in the
        PEL and re-fired (and re-crashed) on every reclaim — a poison entry that
        also left the document stuck in ``processing``.
        """
        if trigger == ProcessingTrigger.INITIAL_INGEST:
            return await self._open_initial_ingest_idempotent(document_id, force_ocr)
        # Non-initial triggers (user_reindex / script_purge / startup_sweep)
        # have no unique index — many per doc are expected. Plain insert.
        row = DocumentProcessingHistory(
            document_id=document_id,
            status=ProcessingStatus.PROCESSING.value,
            force_ocr=force_ocr,
            trigger=trigger.value,
        )
        self.db.add(row)
        await self.db.flush()
        await self.db.commit()
        return row.id

    async def _open_initial_ingest_idempotent(
        self, document_id: int, force_ocr: bool,
    ) -> int:
        """UPSERT the single ``initial_ingest`` row for a document.

        Postgres path uses ``ON CONFLICT`` against the **partial unique index**
        by its predicate (it is an INDEX, not a CONSTRAINT, so
        ``ON CONFLICT ON CONSTRAINT`` does not apply — the index-inference form
        ``ON CONFLICT (col) WHERE <predicate>`` is required). The sqlite test
        harness gets a portable SELECT-then-write fallback (the worker is a
        single consumer, so the TOCTOU window the index would guard against does
        not arise on that path).
        """
        is_postgres = (
            self.db.bind is not None and self.db.bind.dialect.name == "postgresql"
        )
        if is_postgres:
            hid = (await self.db.execute(
                text(
                    """
                    INSERT INTO document_processing_history
                        (document_id, status, force_ocr, trigger, started_at, extra)
                    VALUES (:doc_id, 'processing', :force_ocr, 'initial_ingest', :now, '{}')
                    ON CONFLICT (document_id) WHERE (trigger = 'initial_ingest')
                    DO UPDATE SET
                        status = 'processing',
                        force_ocr = EXCLUDED.force_ocr,
                        started_at = EXCLUDED.started_at,
                        finished_at = NULL,
                        error_message = NULL
                    RETURNING id
                    """
                ),
                {"doc_id": document_id, "force_ocr": force_ocr, "now": _now()},
            )).scalar_one()
            await self.db.commit()
            return int(hid)

        existing = (await self.db.execute(
            select(DocumentProcessingHistory.id).where(
                DocumentProcessingHistory.document_id == document_id,
                DocumentProcessingHistory.trigger == ProcessingTrigger.INITIAL_INGEST.value,
            ).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            await self.db.execute(
                text(
                    """
                    UPDATE document_processing_history
                    SET status = 'processing', force_ocr = :force_ocr,
                        started_at = :now, finished_at = NULL, error_message = NULL
                    WHERE id = :id
                    """
                ),
                {"id": existing, "force_ocr": force_ocr, "now": _now()},
            )
            await self.db.commit()
            return int(existing)
        row = DocumentProcessingHistory(
            document_id=document_id,
            status=ProcessingStatus.PROCESSING.value,
            force_ocr=force_ocr,
            trigger=ProcessingTrigger.INITIAL_INGEST.value,
        )
        self.db.add(row)
        await self.db.flush()
        await self.db.commit()
        return row.id

    async def close_success(
        self,
        history_id: int,
        chunks_produced: int | None,
        chunks_dropped: int | None,
        ocr_engine: str | None,
    ) -> None:
        """UPDATE row to ``completed`` with the metrics the caller knows.
        All metrics are nullable: a caller that doesn't track one passes
        None (e.g. bulk-import worker may not track dropped chunks).
        Own transaction."""
        await self.db.execute(
            text(
                """
                UPDATE document_processing_history
                SET status = 'completed',
                    finished_at = :finished_at,
                    chunks_produced = :chunks_produced,
                    chunks_dropped_low_quality = :chunks_dropped,
                    ocr_engine = :ocr_engine
                WHERE id = :history_id
                """
            ),
            {
                "history_id": history_id,
                "finished_at": _now(),
                "chunks_produced": chunks_produced,
                "chunks_dropped": chunks_dropped,
                "ocr_engine": ocr_engine,
            },
        )
        await self.db.commit()

    async def close_failure(self, history_id: int, error_message: str) -> None:
        """UPDATE row to ``failed`` with the error message. Own transaction.

        Postgres ``text`` accepts arbitrary length; no truncation here.
        """
        await self.db.execute(
            text(
                """
                UPDATE document_processing_history
                SET status = 'failed',
                    finished_at = :finished_at,
                    error_message = :error_message
                WHERE id = :history_id
                """
            ),
            {
                "history_id": history_id,
                "finished_at": _now(),
                "error_message": error_message,
            },
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def has_force_ocr_succeeded(self, document_id: int) -> bool:
        """True iff at least one COMPLETED history row exists for this doc
        with ``force_ocr=true``. Used by the cleanup script's idempotence
        guard. Hits the partial index ``idx_dph_document_force_ocr_status``.
        """
        result = await self.db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM document_processing_history
                    WHERE document_id = :document_id
                      AND force_ocr = true
                      AND status = 'completed'
                )
                """
            ),
            {"document_id": document_id},
        )
        return bool(result.scalar())

    async def has_successful_gate_reprocess(self, document_id: int) -> bool:
        """True iff a COMPLETED ``script_purge`` history row exists for this doc
        with ``force_ocr=false`` — i.e. the cleanup script already reprocessed it
        in gate-decided (non-forced) mode.

        Default-mode idempotence guard for ``bin/purge_low_quality_chunks.py``:
        the first gate reprocess runs (recovering a doc an earlier *forced* run
        may have degraded), then subsequent runs skip it so repeated runs
        converge. Deliberately distinct from ``has_force_ocr_succeeded`` — old
        ``force_ocr=true`` rows do NOT count, so the recovery pass still
        reprocesses the previously force-OCR'd corpus exactly once. (``trigger``
        is quoted — it is a Postgres reserved word.)
        """
        result = await self.db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM document_processing_history
                    WHERE document_id = :document_id
                      AND force_ocr = false
                      AND "trigger" = 'script_purge'
                      AND status = 'completed'
                )
                """
            ),
            {"document_id": document_id},
        )
        return bool(result.scalar())

    async def latest(self, document_id: int) -> DocumentProcessingHistory | None:
        """Most-recent history row for a doc by ``started_at``. For admin UX
        (deferred item C)."""
        stmt = (
            select(DocumentProcessingHistory)
            .where(DocumentProcessingHistory.document_id == document_id)
            .order_by(DocumentProcessingHistory.started_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def initial_ingest_status(self, document_id: int) -> str | None:
        """Status of the doc's single ``initial_ingest`` row, or ``None`` if it
        has never been ingested.

        The document-worker uses this to be an idempotent consumer of the
        at-least-once Redis stream: ``completed`` → a re-delivered entry is a
        duplicate (drop it, don't re-ingest); ``processing``/``failed`` → an
        incomplete first ingest to retry (purge partial chunks first); ``None``
        → a genuine first ingest.
        """
        result = await self.db.execute(
            select(DocumentProcessingHistory.status)
            .where(
                DocumentProcessingHistory.document_id == document_id,
                DocumentProcessingHistory.trigger == ProcessingTrigger.INITIAL_INGEST.value,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Context manager (the seam every ingest path uses)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def track(
        self,
        document_id: int,
        force_ocr: bool,
        trigger: ProcessingTrigger,
    ) -> AsyncIterator[HistoryRow]:
        """Wrap an ingest body. Yields a ``HistoryRow`` whose metric fields
        the caller assigns before exiting the ``async with``. On clean exit
        the manager UPDATEs the row to ``completed`` once. On exception the
        manager UPDATEs to ``failed`` once and re-raises the original.

        Single-writer semantics: the manager owns row state transitions
        (open + close), the caller owns the metrics (chunks_produced,
        chunks_dropped, ocr_engine). There is no double-close race.

        If ``close_failure`` itself raises (most likely because the same
        DB outage that killed the ingest also blocks the audit write), the
        secondary exception is LOGGED and the ORIGINAL exception propagates
        via bare ``raise``. Diagnostics matter most in outage scenarios.
        """
        hid = await self.open(document_id, force_ocr, trigger)
        row = HistoryRow(hid=hid)
        try:
            yield row
        except Exception as exc:
            try:
                await self.close_failure(hid, str(exc))
            except Exception as close_exc:
                logger.error(
                    "Failed to write history close_failure for hid=%s (doc_id=%s): %s: %s | at %s",
                    hid, document_id, type(close_exc).__name__,
                    error_text_without_content(close_exc), frames_without_content(close_exc),
                )
            raise
        else:
            await self.close_success(
                hid,
                chunks_produced=row.chunks_produced,
                chunks_dropped=row.chunks_dropped,
                ocr_engine=row.ocr_engine,
            )
