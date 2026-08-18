"""PDF-split slow lane (PR3 of docs/design/pdf-split.md).

The job function the dedicated pdf-split worker runs for a document the inline
pre-stage routed here: bad scans whose garbage pages need per-page VLM
transcription, and text-layer files whose signals exceed one boundary-LLM
window. Unbounded duration by design — the worker's row heartbeat
(``documents.split_heartbeat_at``) keeps the claim alive, and there is
deliberately NO page cap (cost is bounded by the per-call VLM timeout and this
lane's isolation, never by skipping pages).

Outcomes mirror the inline pre-stage via the shared ``act_on_verdict``:
confident → split executed (persisted plan, full crash-safe machinery),
uncertain → owner review proposal, single → the document is HANDED BACK to the
normal ingest pipeline (re-enqueued on the document queue with the
``skip_split`` loop-breaker) — a slow-lane file is never lost, worst case it
ingests as one document exactly like pre-PR3.
"""
from __future__ import annotations

import asyncio

from loguru import logger

from models.database import (
    DOC_STATUS_PENDING,
    DOC_STATUS_SPLIT_ARCHIVED,
    DOC_STATUS_SPLIT_REVIEW,
    Document,
)
from services.database import AsyncSessionLocal
from services.pdf_split_detector import (
    detect_boundaries,
    extract_page_signals,
    vlm_fill_signals,
)
from services.pdf_splitter import (
    _load_stored_plan,
    _rejection_recorded,
    act_on_verdict,
    execute_split,
)
from services.redis_client import get_redis
from services.task_queue import DocumentTaskQueue


async def _hand_back_single(db, doc: Document, user_id: int | None) -> None:
    """Return the document to the normal ingest pipeline as ONE document.
    ``skip_split`` because this lane already decided (with the best available
    evidence) that splitting is not warranted — the inline pre-stage must not
    re-detect and bounce it back here."""
    doc.status = DOC_STATUS_PENDING
    await db.commit()
    await DocumentTaskQueue(redis_client=get_redis()).enqueue(
        {
            "document_id": doc.id,
            "force_ocr": False,
            "user_id": user_id,
            "skip_split": True,
        }
    )


async def process_slow_split(document_id: int, user_id: int | None) -> str:
    """Run slow-lane detection + resolution for one parked document.

    Returns the outcome for the worker's logging: ``split`` / ``review`` /
    ``single`` / ``skip`` (nothing to do — resolved elsewhere). Transient
    failures (LLM host down, DB blips, RETRY-class child ingests) raise and
    leave the entry in the PEL; terminal split-execution failures raise for
    the worker's failed-marking."""
    async with AsyncSessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            return "skip"
        if doc.status in (DOC_STATUS_SPLIT_ARCHIVED, DOC_STATUS_SPLIT_REVIEW):
            # Already split, or the review flow owns it (an approve/reject
            # re-drives via its own enqueue).
            return "skip"

        # Resume/approve replay: a persisted plan always wins (never re-detect).
        plan_row, stored = await _load_stored_plan(db, doc.id)
        if stored is not None:
            await execute_split(db, doc, stored, user_id=user_id, plan_row=plan_row)
            return "split"

        # Durable owner decision: rejected = treat-as-single, forever.
        if await _rejection_recorded(db, doc.id):
            await _hand_back_single(db, doc, user_id)
            return "single"

        loop = asyncio.get_running_loop()
        signals = await loop.run_in_executor(
            None, extract_page_signals, doc.file_path
        )
        if not signals:
            logger.warning(
                f"pdf-split[slow]: no page signals for doc {doc.id} — handing "
                f"back as a single document"
            )
            await _hand_back_single(db, doc, user_id)
            return "single"

        signals, filled = await vlm_fill_signals(doc.file_path, signals)
        garbage_left = sum(1 for s in signals if not s.quality_ok)
        logger.info(
            f"pdf-split[slow]: doc {doc.id} — VLM transcribed {filled} page(s), "
            f"{garbage_left} still unreadable"
        )

        verdict = await detect_boundaries(signals)
        outcome = await act_on_verdict(db, doc, verdict, user_id)
        if outcome == "single":
            await _hand_back_single(db, doc, user_id)
        return outcome
