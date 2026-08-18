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
    DOC_STATUS_SPLIT_PENDING,
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
    evidence) that splitting is not warranted.

    Enqueue-failure safety: the status flip commits first (the doc must not
    look split-parked while its queue entry exists), but if the enqueue then
    fails the doc would strand in 'pending' (the worker's claim guard treats
    non-split_pending as resolved-elsewhere and ACKs). So a failed enqueue
    REVERTS the park state and raises transient — the PEL retry redoes the
    whole hand-back."""
    doc.status = DOC_STATUS_PENDING
    doc.split_heartbeat_at = None
    await db.commit()
    try:
        await DocumentTaskQueue(redis_client=get_redis()).enqueue(
            {
                "document_id": doc.id,
                "force_ocr": False,
                "user_id": user_id,
                "skip_split": True,
            }
        )
    except Exception as e:
        try:
            doc.status = DOC_STATUS_SPLIT_PENDING
            await db.commit()
        except Exception as re_e:  # noqa: BLE001 - revert is best-effort
            logger.error(
                f"pdf-split[slow]: hand-back revert failed for doc {doc.id}: {re_e}"
            )
        from services.pdf_split_errors import SplitTransientError

        raise SplitTransientError(
            f"hand-back enqueue failed for doc {doc.id}: {e}"
        ) from e


async def process_slow_split(document_id: int, user_id: int | None) -> str:
    """Run slow-lane detection + resolution for one parked document.

    Returns the outcome for the worker's logging: ``split`` / ``review`` /
    ``single`` / ``skip`` (nothing to do — resolved elsewhere). Transient
    failures (LLM/vision host down, DB blips, RETRY-class child ingests,
    failed hand-back enqueues) raise and leave the entry in the PEL; terminal
    split-execution failures raise for the worker's failed-marking.

    The DB session is NOT held across the unbounded VLM/boundary work — a
    pooled connection idle-in-transaction for an hours-long transcription is
    the exact shape of the 2026-07-01 pool-exhaustion outage. Phase 1 (cheap
    reads + early exits) and phase 2 (act on the verdict) each use their own
    short-lived session; phase 2 re-guards the doc state."""
    # -- Phase 1: cheap reads + early exits (own short session) --
    async with AsyncSessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            return "skip"
        if doc.status in (DOC_STATUS_SPLIT_ARCHIVED, DOC_STATUS_SPLIT_REVIEW):
            # Already split, or the review flow owns it (an approve/reject
            # re-drives via its own enqueue).
            return "skip"

        # Resume/approve replay: a persisted plan always wins (never re-detect).
        # execute_split is DB-bound work — running it inside this session is
        # fine (its slow parts are per-piece, not one long transaction).
        plan_row, stored = await _load_stored_plan(db, doc.id)
        if stored is not None:
            await execute_split(db, doc, stored, user_id=user_id, plan_row=plan_row)
            return "split"

        # Durable owner decision: rejected = treat-as-single, forever.
        if await _rejection_recorded(db, doc.id):
            await _hand_back_single(db, doc, user_id)
            return "single"

        file_path = doc.file_path

    # -- Unbounded work: NO session held --
    loop = asyncio.get_running_loop()
    signals = await loop.run_in_executor(None, extract_page_signals, file_path)
    verdict = None
    if signals:
        garbage_before = sum(1 for s in signals if not s.quality_ok)
        signals, filled = await vlm_fill_signals(file_path, signals)
        garbage_left = sum(1 for s in signals if not s.quality_ok)
        logger.info(
            f"pdf-split[slow]: doc {document_id} — VLM transcribed {filled} "
            f"page(s), {garbage_left} still unreadable"
        )
        if garbage_before > 0 and filled == 0:
            # A wholesale VLM outage is indistinguishable per page from
            # 'unreadable' (extract_text_from_image swallows transport errors
            # into None). Zero successes across ALL garbage pages is the
            # outage signature — deciding boundaries over pure placeholders
            # would permanently ingest a multi-doc scan as ONE document, so
            # retry instead (the worker's transient cap bounds this and its
            # fail-safe is the single hand-back anyway).
            from services.pdf_split_errors import SplitTransientError

            raise SplitTransientError(
                f"VLM transcribed 0 of {garbage_before} unreadable pages for "
                f"doc {document_id} — vision host down or model missing"
            )
        verdict = await detect_boundaries(signals)

    # -- Phase 2: act on the verdict (fresh session, re-guarded) --
    async with AsyncSessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            return "skip"
        if doc.status != DOC_STATUS_SPLIT_PENDING:
            # Resolved elsewhere while we worked (review filed by a racing
            # path, un-parked, archived) — do not override.
            return "skip"
        if not signals:
            logger.warning(
                f"pdf-split[slow]: no page signals for doc {doc.id} — handing "
                f"back as a single document"
            )
            await _hand_back_single(db, doc, user_id)
            return "single"
        outcome = await act_on_verdict(db, doc, verdict, user_id)
        if outcome == "single":
            await _hand_back_single(db, doc, user_id)
        return outcome
