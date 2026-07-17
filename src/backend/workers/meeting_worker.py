"""
Meeting-transcription worker (§2).

Consumes tasks from the ``renfield:tasks:meeting`` Redis Stream and runs the
diarization + ASR pipeline (via the voice-server) out-of-process from the
backend, ingesting the speaker-attributed transcript into the KB.

Entry point::

    python -m workers.meeting_worker

Mirrors ``document_processor_worker`` (module isolation, heartbeat, reclaim,
poison-pill quarantine, transient/terminal classification), with ONE deliberate
divergence: because a meeting job can run for hours (vs Docling's 15-120 s), the
document worker's "PROCESSING is always stale, purge & retry" shortcut is
UNSAFE. Instead the ``Meeting`` row carries a ``status`` + ``heartbeat_at``
in-flight guard (design D13): on redelivery a ``completed`` meeting is acked &
skipped, a ``processing`` one with a FRESH heartbeat is left in the PEL (a
genuinely-running job — don't double-process), and only a dead one
(stale/absent heartbeat) is retried.

Design constraints (same as the document worker):
- Must NOT import ``main`` / instantiate the FastAPI app (memory isolation).
- Graceful SIGTERM/SIGINT drain; the Streams PEL + reclaim recovers a SIGKILL.
- Heartbeat key ``renfield:worker:meeting:heartbeat`` gates the upload route.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import datetime, timedelta

import redis.asyncio as aioredis
from loguru import logger
from redis import exceptions as redis_exceptions
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError

from models.database import Meeting
from services.database import AsyncSessionLocal
from services.meeting_pipeline import process_meeting
from services.task_queue import (
    _REDIS_SOCKET_TIMEOUT_S,
    MeetingTaskQueue,
    StreamEntry,
)
from services.voice_server_client import VoiceServerError
from utils.config import settings

HEARTBEAT_KEY = "renfield:worker:meeting:heartbeat"
HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_TTL_S = 90

# Row-level heartbeat: the worker refreshes Meeting.heartbeat_at this often while
# a job runs; a redelivered PROCESSING row whose heartbeat is older than the
# stale threshold is treated as a dead attempt and retried.
ROW_HEARTBEAT_REFRESH_S = 30
ROW_HEARTBEAT_STALE_S = 120


def _pod_name() -> str:
    return os.environ.get("POD_NAME") or os.environ.get("HOSTNAME", "worker-local")


# Infra blips → RETRYABLE (left un-ACKed for reclaim). VoiceServerError is
# included: a voice-server restart mid-job should retry, not quarantine. A
# genuinely unprocessable audio redelivers until the poison cap, then quarantines.
_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    VoiceServerError,
    redis_exceptions.ConnectionError,
    redis_exceptions.TimeoutError,
    OperationalError,
    InterfaceError,
    DisconnectionError,
)


def _is_transient_error(exc: BaseException) -> bool:
    return isinstance(exc, _TRANSIENT_EXC)


async def _mark_meeting_failed(meeting_id, error: BaseException) -> bool:
    """Persist ``status=failed`` for a terminally-failed meeting in a FRESH
    session. Returns True when the terminal state is recorded (committed,
    already failed, or the row is gone — all safe to ACK), False when it could
    NOT be persisted so the caller leaves the entry in the PEL for reclaim."""
    try:
        async with AsyncSessionLocal() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None:
                return True
            if meeting.status != "failed":
                meeting.status = "failed"
                meeting.error = str(error)[:2000]
                await db.commit()
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"could not mark meeting {meeting_id} failed: {e}")
        return False


async def _heartbeat_loop(
    redis: aioredis.Redis, stop_event: asyncio.Event, consumer_id: str
) -> None:
    """Refresh the process-level liveness key while the worker is up."""
    while not stop_event.is_set():
        try:
            await redis.set(HEARTBEAT_KEY, consumer_id, ex=HEARTBEAT_TTL_S)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"heartbeat write failed: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_S)
        except asyncio.TimeoutError:
            continue


async def _row_heartbeat_loop(meeting_id, stop_event: asyncio.Event) -> None:
    """Refresh Meeting.heartbeat_at while the (long) job runs so a redelivery can
    tell a live job from a dead one. Own session per write (short-lived)."""
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                meeting = await db.get(Meeting, meeting_id)
                if meeting is not None:
                    meeting.heartbeat_at = datetime.utcnow()
                    await db.commit()
        except Exception as e:  # noqa: BLE001 - a heartbeat write must never crash the job
            logger.debug(f"row heartbeat write failed for meeting {meeting_id}: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=ROW_HEARTBEAT_REFRESH_S)
        except asyncio.TimeoutError:
            continue


def _transient_key(entry_id: str) -> str:
    return f"renfield:tasks:meeting:transient:{entry_id}"


async def _clear_transient(redis: aioredis.Redis, entry_id: str) -> None:
    try:
        await redis.delete(_transient_key(entry_id))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"transient-counter cleanup failed for {entry_id}: {e}")


async def _claim_meeting_row(meeting_id) -> str:
    """Row-level in-flight guard (design D13). Returns one of:
      "skip"      — already completed (duplicate delivery) → ack & skip
      "wait"      — processing with a FRESH heartbeat (live job) → leave in PEL
      "proceed"   — pending / failed / dead-processing → claimed as processing
      "gone"      — the meeting row no longer exists → ack & skip
    Sets status=processing + heartbeat_at when it returns "proceed"."""
    async with AsyncSessionLocal() as db:
        meeting = await db.get(Meeting, meeting_id)
        if meeting is None:
            return "gone"
        if meeting.status == "completed":
            return "skip"
        if meeting.status == "processing" and meeting.heartbeat_at is not None:
            if datetime.utcnow() - meeting.heartbeat_at < timedelta(seconds=ROW_HEARTBEAT_STALE_S):
                return "wait"
        # pending, failed, or processing-with-stale/absent-heartbeat → claim it.
        meeting.status = "processing"
        meeting.heartbeat_at = datetime.utcnow()
        meeting.error = None
        await db.commit()
        return "proceed"


async def _process_entry(
    redis: aioredis.Redis, queue: MeetingTaskQueue, entry: StreamEntry
) -> None:
    """Handle a single meeting task. Success → XACK; exception → leave in PEL."""
    meeting_id = entry.params.get("meeting_id")
    audio_path = entry.params.get("audio_path")
    if meeting_id is None or not audio_path:
        logger.error(f"skipping entry {entry.entry_id}: missing meeting_id/audio_path")
        await queue.ack(entry.entry_id)
        return

    # OOM-poison guard (identical shape to the document worker): an entry
    # crash-redelivered past the cap is quarantined instead of crashlooping.
    delivery_count = getattr(entry, "delivery_count", 1)
    if delivery_count > 1:
        try:
            transient_leaves = int(await redis.get(_transient_key(entry.entry_id)) or 0)
        except Exception:  # noqa: BLE001
            transient_leaves = 0
        crash_count = delivery_count - transient_leaves
        if crash_count > settings.worker_max_deliveries:
            logger.error(
                f"meeting {meeting_id}: entry {entry.entry_id} crash-redelivered "
                f"{crash_count}x (> {settings.worker_max_deliveries}) — quarantining"
            )
            await _mark_meeting_failed(
                meeting_id,
                RuntimeError(
                    f"quarantined after {crash_count} crash redeliveries "
                    f"(worker kept dying mid-processing)"
                ),
            )
            await queue.ack(entry.entry_id)
            await _clear_transient(redis, entry.entry_id)
            return

    # Row-level in-flight guard.
    claim = await _claim_meeting_row(meeting_id)
    if claim in ("skip", "gone"):
        await queue.ack(entry.entry_id)
        await _clear_transient(redis, entry.entry_id)
        logger.info(f"meeting {meeting_id}: {claim} (entry {entry.entry_id}) — acked")
        return
    if claim == "wait":
        # A live job is running (fresh heartbeat). Leave in the PEL; a later
        # reclaim retries once the heartbeat goes stale. Do NOT ack.
        logger.info(
            f"meeting {meeting_id}: live job (fresh heartbeat) — leaving entry "
            f"{entry.entry_id} in PEL"
        )
        return

    # claim == "proceed": we own it. Keep the row heartbeat fresh during the job.
    row_stop = asyncio.Event()
    row_hb = asyncio.create_task(_row_heartbeat_loop(meeting_id, row_stop))
    try:
        await process_meeting(meeting_id, audio_path)
        await queue.ack(entry.entry_id)
        await _clear_transient(redis, entry.entry_id)
        logger.info(f"transcribed meeting {meeting_id} (entry {entry.entry_id})")
    except Exception as e:
        logger.exception(f"meeting {meeting_id} (entry {entry.entry_id}) failed: {e}")
        if _is_transient_error(e):
            # Infra blip / voice-server hiccup: leave un-ACKed for reclaim; record
            # the CLEAN leave so it isn't counted as a crash by the poison guard.
            try:
                tkey = _transient_key(entry.entry_id)
                await redis.incr(tkey)
                await redis.expire(tkey, 86_400)
            except Exception as ie:  # noqa: BLE001
                logger.debug(f"transient-counter incr failed for {entry.entry_id}: {ie}")
            logger.warning(
                f"meeting {meeting_id}: transient {type(e).__name__} — leaving in PEL"
            )
        else:
            if await _mark_meeting_failed(meeting_id, e):
                await queue.ack(entry.entry_id)
                await _clear_transient(redis, entry.entry_id)
                logger.error(
                    f"meeting {meeting_id}: terminal {type(e).__name__} — marked failed"
                )
            else:
                logger.error(
                    f"meeting {meeting_id}: terminal {type(e).__name__} but could not "
                    f"record failed status — leaving in PEL for reclaim"
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
    logger.info(f"meeting-worker starting (consumer={consumer!r})")

    # NB: no register_document_ingest_hooks() here — the transcript ingest
    # (task #10) calls folder_ingest.ingest_document which only ENQUEUES the
    # transcript Document to the document worker; the KG/Schicht-A hooks fire
    # THERE (in the document worker's own process), not in this pod.

    redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=_REDIS_SOCKET_TIMEOUT_S,
    )
    queue = MeetingTaskQueue(redis_client=redis, consumer_id=consumer)
    await queue.ensure_group()

    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(redis, stop_event, consumer))

    reclaimed = await queue.reclaim_stale()
    if reclaimed:
        logger.warning(f"reclaimed {len(reclaimed)} pending meeting entries on startup")
        for entry in reclaimed:
            await _process_entry(redis, queue, entry)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Periodic reclaim — safe against stealing our own in-flight task only at
    # replicas=1 (the loop is blocked in _process_entry while a job runs). The
    # k8s manifest pins replicas:1 + Recreate for this reason.
    reclaim_interval = settings.worker_reclaim_interval_seconds
    last_reclaim = time.monotonic()
    try:
        while not stop_event.is_set():
            if reclaim_interval > 0 and time.monotonic() - last_reclaim >= reclaim_interval:
                last_reclaim = time.monotonic()
                try:
                    for stale in await queue.reclaim_stale():
                        await _process_entry(redis, queue, stale)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"periodic reclaim failed: {e}")
            entry = await queue.read_one(block_ms=5_000)
            if entry is None:
                continue
            await _process_entry(redis, queue, entry)
    finally:
        logger.info("meeting-worker shutting down")
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
        logger.info("meeting-worker exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
