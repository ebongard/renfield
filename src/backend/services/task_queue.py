"""
Task Queue Service mit Redis (async).

Two implementations live here:

- ``TaskQueue`` (original): fire-and-forget list-based queue using LPUSH/RPOP.
  Destructive read — a crashing consumer loses the in-flight task. Fine for
  tasks that can be dropped silently.
- ``DocumentTaskQueue`` (#388): Redis Streams with a consumer group. Entries
  stay in the Pending Entries List until ACKed, so a crash leaves the task
  recoverable. Used by the document-processor worker so that an OOM-kill
  mid-OCR does not silently orphan a Document row in ``status=processing``.
"""
import json
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from loguru import logger
from redis.exceptions import TimeoutError as RedisTimeoutError

from utils.config import settings

# redis-py 8.0 changed DEFAULT_SOCKET_TIMEOUT from None to 5s. A client built via
# from_url() WITHOUT an explicit socket_timeout therefore applies a 5s read timeout
# to EVERY command — including the blocking ``XREADGROUP ... BLOCK 5000`` in
# DocumentTaskQueue.read_one(), whose 5s server-side block then races the 5s socket
# timeout, loses, raises redis.exceptions.TimeoutError, and disconnects the socket on
# essentially every idle poll (this crashlooped the document worker after the image
# floated redis to 8.0.0). We pin an explicit socket_timeout STRICTLY GREATER than
# read_one's block window: the empty BLOCK reply then always arrives before the read
# times out (returns [] cleanly, connection reused), while xack/xadd/heartbeat still
# get a sane finite timeout instead of hanging forever. MUST stay > the largest
# block_ms read_one is ever called with (currently 5_000ms). Verified vs redis-py 8.0.0.
_REDIS_SOCKET_TIMEOUT_S = 15


class TaskQueue:
    """Async Task Queue mit Redis"""

    def __init__(self):
        self.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        self.queue_name = "renfield:tasks"

    async def enqueue(self, task_type: str, parameters: dict) -> str:
        """Task in Queue einreihen"""
        try:
            task_id = f"task:{task_type}:{await self.redis_client.incr('task:counter')}"

            task_data = {
                "id": task_id,
                "type": task_type,
                "parameters": parameters,
                "status": "queued"
            }

            # In Redis speichern
            await self.redis_client.lpush(self.queue_name, json.dumps(task_data))
            await self.redis_client.set(task_id, json.dumps(task_data))

            logger.info(f"Task {task_id} eingefuegt")
            return task_id
        except Exception as e:
            logger.error(f"Enqueue Fehler: {e}")
            raise

    async def dequeue(self) -> dict | None:
        """Naechsten Task aus Queue holen"""
        try:
            task_json = await self.redis_client.rpop(self.queue_name)
            if task_json:
                return json.loads(task_json)
            return None
        except Exception as e:
            logger.error(f"Dequeue Fehler: {e}")
            return None

    async def get_task_status(self, task_id: str) -> dict | None:
        """Task-Status abrufen"""
        try:
            task_json = await self.redis_client.get(task_id)
            if task_json:
                return json.loads(task_json)
            return None
        except Exception as e:
            logger.error(f"Get Status Fehler: {e}")
            return None

    async def update_task_status(self, task_id: str, status: str, result: dict | None = None):
        """Task-Status aktualisieren"""
        try:
            task = await self.get_task_status(task_id)
            if task:
                task["status"] = status
                if result:
                    task["result"] = result
                await self.redis_client.set(task_id, json.dumps(task))
                logger.info(f"Task {task_id} Status: {status}")
        except Exception as e:
            logger.error(f"Update Status Fehler: {e}")

    async def queue_length(self) -> int:
        """Anzahl der Tasks in Queue"""
        return await self.redis_client.llen(self.queue_name)

    async def close(self):
        """Close Redis connection gracefully."""
        await self.redis_client.close()


# ---------------------------------------------------------------------------
# DocumentTaskQueue — Redis Streams with consumer group + reclaim.
# ---------------------------------------------------------------------------

@dataclass
class StreamEntry:
    """One document-processing task pulled from the stream."""

    entry_id: str
    params: dict[str, Any]
    # How many times this entry has been delivered (XPENDING times_delivered).
    # 1 for a fresh read_one; >1 for a reclaimed entry (a prior consumer took it
    # and died without ACKing). The worker uses this as an OOM-poison guard: a
    # doc redelivered past the cap is quarantined (marked failed) instead of
    # re-processed, so a doc that OOM-kills the worker every attempt can't
    # crashloop the queue once periodic reclaim re-adopts it.
    delivery_count: int = 1


class DocumentTaskQueue:
    """Durable task queue for document ingestion using Redis Streams.

    Why Streams (not a Redis list): the list-based ``TaskQueue`` uses RPOP
    which removes the entry on read. If the worker crashes between RPOP and
    finishing the task, the work is silently lost and the Document row is
    stuck in ``status=processing`` forever. Streams keep the entry in the
    Pending Entries List (PEL) until ``XACK`` is called; a reboot or a
    separate consumer can pick it up via ``XCLAIM`` once the visibility
    window has elapsed.

    Contract:
      * One stream (``renfield:tasks:document``) and one consumer group
        (``docworker``). Each worker pod is a consumer identified by its
        pod name so ``XPENDING`` can attribute stuck entries.
      * ``enqueue`` is a single ``XADD`` — atomic on the Redis side.
      * ``read_one`` uses ``XREADGROUP BLOCK`` for efficient long-polling;
        returns ``None`` if the timeout fires with no new entry.
      * ``ack`` is called after the task finishes successfully. If an
        exception propagates out of the handler, the entry stays in the PEL
        and ``reclaim_stale`` moves it to the current consumer on the next
        start-up (or whenever the caller decides to reap).
    """

    DEFAULT_STREAM = "renfield:tasks:document"
    DEFAULT_GROUP = "docworker"
    DEFAULT_VISIBILITY_MS = 600_000  # 10 min, covers worst-case OCR

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        consumer_id: str = "worker-local",
        stream_key: str = DEFAULT_STREAM,
        group_name: str = DEFAULT_GROUP,
        visibility_ms: int = DEFAULT_VISIBILITY_MS,
    ):
        self.redis_client = redis_client or aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_S,
        )
        self.consumer_id = consumer_id
        self.stream_key = stream_key
        self.group_name = group_name
        self.visibility_ms = visibility_ms
        self._owns_client = redis_client is None

    async def ensure_group(self) -> None:
        """Create the consumer group if it doesn't exist.

        ``MKSTREAM`` creates the stream on the fly, which is what we want on
        a fresh Redis. If the group already exists Redis returns
        ``BUSYGROUP`` — we treat that as success.
        """
        try:
            await self.redis_client.xgroup_create(
                name=self.stream_key,
                groupname=self.group_name,
                id="$",
                mkstream=True,
            )
            logger.info(
                f"DocumentTaskQueue: created consumer group {self.group_name!r} "
                f"on stream {self.stream_key!r}"
            )
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            # Group already existed — normal on subsequent starts.

    async def enqueue(self, params: dict[str, Any]) -> str:
        """Add a task to the stream. Returns the stream entry id (ms-seq)."""
        entry_id = await self.redis_client.xadd(
            self.stream_key,
            {"payload": json.dumps(params)},
        )
        logger.info(f"DocumentTaskQueue: enqueued {entry_id} params={params}")
        return entry_id

    async def read_one(self, block_ms: int = 5_000) -> StreamEntry | None:
        """Block for up to ``block_ms`` ms waiting for a task. Returns
        ``None`` if the window closed with no new entry."""
        try:
            result = await self.redis_client.xreadgroup(
                groupname=self.group_name,
                consumername=self.consumer_id,
                streams={self.stream_key: ">"},
                count=1,
                block=block_ms,
            )
        except RedisTimeoutError:
            # Defense-in-depth. The primary fix is _REDIS_SOCKET_TIMEOUT_S being
            # set > block_ms at client construction, so a normal idle poll returns
            # [] (handled below), NOT a timeout. Reaching here means a single read
            # exceeded the socket timeout (~15s) — redis is genuinely pathological
            # (overloaded / packets dropped while TCP stays up). Degrade to "no
            # task" so the worker retries instead of crashing, but WARN so the
            # stall is visible rather than silently masked.
            logger.warning(
                f"DocumentTaskQueue.read_one: redis read timed out after "
                f">{_REDIS_SOCKET_TIMEOUT_S}s (block_ms={block_ms}); treating as no "
                f"task. Redis may be overloaded or unreachable at the socket layer."
            )
            return None
        if not result:
            return None
        # XREADGROUP returns [(stream_name, [(entry_id, {field: value}), ...])]
        _stream, entries = result[0]
        entry_id, fields = entries[0]
        try:
            params = json.loads(fields.get("payload", "{}"))
        except json.JSONDecodeError as e:
            logger.error(
                f"DocumentTaskQueue: bad payload for {entry_id}: {e}. "
                "Acking to prevent poison-pill loop."
            )
            await self.ack(entry_id)
            return None
        return StreamEntry(entry_id=entry_id, params=params)

    async def ack(self, entry_id: str) -> None:
        """Acknowledge successful processing. Removes the entry from the PEL."""
        await self.redis_client.xack(self.stream_key, self.group_name, entry_id)

    async def reclaim_stale(self, min_idle_ms: int | None = None) -> list[StreamEntry]:
        """Claim entries from dead consumers whose idle time exceeds the
        visibility window. Typically called once on worker startup so the
        current pod adopts anything a previous (now-gone) pod was working on
        when it died.
        """
        min_idle = min_idle_ms if min_idle_ms is not None else self.visibility_ms
        # XAUTOCLAIM returns (next_cursor, claimed_entries, deleted_ids)
        cursor = "0-0"
        claimed: list[StreamEntry] = []
        while True:
            result = await self.redis_client.xautoclaim(
                name=self.stream_key,
                groupname=self.group_name,
                consumername=self.consumer_id,
                min_idle_time=min_idle,
                start_id=cursor,
                count=100,
            )
            # redis.asyncio returns a 3-tuple (next_cursor, items, deleted)
            next_cursor, items, _deleted = result
            for entry_id, fields in items:
                try:
                    params = json.loads(fields.get("payload", "{}"))
                except json.JSONDecodeError:
                    # Poison entry — ack to drop, log.
                    logger.error(
                        f"DocumentTaskQueue: reclaimed {entry_id} has bad payload; dropping"
                    )
                    await self.ack(entry_id)
                    continue
                claimed.append(StreamEntry(entry_id=entry_id, params=params))
            # XAUTOCLAIM returns "0-0" as next_cursor when the scan has
            # completed a full loop. An empty items batch with a non-"0-0"
            # cursor just means "no entries matched the min_idle filter in
            # this window"; later windows may still have entries, so we
            # must keep iterating.
            if next_cursor == "0-0":
                break
            cursor = next_cursor
        if claimed:
            # Attach the per-entry delivery count (XAUTOCLAIM just incremented it)
            # so the worker can quarantine an entry redelivered past the poison
            # cap — a doc that OOM-kills the worker every attempt must NOT be
            # re-processed indefinitely (it would crashloop the queue). Best-effort:
            # a redis hiccup here must NOT abort the reclaim (on the startup path an
            # exception would crash main() → pod restart). Fall back to the default
            # count; the entries still process, just without the poison stamp.
            try:
                counts = await self._pending_delivery_counts()
                for e in claimed:
                    e.delivery_count = counts.get(e.entry_id, e.delivery_count)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"DocumentTaskQueue: delivery-count fetch failed ({exc}); "
                    f"reclaimed entries proceed without a poison stamp"
                )
            logger.warning(
                f"DocumentTaskQueue: reclaimed {len(claimed)} stale entries "
                f"from previous consumers"
            )
        return claimed

    async def _pending_delivery_counts(self) -> dict[str, int]:
        """Map ``entry_id -> times_delivered`` for every entry in the PEL.

        Used to stamp reclaimed entries with their redelivery count (the poison
        guard). Paginates the PEL with the exclusive ``(id`` cursor (Redis 6.2+)."""
        counts: dict[str, int] = {}
        start = "-"
        while True:
            pend = await self.redis_client.xpending_range(
                self.stream_key, self.group_name, min=start, max="+", count=200,
            )
            if not pend:
                break
            for p in pend:
                counts[p["message_id"]] = int(p["times_delivered"])
            if len(pend) < 200:
                break
            start = "(" + pend[-1]["message_id"]  # exclusive next page
        return counts

    async def pending_count(self) -> int:
        """Number of entries currently in the consumer group's PEL.

        Combined with ``stream_length`` this lets the API compute a user-facing
        queue position for a pending Document row.
        """
        summary = await self.redis_client.xpending(self.stream_key, self.group_name)
        # xpending with no args returns either a dict or a positional list
        # depending on the Redis library version. Normalise defensively.
        if isinstance(summary, dict):
            return int(summary.get("pending", 0))
        if isinstance(summary, list) and summary:
            return int(summary[0])
        return 0

    async def stream_length(self) -> int:
        """Total length of the stream (includes already-acked entries that
        haven't been trimmed)."""
        return int(await self.redis_client.xlen(self.stream_key))

    async def close(self) -> None:
        if self._owns_client:
            await self.redis_client.aclose()


class MeetingTaskQueue(DocumentTaskQueue):
    """Durable queue for §2 meeting-transcription jobs.

    Same Redis-Streams durability contract as ``DocumentTaskQueue`` (a crash
    leaves the entry in the PEL, recovered via ``reclaim_stale``), on a
    DEDICATED stream + consumer group so meeting jobs never share a visibility
    window with document ingestion. The visibility window is sized to the max
    meeting duration (jobs run far longer than OCR), so a genuinely-running job
    is never spuriously reclaimed out from under itself mid-run.

    Payload carries the audio PATH, never the bytes (the worker reads it off the
    shared uploads PVC).
    """

    DEFAULT_STREAM = "renfield:tasks:meeting"
    DEFAULT_GROUP = "meetingworker"

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        consumer_id: str = "worker-local",
        visibility_ms: int | None = None,
    ):
        if visibility_ms is None:
            # cap (h) -> ms, + 50% margin so a job running right up to the cap is
            # still "fresh" and not reclaimed by a concurrent reaper.
            visibility_ms = int(settings.meeting_max_duration_h * 3600 * 1000 * 1.5)
        super().__init__(
            redis_client=redis_client,
            consumer_id=consumer_id,
            stream_key=self.DEFAULT_STREAM,
            group_name=self.DEFAULT_GROUP,
            visibility_ms=visibility_ms,
        )


# ---------------------------------------------------------------------------
# Document-worker liveness (shared)
# ---------------------------------------------------------------------------

# Written every 30 s by the document-worker pod with a 90 s TTL (see
# workers/document_processor_worker.HEARTBEAT_KEY). Absent → no consumer is
# draining the stream, so callers should 503 instead of enqueueing into a
# queue nobody reads. Promoted here from api/routes/knowledge.py (which now
# delegates) — it had grown four underscore-private cross-module importers.
DOCUMENT_WORKER_HEARTBEAT_KEY = "renfield:worker:document:heartbeat"


async def document_worker_is_alive() -> bool:
    """True when a document worker refreshed its heartbeat within the TTL."""
    from services.redis_client import get_redis

    try:
        value = await get_redis().get(DOCUMENT_WORKER_HEARTBEAT_KEY)
    except Exception as e:  # noqa: BLE001
        # A Redis outage masks the worker's real state; treat as dead so we
        # fail loudly rather than silently enqueue into a broken Redis.
        logger.warning(f"heartbeat check failed: {e}; treating worker as unavailable")
        return False
    return value is not None


class PdfSplitTaskQueue(DocumentTaskQueue):
    """Durable queue for the PDF-split SLOW LANE (docs/design/pdf-split.md PR3).

    Same Redis-Streams durability contract as ``DocumentTaskQueue``, on a
    DEDICATED stream + consumer group: a bad scan needing per-page VLM
    transcription runs for unbounded minutes and must never head-of-line-block
    document ingestion nor share its visibility window. Liveness of a running
    job is judged by the ROW heartbeat (``documents.split_heartbeat_at``), not
    a duration estimate — the worker reclaims on heartbeat staleness.

    Payload: ``{document_id, user_id}`` (bytes stay on the uploads PVC).
    """

    DEFAULT_STREAM = "renfield:tasks:pdfsplit"
    DEFAULT_GROUP = "pdfsplitworker"


PDF_SPLIT_WORKER_HEARTBEAT_KEY = "renfield:worker:pdfsplit:heartbeat"


async def pdf_split_worker_is_alive() -> bool:
    """True when a pdf-split worker refreshed its heartbeat within the TTL.
    Gates the slow-lane routing: with no worker deployed, the inline path
    keeps the pre-PR3 status quo (single-document ingest, loud log) instead of
    parking documents on a stream nobody drains."""
    from services.redis_client import get_redis

    try:
        value = await get_redis().get(PDF_SPLIT_WORKER_HEARTBEAT_KEY)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"pdfsplit heartbeat check failed: {e}; treating worker as unavailable"
        )
        return False
    return value is not None
