"""
Document-processor worker (#388).

Consumes tasks from the ``renfield:tasks:document`` Redis Stream and runs
the Docling/EasyOCR/embedding pipeline out-of-process from the backend.

Entry point::

    python -m workers.document_processor_worker

Design constraints:

- Must NOT import ``main`` or instantiate the FastAPI app. Test
  ``test_worker_module_isolation`` asserts this; adding such an import
  would pull the entire lifecycle (MCP clients connecting to 10 servers,
  Whisper download, Speechbrain, …) into the worker pod and defeat the
  memory budget that motivated the split.
- Graceful shutdown on SIGTERM/SIGINT: finish the current task, ack it,
  then exit. Kubernetes gives ~30 s grace-period; an in-flight OCR on a
  large PDF may exceed that, in which case Kubernetes escalates to
  SIGKILL. The Streams PEL + reclaim_stale on next boot recovers the task.
- Heartbeat key ``renfield:worker:document:heartbeat`` lets the API
  short-circuit enqueue when no worker is alive. See
  ``_worker_is_alive`` in ``api/routes/knowledge.py``.

Consumes all uploads enqueued by ``/api/knowledge/upload``. The
synchronous chat-upload path still runs Docling in the API pod —
separate lifecycle, lower per-request memory footprint.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time

import httpx
import redis.asyncio as aioredis
from loguru import logger
from redis import exceptions as redis_exceptions
from sqlalchemy import delete, text
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError

from models.database import (
    DOC_STATUS_FAILED,
    DOC_STATUS_SPLIT_ARCHIVED,
    DOC_STATUS_SPLIT_PENDING,
    DOC_STATUS_SPLIT_REVIEW,
    Document,
    DocumentChunk,
)
from services.database import AsyncSessionLocal
from services.pdf_split_errors import SplitTransientError

try:  # ollama is in the worker's import graph (rag embedding client); guard
    from ollama import ResponseError as _OllamaResponseError  # packaging drift
except Exception:  # pragma: no cover - defensive
    _OllamaResponseError = None
from services.document_processing_history import (
    DocumentProcessingHistoryService,
    ProcessingStatus,
)
from services.progress import DocumentProgress
from services.rag_service import RAGService
from services.task_queue import (
    _REDIS_SOCKET_TIMEOUT_S,
    DocumentTaskQueue,
    StreamEntry,
)
from utils.config import settings

HEARTBEAT_KEY = "renfield:worker:document:heartbeat"
HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_TTL_S = 90


def _pod_name() -> str:
    """Identify this worker instance. In k8s the env var ``POD_NAME`` is set
    via downward API; outside k8s we fall back to hostname for dev runs."""
    return os.environ.get("POD_NAME") or os.environ.get("HOSTNAME", "worker-local")


# Exceptions that mean "the infrastructure blinked" — the LLM/embedding host is
# down, the DB connection dropped, Redis is unreachable. These are RETRYABLE: we
# leave the entry un-ACKed so reclaim_stale re-delivers it on the next boot.
# Everything ELSE reaching the worker is treated as TERMINAL (a poison document
# / genuine bug) — re-running it would just fail again and pile the entry up in
# the PEL forever, so we mark the row failed and ACK it. The folder-ingest D2
# matrix then lets a re-push REINGEST a failed row, and a manual reindex still
# works; both beat an unbounded retry of a doc that can never succeed.
_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,  # embedding timeout (rag_service wraps embeds in wait_for)
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,  # Ollama dropped the connection mid-stream
    redis_exceptions.ConnectionError,
    redis_exceptions.TimeoutError,
    OperationalError,  # DB connection lost / server unavailable
    InterfaceError,
    DisconnectionError,
    # PDF-split retryable outcomes (LLM host down mid-detection, disk-full /
    # lost-race child ingest): the split resume is idempotent, so a PEL retry
    # continues where the last run stopped. Plain SplitExecutionError (its
    # parent class) stays TERMINAL.
    SplitTransientError,
)


def _is_transient_error(exc: BaseException) -> bool:
    """True for infra blips that warrant a PEL retry (see _TRANSIENT_EXC)."""
    if isinstance(exc, _TRANSIENT_EXC):
        return True
    # A reachable-but-degraded Ollama raises ollama.ResponseError: a 5xx (503
    # model loading, 502/504 gateway) is retryable; a 4xx (bad request / model
    # not found) is a terminal config/data error. Host-down arrives earlier as
    # httpx.ConnectError, already covered above.
    if _OllamaResponseError is not None and isinstance(exc, _OllamaResponseError):
        return getattr(exc, "status_code", 0) >= 500
    return False


async def _mark_document_failed(doc_id, error: BaseException) -> bool:
    """Persist ``status=failed`` for a terminally-failed doc in a FRESH session.
    process_existing_document already marks failed on its stage-2/3 + Docling
    paths, but an error from the reindex / status-probe / chunk-purge steps (or
    a half-rolled-back session) might not have — this guarantees the row
    reflects the terminal failure so the UI and the D2 REINGEST branch see it.

    Returns True when the row's terminal state is recorded (committed, already
    failed, or the row is gone — all stable to ACK), False when it could NOT be
    persisted so the caller should leave the entry in the PEL for reclaim rather
    than ACK away a failure it failed to record."""
    try:
        async with AsyncSessionLocal() as db:
            doc = await db.get(Document, doc_id)
            if doc is None:
                return True  # row vanished — nothing to retry, safe to ack
            if doc.status != DOC_STATUS_FAILED:
                doc.status = DOC_STATUS_FAILED
                doc.error_message = str(error)[:2000]
                await db.commit()
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"could not mark doc {doc_id} failed: {e}")
        return False


async def _heartbeat_loop(
    redis: aioredis.Redis,
    stop_event: asyncio.Event,
    consumer_id: str,
) -> None:
    """Refresh the liveness key while the worker is up."""
    while not stop_event.is_set():
        try:
            await redis.set(HEARTBEAT_KEY, consumer_id, ex=HEARTBEAT_TTL_S)
        except Exception as e:
            logger.warning(f"heartbeat write failed: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_S)
        except asyncio.TimeoutError:
            continue


def _transient_key(entry_id: str) -> str:
    """Redis key holding the count of CLEAN transient leaves for a PEL entry.
    Subtracted from the redelivery count so infra blips (which the worker catches
    and leaves un-ACKed on purpose) don't burn the OOM-poison budget — only
    genuine mid-processing crashes (OOM-kills, which can't record a leave) do."""
    return f"renfield:tasks:transient:{entry_id}"


async def _clear_transient(redis: aioredis.Redis, entry_id: str) -> None:
    """Drop the transient-leave counter once the entry reaches a terminal state."""
    try:
        await redis.delete(_transient_key(entry_id))
    except Exception as e:  # noqa: BLE001 - cleanup is best-effort
        logger.debug(f"transient-counter cleanup failed for {entry_id}: {e}")


async def _process_entry(
    redis: aioredis.Redis,
    queue: DocumentTaskQueue,
    entry: StreamEntry,
) -> None:
    """Handle a single stream entry. On success the entry is XACKed; on
    exception we leave it in the PEL so the next reclaim picks it up."""
    doc_id = entry.params.get("document_id")
    if doc_id is None:
        logger.error(f"skipping entry {entry.entry_id}: missing document_id")
        await queue.ack(entry.entry_id)
        return

    # OOM-poison guard: an entry redelivered past the cap means the previous
    # consumer(s) kept dying mid-processing (e.g. an OOMKill on a huge OCR).
    # With periodic reclaim now re-adopting orphaned entries, re-processing such
    # a doc every time would crashloop the queue — so quarantine it (mark the doc
    # failed, ACK the entry). delivery_count is 1 for a fresh read; only a
    # repeatedly-reclaimed entry exceeds the cap.
    delivery_count = getattr(entry, "delivery_count", 1)
    if delivery_count > 1:
        # Subtract CLEAN transient leaves (infra blips the worker caught and left
        # un-ACKed) from the redelivery count — periodic reclaim re-delivers those
        # too, and a sustained embedding/DB outage would otherwise burn the poison
        # budget and wrongly fail healthy docs. Only genuine crash redeliveries
        # (an OOM-kill can't record a leave) should count.
        try:
            transient_leaves = int(await redis.get(_transient_key(entry.entry_id)) or 0)
        except Exception:  # noqa: BLE001 - a redis hiccup must not misfire the guard
            transient_leaves = 0
        crash_count = delivery_count - transient_leaves
        if crash_count > settings.worker_max_deliveries:
            logger.error(
                f"doc {doc_id}: entry {entry.entry_id} crash-redelivered {crash_count}x "
                f"(delivered {delivery_count}, transient {transient_leaves}; "
                f"> {settings.worker_max_deliveries}) — quarantining as failed "
                f"(likely OOM-poison / unprocessable)"
            )
            await _mark_document_failed(
                doc_id,
                RuntimeError(
                    f"quarantined after {crash_count} crash redeliveries "
                    f"(worker kept dying mid-processing; likely too large to process)"
                ),
            )
            await queue.ack(entry.entry_id)
            await _clear_transient(redis, entry.entry_id)
            return

    force_ocr = bool(entry.params.get("force_ocr", False))
    user_id = entry.params.get("user_id")
    # trigger distinguishes an initial upload from an async user-reindex
    # (#async-reindex). Absent → initial_ingest (back-compat with older entries).
    trigger = str(entry.params.get("trigger") or "initial_ingest")
    progress = DocumentProgress(redis, doc_id)

    if trigger == "paperless_refile":
        # Retry path: file a still-pending doc into Paperless WITHOUT re-running
        # the KB pipeline. The Docling OCR + Paperless MCP work runs here in the
        # worker (its home) — the backend only enqueues these. Best-effort; ack
        # regardless (a leg failure leaves the doc pending for the next re-enqueue).
        try:
            from services.paperless_filing_hook import refile_document_paperless

            await refile_document_paperless(doc_id, user_id=user_id)
        except Exception as e:  # noqa: BLE001 - never let a refile crash the loop
            logger.warning(f"paperless_refile for doc {doc_id} failed: {e}")
        await queue.ack(entry.entry_id)
        logger.info(f"paperless_refile doc {doc_id} (entry {entry.entry_id})")
        return

    try:
        async with AsyncSessionLocal() as db:
            rag = RAGService(db)

            # Flag-INDEPENDENT split-lifecycle guard: a doc in a split-owned
            # status must never enter normal processing or a reindex —
            # rebuilding chunks for an archived combined original would
            # resurrect it in retrieval next to its children. Deliberately
            # outside the pdf_split_enabled gate so a flag-off incident
            # rollback can't cause exactly that on a redelivered entry.
            doc_status = (
                await db.execute(
                    text("SELECT status FROM documents WHERE id = :id"),
                    {"id": doc_id},
                )
            ).scalar_one_or_none()
            if doc_status in (DOC_STATUS_SPLIT_ARCHIVED, DOC_STATUS_SPLIT_REVIEW):
                # Parked states (split done / awaiting owner review): drop the
                # entry.
                await queue.ack(entry.entry_id)
                await _clear_transient(redis, entry.entry_id)
                logger.info(
                    f"doc {doc_id}: status {doc_status!r} is owned by the "
                    f"pdf-split lifecycle — acked without processing "
                    f"(entry {entry.entry_id}, trigger {trigger})"
                )
                return
            if doc_status == DOC_STATUS_SPLIT_PENDING:
                # MID-SPLIT (children may already exist). With the flag on,
                # the pre-stage below resumes the persisted plan. With the
                # flag off (incident rollback), PARK the entry in the PEL —
                # never normal-ingest the combined parent, never drop the
                # entry (re-enabling the flag lets the next reclaim resume).
                # Recorded as a clean transient leave so redeliveries don't
                # burn the OOM-poison budget.
                if trigger == "user_reindex" or not settings.pdf_split_enabled:
                    if trigger == "user_reindex":
                        # A reindex must not resume/replay a split; drop it.
                        await queue.ack(entry.entry_id)
                        logger.info(
                            f"doc {doc_id}: mid-split — user_reindex refused "
                            f"(entry {entry.entry_id})"
                        )
                        return
                    try:
                        tkey = _transient_key(entry.entry_id)
                        await redis.incr(tkey)
                        await redis.expire(tkey, 86_400)
                    except Exception as ie:  # noqa: BLE001 - best-effort
                        logger.debug(
                            f"transient-counter incr failed for {entry.entry_id}: {ie}"
                        )
                    logger.warning(
                        f"doc {doc_id}: MID-SPLIT but PDF_SPLIT_ENABLED is "
                        f"off — parking entry {entry.entry_id} in the PEL "
                        f"(re-enable the flag to resume the split; the "
                        f"combined parent is NOT normally ingested)"
                    )
                    return  # no ack — reclaim redelivers

            if trigger == "user_reindex":
                # Async reindex: ALWAYS reprocess. reindex_document purges the
                # doc's chunks then rebuilds (records a user_reindex history row,
                # which has no unique index). The worker is a single consumer, so
                # concurrent reindex requests for one doc are serialized here —
                # the overlap that duplicated chunks on the old inline path
                # cannot happen.
                await rag.reindex_document(
                    doc_id, force_ocr=force_ocr, user_id=user_id,
                )
                await queue.ack(entry.entry_id)
                logger.info(f"reindexed doc {doc_id} (entry {entry.entry_id})")
                return

            history = DocumentProcessingHistoryService(db)

            # Idempotent consumer. The stream is at-least-once: reclaim_stale
            # re-delivers stale PEL entries on restart, including entries whose
            # ingest already SUCCEEDED but was SIGKILLed before the ack below.
            # Branch on the doc's initial-ingest state so a re-delivery never
            # double-ingests (process_existing_document APPENDS chunks + re-fires
            # the KG/Schicht-A hooks — it does not purge first).
            ingest_status = await history.initial_ingest_status(doc_id)
            if ingest_status == ProcessingStatus.COMPLETED.value:
                # Already fully ingested → duplicate delivery. Do NOT reprocess
                # (would append duplicate chunks + duplicate KG entities). Drop
                # it, and self-heal a doc left stuck in 'processing' by an
                # earlier failed re-attempt (the stuck-doc bug this guard fixes).
                await db.execute(
                    text(
                        "UPDATE documents SET status = 'completed' "
                        "WHERE id = :id AND status <> 'completed'"
                    ),
                    {"id": doc_id},
                )
                await db.commit()
                await queue.ack(entry.entry_id)
                logger.info(
                    f"doc {doc_id}: initial ingest already completed — duplicate "
                    f"delivery, acked (entry {entry.entry_id})"
                )
                return
            # PDF-split pre-stage (dark unless PDF_SPLIT_ENABLED): decide
            # whether this PDF is really several stapled documents BEFORE any
            # Docling work. True → the split lifecycle owns the doc (children
            # were created + enqueued through the normal bridge, the combined
            # original is archived) — ack and stop. Detection errors degrade
            # to False inside; an execution error propagates to the generic
            # transient/terminal handling below (execute_split resumes
            # idempotently on redelivery). Lazy import keeps the flag-off
            # path free of the detector's import graph.
            if settings.pdf_split_enabled:
                from services.pdf_splitter import maybe_split_at_ingest

                if await maybe_split_at_ingest(
                    db,
                    doc_id,
                    skip_split=bool(entry.params.get("skip_split", False)),
                    user_id=user_id,
                ):
                    await queue.ack(entry.entry_id)
                    await _clear_transient(redis, entry.entry_id)
                    logger.info(
                        f"doc {doc_id}: handled by pdf-split "
                        f"(entry {entry.entry_id})"
                    )
                    return

            if ingest_status in (
                ProcessingStatus.PROCESSING.value,
                ProcessingStatus.FAILED.value,
            ):
                # Incomplete first ingest being retried — purge any partial
                # chunks (mirrors reindex_document) so the rebuild is idempotent.
                await db.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == doc_id)
                )
                await db.commit()
                logger.info(
                    f"doc {doc_id}: retrying incomplete ingest — purged partial chunks"
                )

            await rag.process_existing_document(
                document_id=doc_id,
                force_ocr=force_ocr,
                user_id=user_id,
                progress=progress,
            )
        await queue.ack(entry.entry_id)
        await _clear_transient(redis, entry.entry_id)
        logger.info(f"processed doc {doc_id} (entry {entry.entry_id})")
    except Exception as e:
        logger.exception(f"task {entry.entry_id} for doc {doc_id} failed: {e}")
        if _is_transient_error(e):
            # Infra blip (LLM/embedding host down, DB/Redis dropped). Leave the
            # entry un-ACKed so reclaim_stale re-delivers it. Record this CLEAN
            # leave so the redelivery isn't counted as a processing crash by the
            # poison guard (an OOM-kill can't record one, which is the distinction).
            try:
                tkey = _transient_key(entry.entry_id)
                await redis.incr(tkey)
                await redis.expire(tkey, 86_400)
            except Exception as ie:  # noqa: BLE001 - best-effort
                logger.debug(f"transient-counter incr failed for {entry.entry_id}: {ie}")
            logger.warning(
                f"task {entry.entry_id} for doc {doc_id}: transient "
                f"{type(e).__name__} — leaving in PEL for reclaim"
            )
        else:
            # Terminal/poison doc: mark failed + ACK so it stops piling up in the
            # PEL and re-failing every restart. D2 REINGEST / manual reindex can
            # still retry the row deliberately. Only ACK once the failed state is
            # actually recorded — if we couldn't persist it (e.g. DB blip while
            # marking), leave the entry in the PEL so reclaim retries rather than
            # silently dropping a doc whose terminal status we never wrote.
            if await _mark_document_failed(doc_id, e):
                await queue.ack(entry.entry_id)
                await _clear_transient(redis, entry.entry_id)
                logger.error(
                    f"task {entry.entry_id} for doc {doc_id}: terminal "
                    f"{type(e).__name__} — marked failed and acked"
                )
            else:
                logger.error(
                    f"task {entry.entry_id} for doc {doc_id}: terminal "
                    f"{type(e).__name__} but could not record failed status — "
                    f"leaving in PEL for reclaim"
                )
    finally:
        try:
            await progress.clear()
        except Exception as e:
            logger.warning(f"progress clear failed for doc {doc_id}: {e}")


async def main() -> None:
    consumer = _pod_name()
    logger.info(f"document-worker starting (consumer={consumer!r})")

    # The worker fires run_hooks("post_document_ingest", ...) from
    # RAGService.process_existing_document, but it never runs the FastAPI
    # lifecycle where those hooks are normally registered. Populate the
    # global registry here or KG + Schicht A extraction silently no-op for
    # every knowledge-base upload (the primary ingestion path). Import-light
    # by design — see services/document_ingest_hooks.py.
    from services.document_ingest_hooks import register_document_ingest_hooks

    register_document_ingest_hooks()

    # socket_timeout > read_one's block window — see _REDIS_SOCKET_TIMEOUT_S.
    # This client is shared by the blocking read loop AND the heartbeat, so the
    # explicit timeout must be set here too (passing the client in bypasses
    # DocumentTaskQueue's own from_url default).
    redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=_REDIS_SOCKET_TIMEOUT_S,
    )
    queue = DocumentTaskQueue(redis_client=redis, consumer_id=consumer)
    await queue.ensure_group()

    # Heartbeat MUST start before reclaim_stale. On restart, the PEL may
    # contain several entries left over from the previous consumer; each
    # reclaimed entry runs through _process_entry (Docling: 15–120 s).
    # Posting the heartbeat during that window keeps /api/knowledge/upload
    # green instead of 503'ing for minutes while the worker is in fact
    # alive and catching up.
    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(redis, stop_event, consumer))

    # Reclaim anything a previous consumer started but didn't finish.
    reclaimed = await queue.reclaim_stale()
    if reclaimed:
        logger.warning(
            f"reclaimed {len(reclaimed)} pending entries on startup; "
            "processing them before reading new tasks"
        )
        for entry in reclaimed:
            await _process_entry(redis, queue, entry)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Periodic reclaim: startup reclaim_stale only re-adopts what a PREVIOUS pod
    # orphaned. An entry orphaned WHILE this pod keeps running (OOMKill mid-OCR
    # that the pod survives, or a transient-error return) would otherwise never
    # be retried until the next restart. Reap on an interval so recovery no
    # longer depends on a restart.
    #
    # Safe against stealing this worker's OWN in-flight task because the loop is
    # blocked in `await _process_entry` while a doc is processing, so this reclaim
    # branch cannot run mid-processing — even for a doc slower than visibility_ms.
    # NOTE: this relies on the SINGLE-consumer deployment (replicas=1). With
    # replicas>1, a doc whose processing exceeds visibility_ms (e.g. a ~20min OCR)
    # could be XAUTOCLAIM'd by ANOTHER replica's periodic reclaim → double-process.
    # If this Deployment is ever scaled out, raise the periodic min-idle to
    # visibility_ms + max-processing-time margin.
    reclaim_interval = settings.worker_reclaim_interval_seconds
    last_reclaim = time.monotonic()
    try:
        while not stop_event.is_set():
            if reclaim_interval > 0 and time.monotonic() - last_reclaim >= reclaim_interval:
                last_reclaim = time.monotonic()
                try:
                    for stale in await queue.reclaim_stale():
                        await _process_entry(redis, queue, stale)
                except Exception as e:  # noqa: BLE001 - reclaim must never kill the loop
                    logger.warning(f"periodic reclaim failed: {e}")
            entry = await queue.read_one(block_ms=5_000)
            if entry is None:
                continue
            await _process_entry(redis, queue, entry)
    finally:
        logger.info("document-worker shutting down")
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await redis.delete(HEARTBEAT_KEY)
        except Exception:
            pass
        await queue.close()
        await redis.aclose()
        logger.info("document-worker exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
