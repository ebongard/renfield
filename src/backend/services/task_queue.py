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

from utils.config import settings


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
            settings.redis_url, decode_responses=True
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
        result = await self.redis_client.xreadgroup(
            groupname=self.group_name,
            consumername=self.consumer_id,
            streams={self.stream_key: ">"},
            count=1,
            block=block_ms,
        )
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
            logger.warning(
                f"DocumentTaskQueue: reclaimed {len(claimed)} stale entries "
                f"from previous consumers"
            )
        return claimed

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
