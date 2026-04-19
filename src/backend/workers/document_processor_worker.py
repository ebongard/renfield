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

Not wired into ``/api/knowledge/upload`` yet. PR A ships the worker as
runnable infra; the upload endpoint still takes the inline code path
until ``DOCUMENT_WORKER_ENABLED`` is flipped in PR C1.
"""
from __future__ import annotations

import asyncio
import os
import signal

import redis.asyncio as aioredis
from loguru import logger

from services.database import AsyncSessionLocal
from services.progress import DocumentProgress
from services.rag_service import RAGService
from services.task_queue import DocumentTaskQueue, StreamEntry
from utils.config import settings

HEARTBEAT_KEY = "renfield:worker:document:heartbeat"
HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_TTL_S = 90


def _pod_name() -> str:
    """Identify this worker instance. In k8s the env var ``POD_NAME`` is set
    via downward API; outside k8s we fall back to hostname for dev runs."""
    return os.environ.get("POD_NAME") or os.environ.get("HOSTNAME", "worker-local")


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

    force_ocr = bool(entry.params.get("force_ocr", False))
    user_id = entry.params.get("user_id")
    progress = DocumentProgress(redis, doc_id)
    try:
        async with AsyncSessionLocal() as db:
            rag = RAGService(db)
            await rag.process_existing_document(
                document_id=doc_id,
                force_ocr=force_ocr,
                user_id=user_id,
                progress=progress,
            )
        await queue.ack(entry.entry_id)
        logger.info(f"processed doc {doc_id} (entry {entry.entry_id})")
    except Exception as e:
        logger.exception(f"task {entry.entry_id} for doc {doc_id} failed: {e}")
        # Deliberately NOT ack'ing — reclaim_stale picks it up next time.
    finally:
        try:
            await progress.clear()
        except Exception as e:
            logger.warning(f"progress clear failed for doc {doc_id}: {e}")


async def main() -> None:
    consumer = _pod_name()
    logger.info(f"document-worker starting (consumer={consumer!r})")

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
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

    try:
        while not stop_event.is_set():
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
