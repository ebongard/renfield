"""
PDF-split slow-lane worker (docs/design/pdf-split.md PR3).

Consumes tasks from the ``renfield:tasks:pdfsplit`` Redis Stream and runs the
slow split lane (per-page VLM transcription of garbage pages + multi-window
boundary detection) out-of-process from both the backend AND the document
worker — a bad scan can take unbounded minutes and must never head-of-line-
block document ingestion.

Entry point::

    python -m workers.pdf_split_worker

Mirrors ``meeting_worker`` (module isolation, pod heartbeat, reclaim, poison
guard, transient/terminal classification, ROW-level in-flight heartbeat —
``documents.split_heartbeat_at`` — because jobs outlive any fixed visibility
window; replicas MUST stay 1). ONE deliberate divergence in the poison /
transient-cap outcome: a slow-lane document is NEVER quarantined as ``failed``
— the fail-safe is handing it back to normal ingest as a SINGLE document
(``skip_split``), which is exactly the pre-PR3 status quo. A document that
reached this lane is processable; only the split decision timed out.

Design constraints (same as the other workers):
- Must NOT import ``main`` / instantiate the FastAPI app (memory isolation).
- Graceful SIGTERM/SIGINT drain; the Streams PEL + reclaim recovers a SIGKILL.
- Heartbeat key ``renfield:worker:pdfsplit:heartbeat`` gates the slow-lane
  routing in the inline pre-stage (no worker alive → status-quo single ingest).
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import datetime, timedelta

import httpx
import redis.asyncio as aioredis
from loguru import logger
from redis import exceptions as redis_exceptions
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError

from models.database import (
    DOC_STATUS_PENDING,
    DOC_STATUS_SPLIT_ARCHIVED,
    DOC_STATUS_SPLIT_PENDING,
    DOC_STATUS_SPLIT_REVIEW,
    Document,
)
from services.database import AsyncSessionLocal
from services.pdf_split_errors import SplitTransientError
from services.pdf_split_slow_lane import process_slow_split
from services.task_queue import (
    _REDIS_SOCKET_TIMEOUT_S,
    PDF_SPLIT_WORKER_HEARTBEAT_KEY,
    DocumentTaskQueue,
    PdfSplitTaskQueue,
    StreamEntry,
)
from utils.config import settings

HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_TTL_S = 90

# Row-level heartbeat on documents.split_heartbeat_at (mirrors the meeting
# worker's Meeting.heartbeat_at): refreshed while a job runs; a redelivered
# split_pending row with a FRESH heartbeat is a live job → left in the PEL.
ROW_HEARTBEAT_REFRESH_S = 30
ROW_HEARTBEAT_STALE_S = 120

_RECLAIM_MIN_IDLE_MS = ROW_HEARTBEAT_STALE_S * 1000


def _pod_name() -> str:
    return os.environ.get("POD_NAME") or os.environ.get("HOSTNAME", "worker-local")


# Infra blips → RETRYABLE (left un-ACKed for reclaim). SplitTransientError is
# the lane's own retry signal (LLM host down mid-detection, RETRY-class child
# ingest). Everything else terminal-ish — but see _hand_back_as_single: the
# fail-safe outcome is single-ingest, not a failed doc.
_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    redis_exceptions.ConnectionError,
    redis_exceptions.TimeoutError,
    OperationalError,
    InterfaceError,
    DisconnectionError,
    SplitTransientError,
)


def _is_transient_error(exc: BaseException) -> bool:
    return isinstance(exc, _TRANSIENT_EXC)


async def _hand_back_as_single(document_id, reason: str) -> bool:
    """Fail-safe terminal outcome: give up on splitting and return the doc to
    normal single-document ingest (pre-PR3 status quo). Children that already
    exist stay (visible, deletable); the combined parent then ingests whole —
    ONLY reached when the split path is verifiably stuck, and preferred over a
    permanently failed doc. Returns False when the hand-back could not be
    persisted (caller leaves the entry in the PEL)."""
    try:
        async with AsyncSessionLocal() as db:
            doc = await db.get(Document, document_id)
            if doc is None:
                return True
            if doc.status == DOC_STATUS_SPLIT_ARCHIVED:
                return True  # split finished after all — nothing to do
            logger.error(
                f"pdf-split[slow]: doc {document_id} hand-back as single "
                f"document ({reason})"
            )
            doc.status = DOC_STATUS_PENDING
            doc.split_heartbeat_at = None
            await db.commit()
        from services.redis_client import get_redis

        await DocumentTaskQueue(redis_client=get_redis()).enqueue(
            {"document_id": document_id, "force_ocr": False, "user_id": None,
             "skip_split": True}
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pdf-split[slow]: hand-back failed for doc {document_id}: {e}")
        return False


async def _heartbeat_loop(
    redis: aioredis.Redis, stop_event: asyncio.Event, consumer_id: str
) -> None:
    while not stop_event.is_set():
        try:
            await redis.set(
                PDF_SPLIT_WORKER_HEARTBEAT_KEY, consumer_id, ex=HEARTBEAT_TTL_S
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"heartbeat write failed: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_S)
        except TimeoutError:
            continue


async def _row_heartbeat_loop(document_id, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                doc = await db.get(Document, document_id)
                if doc is not None:
                    doc.split_heartbeat_at = datetime.utcnow()
                    await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"row heartbeat write failed for doc {document_id}: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=ROW_HEARTBEAT_REFRESH_S)
        except TimeoutError:
            continue


def _transient_key(entry_id: str) -> str:
    return f"renfield:tasks:pdfsplit:transient:{entry_id}"


async def _clear_transient(redis: aioredis.Redis, entry_id: str) -> None:
    try:
        await redis.delete(_transient_key(entry_id))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"transient-counter cleanup failed for {entry_id}: {e}")


async def _claim_row(document_id) -> str:
    """Row-level in-flight guard. Returns:
      "skip"    — resolved elsewhere (archived / review / gone / not split-parked)
      "wait"    — split_pending with a FRESH heartbeat (live job) → leave in PEL
      "proceed" — claimed (heartbeat stamped)"""
    async with AsyncSessionLocal() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            return "skip"
        if doc.status in (DOC_STATUS_SPLIT_ARCHIVED, DOC_STATUS_SPLIT_REVIEW):
            return "skip"
        if doc.status != DOC_STATUS_SPLIT_PENDING:
            # Un-parked elsewhere (reject hand-back, un-park+redetect) — the
            # document queue owns it now.
            return "skip"
        if doc.split_heartbeat_at is not None and (
            datetime.utcnow() - doc.split_heartbeat_at
            < timedelta(seconds=ROW_HEARTBEAT_STALE_S)
        ):
            return "wait"
        doc.split_heartbeat_at = datetime.utcnow()
        await db.commit()
        return "proceed"


async def _process_entry(
    redis: aioredis.Redis, queue: PdfSplitTaskQueue, entry: StreamEntry
) -> None:
    document_id = entry.params.get("document_id")
    user_id = entry.params.get("user_id")
    if document_id is None:
        logger.error(f"skipping entry {entry.entry_id}: missing document_id")
        await queue.ack(entry.entry_id)
        return

    # Poison guard (crash redeliveries) + transient-retry cap. Outcome is the
    # single-document hand-back, NEVER a failed doc (see module docstring).
    delivery_count = getattr(entry, "delivery_count", 1)
    if delivery_count > 1:
        try:
            transient_leaves = int(await redis.get(_transient_key(entry.entry_id)) or 0)
        except Exception:  # noqa: BLE001
            transient_leaves = 0
        crash_count = delivery_count - transient_leaves
        give_up = None
        if crash_count > settings.worker_max_deliveries:
            give_up = f"crash-redelivered {crash_count}x"
        elif transient_leaves > settings.pdf_split_worker_max_transient_retries:
            give_up = f"transient-failed {transient_leaves}x"
        if give_up:
            if await _hand_back_as_single(document_id, give_up):
                await queue.ack(entry.entry_id)
                await _clear_transient(redis, entry.entry_id)
            return

    claim = await _claim_row(document_id)
    if claim == "skip":
        await queue.ack(entry.entry_id)
        await _clear_transient(redis, entry.entry_id)
        return
    if claim == "wait":
        logger.info(
            f"doc {document_id}: live slow-split job (fresh heartbeat) — "
            f"leaving entry {entry.entry_id} in PEL"
        )
        return

    row_stop = asyncio.Event()
    row_hb = asyncio.create_task(_row_heartbeat_loop(document_id, row_stop))
    try:
        outcome = await process_slow_split(document_id, user_id)
        await queue.ack(entry.entry_id)
        await _clear_transient(redis, entry.entry_id)
        logger.info(
            f"slow-split doc {document_id}: {outcome} (entry {entry.entry_id})"
        )
    except Exception as e:
        logger.exception(f"slow-split doc {document_id} failed: {e}")
        if _is_transient_error(e):
            try:
                tkey = _transient_key(entry.entry_id)
                await redis.incr(tkey)
                await redis.expire(tkey, 86_400)
            except Exception as ie:  # noqa: BLE001
                logger.debug(f"transient-counter incr failed: {ie}")
            logger.warning(
                f"doc {document_id}: transient {type(e).__name__} — leaving in PEL"
            )
        else:
            # Terminal (e.g. SplitExecutionError — a child failed terminally
            # mid-execute). Children may already exist, so a single-ingest
            # hand-back would DOUBLE-ingest the combined parent: this one case
            # marks the doc failed (re-push REINGEST is the deliberate retry),
            # mirroring the document worker.
            try:
                async with AsyncSessionLocal() as db:
                    doc = await db.get(Document, document_id)
                    if doc is not None and doc.status != "failed":
                        doc.status = "failed"
                        doc.error_message = str(e)[:2000]
                        doc.split_heartbeat_at = None
                        await db.commit()
                await queue.ack(entry.entry_id)
                await _clear_transient(redis, entry.entry_id)
                logger.error(
                    f"doc {document_id}: terminal {type(e).__name__} — marked failed"
                )
            except Exception as me:  # noqa: BLE001
                logger.error(
                    f"doc {document_id}: could not record terminal failure ({me}) "
                    f"— leaving in PEL"
                )
    finally:
        row_stop.set()
        row_hb.cancel()
        try:
            await row_hb
        except asyncio.CancelledError:
            pass


async def main() -> None:
    consumer = _pod_name()
    logger.info(f"pdf-split-worker starting (consumer={consumer!r})")

    # No register_document_ingest_hooks(): execute_split only ENQUEUES children
    # to the document worker — KG/Schicht-A/Paperless hooks fire THERE.

    redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=_REDIS_SOCKET_TIMEOUT_S,
    )
    queue = PdfSplitTaskQueue(redis_client=redis, consumer_id=consumer)
    await queue.ensure_group()

    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(redis, stop_event, consumer))

    reclaimed = await queue.reclaim_stale(min_idle_ms=_RECLAIM_MIN_IDLE_MS)
    if reclaimed:
        logger.warning(f"reclaimed {len(reclaimed)} pending pdfsplit entries on startup")
        for entry in reclaimed:
            await _process_entry(redis, queue, entry)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Periodic reclaim — safe only at replicas=1 (the read loop is blocked
    # during a job); the k8s manifest pins replicas:1 + Recreate.
    reclaim_interval = settings.worker_reclaim_interval_seconds
    last_reclaim = time.monotonic()
    try:
        while not stop_event.is_set():
            if reclaim_interval > 0 and time.monotonic() - last_reclaim >= reclaim_interval:
                last_reclaim = time.monotonic()
                try:
                    for stale in await queue.reclaim_stale(min_idle_ms=_RECLAIM_MIN_IDLE_MS):
                        await _process_entry(redis, queue, stale)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"periodic reclaim failed: {e}")
            entry = await queue.read_one(block_ms=5_000)
            if entry is None:
                continue
            await _process_entry(redis, queue, entry)
    finally:
        logger.info("pdf-split-worker shutting down")
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await redis.delete(PDF_SPLIT_WORKER_HEARTBEAT_KEY)
        except Exception:
            pass
        await queue.close()
        await redis.aclose()
        logger.info("pdf-split-worker exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
