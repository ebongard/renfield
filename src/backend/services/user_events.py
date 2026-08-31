"""Per-user server→browser event substrate.

A **content-free** "something in your corpus changed, refetch" signal pushed to a
user's open browser tabs over ``/ws/user``. The payload never carries document
identity (no id/title/filename) — only ``{type, reason}`` — so it is
circle/privacy-safe by construction; the browser's follow-up fetch is re-filtered
server-side.

**Cross-process by design.** Every emitter (worker, reconciler, API route)
``PUBLISH``es to one Redis channel; each API pod runs ONE subscriber
(:func:`run_user_events_subscriber`) that fans the event out to *its* local
:class:`UserEventRegistry`. This is the only correct shape: ingest completion runs
in the **document-worker** pod, which cannot reach the API pod's in-memory sockets
directly (unlike the same-process kiosk hub). Publish-always / subscriber-only
fan-out is also replica-safe — every backend replica subscribes, so an event
reaches whichever replica holds the user's socket.

**Reusable.** ``documents_changed`` is the first event ``type``; obligations /
notes / KG live-updates can add a ``type`` + a frontend ``case`` with no new
plumbing.

Delivery model (see ``docs/design/user-events-ws.md`` §4):
- ``fan_out(target=<int>)`` → that user's sockets only (owners; admins are NOT
  spammed by every user's change).
- ``fan_out(target=None)`` → the ``_ALL`` bucket only. ``_ALL`` holds
  auth-off (single-user household) sockets AND, in auth-on, admin sockets — so an
  unattributable (``owner=None``, e.g. null-KB) change still reaches someone.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

# Redis channel every emitter publishes to; the per-pod subscriber consumes it.
USER_EVENTS_CHANNEL = "renfield:events:user"

# Sentinel registry key for the broadcast bucket (auth-off household + admins).
ALL: str = "__all__"

# Event types (extend freely; the frontend maps each to a query invalidation).
EVENT_DOCUMENTS_CHANGED = "documents_changed"

# Per-socket send timeout — a backpressured/hung tab must never wedge fan-out.
_SEND_TIMEOUT_SECONDS = 5.0


def build_event(event_type: str, reason: str | None = None) -> dict[str, Any]:
    """A content-free event payload. Deliberately no document identity."""
    event: dict[str, Any] = {"type": event_type}
    if reason:
        event["reason"] = reason
    return event


async def publish_user_event(
    redis: Any,
    target_user_id: int | None,
    event_type: str,
    reason: str | None = None,
) -> None:
    """PUBLISH one event to the shared channel. Callable from ANY process — the
    worker passes its own ``aioredis`` client, the API pod passes ``get_redis()``.

    ``target_user_id=None`` routes to the ``_ALL`` bucket (auth-off household /
    admins). Best-effort: a Redis hiccup must never break the ingest/upload path
    that emitted it, so failures are swallowed with a warning.
    """
    try:
        payload = json.dumps({"target": target_user_id, **build_event(event_type, reason)})
        await redis.publish(USER_EVENTS_CHANNEL, payload)
    except Exception as exc:  # noqa: BLE001 — emitting an event is never critical-path
        logger.warning(f"user-events: publish failed ({event_type}/{reason}): {exc}")


async def emit_documents_changed(
    redis: Any,
    *,
    reason: str,
    owner_user_id: int | None = None,
    db: Any = None,
    document: Any = None,
) -> None:
    """Convenience emitter for :data:`EVENT_DOCUMENTS_CHANGED`. Resolves the
    target (owner precedence: explicit ``owner_user_id`` → the document's atom
    owner → ``None``) and publishes. In **auth-off** mode (``ws_auth_enabled``
    False) the target is forced to ``None`` so the single household's ``_ALL``
    sockets receive it. Fully best-effort — emitting an event is never on the
    critical path of the ingest/upload that triggered it."""
    try:
        from utils.config import settings

        owner = owner_user_id
        if owner is None and db is not None and document is not None:
            owner = await resolve_document_owner(db, document)
        target = None if not settings.ws_auth_enabled else owner
        await publish_user_event(redis, target, EVENT_DOCUMENTS_CHANGED, reason)
    except Exception as exc:  # noqa: BLE001 — never let an event break the caller
        logger.warning(f"user-events: emit_documents_changed({reason}) failed: {exc}")


async def resolve_document_owner(db: Any, document: Any) -> int | None:
    """The owning ``user_id`` of a document = the owner of its ``kb_document``
    atom (``Document`` has no direct owner column; ownership lives on ``atoms``).
    ``None`` for an atom-less / null-KB / global-RAG document."""
    atom_id = getattr(document, "atom_id", None)
    if atom_id is None:
        return None
    try:
        from sqlalchemy import select

        from models.database import Atom

        return (
            await db.execute(select(Atom.owner_user_id).where(Atom.atom_id == atom_id))
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — owner lookup failure ⇒ treat as unattributable
        logger.debug(f"user-events: owner lookup failed for atom {atom_id}: {exc}")
        return None


class UserEventRegistry:
    """Per-process map of ``user_id | ALL`` → the set of that user's open sockets.

    Pure and synchronous except :meth:`fan_out`; the WebSocket type is duck-typed
    (``send_json``) so tests can pass a fake. A socket may appear under several
    keys (an admin registers under both its ``user_id`` and ``ALL``); a broken
    send prunes it from **every** key.
    """

    def __init__(self) -> None:
        self._clients: dict[int | str, set[Any]] = {}

    def register(self, key: int | str, ws: Any) -> None:
        self._clients.setdefault(key, set()).add(ws)

    def unregister(self, ws: Any) -> None:
        """Remove a socket from every key it appears under (idempotent)."""
        for key in list(self._clients.keys()):
            bucket = self._clients.get(key)
            if bucket is not None:
                bucket.discard(ws)
                if not bucket:
                    self._clients.pop(key, None)

    def client_count(self) -> int:
        """Distinct connected sockets (a socket under N keys counts once)."""
        seen: set[int] = set()
        for bucket in self._clients.values():
            for ws in bucket:
                seen.add(id(ws))
        return len(seen)

    def _recipients(self, target: int | None) -> list[Any]:
        # target=None → the ALL bucket; target=int → that user's sockets only.
        key: int | str = ALL if target is None else target
        return list(self._clients.get(key, ()))

    async def fan_out(self, target: int | None, event: dict[str, Any]) -> int:
        """Send ``event`` to every recipient of ``target``; prune dead sockets.
        Returns the number of sockets the event was delivered to. No-op (returns
        0) when nobody is listening."""
        recipients = self._recipients(target)
        if not recipients:
            return 0
        results = await asyncio.gather(
            *(self._send_one(ws, event) for ws in recipients), return_exceptions=True
        )
        delivered = 0
        for ws, result in zip(recipients, results):
            if isinstance(result, Exception):
                self.unregister(ws)  # backpressured/closed → drop from all keys
            else:
                delivered += 1
        return delivered

    @staticmethod
    async def _send_one(ws: Any, event: dict[str, Any]) -> None:
        await asyncio.wait_for(ws.send_json(event), timeout=_SEND_TIMEOUT_SECONDS)


class EventCoalescer:
    """Collapse a burst of events for the same ``(target, type)`` into ONE
    delivery per ``window_seconds`` — the folder-ingest-backlog guard (200
    completions ⇒ ~1 fan-out, not 200). ``window_seconds <= 0`` disables
    coalescing (flush immediately). The last event for a key wins (events are
    content-free, so only the routing key matters)."""

    def __init__(self, window_seconds: float, flush) -> None:
        self._window = window_seconds
        self._flush = flush  # async (target, event) -> Any
        self._pending: dict[tuple[int | None, str], dict[str, Any]] = {}
        self._task: asyncio.Task | None = None

    def submit(self, target: int | None, event: dict[str, Any]) -> None:
        if self._window <= 0:
            # No coalescing — schedule an immediate flush of just this event.
            self._pending[(target, event.get("type", ""))] = event
            self._ensure_task()
            return
        self._pending[(target, event.get("type", ""))] = event
        self._ensure_task()

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        if self._window > 0:
            await asyncio.sleep(self._window)
        pending, self._pending = self._pending, {}
        for (target, _type), event in pending.items():
            try:
                await self._flush(target, event)
            except Exception as exc:  # noqa: BLE001 — one bad flush never kills the loop
                logger.warning(f"user-events: coalesced flush failed: {exc}")


# Module-level singleton registry — the API pod's live socket set. The subscriber
# and the WS endpoint share this instance.
_registry = UserEventRegistry()


def get_registry() -> UserEventRegistry:
    return _registry


async def run_user_events_subscriber(
    redis: Any,
    registry: UserEventRegistry | None = None,
    coalesce_window_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Subscribe to :data:`USER_EVENTS_CHANNEL` and fan each event out to the
    local registry (through the coalescer). ONE per API pod, started in the
    lifespan. Resilient: a malformed message is skipped, a Redis drop is
    reconnected with backoff; only an explicit ``stop_event`` ends it."""
    reg = registry or _registry
    coalescer = EventCoalescer(coalesce_window_seconds, reg.fan_out)
    backoff = 1.0
    while stop_event is None or not stop_event.is_set():
        pubsub = None
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(USER_EVENTS_CHANNEL)
            logger.info(f"user-events: subscribed to {USER_EVENTS_CHANNEL}")
            backoff = 1.0  # a healthy subscribe resets the reconnect backoff
            async for message in pubsub.listen():
                if stop_event is not None and stop_event.is_set():
                    break
                if not message or message.get("type") != "message":
                    continue
                data = message.get("data")
                try:
                    parsed = json.loads(data)
                    target = parsed.get("target")
                    event = {k: v for k, v in parsed.items() if k != "target"}
                    if event.get("type"):
                        coalescer.submit(target, event)
                except Exception as exc:  # noqa: BLE001 — skip a bad frame, keep the loop
                    logger.debug(f"user-events: skipping malformed message: {exc}")
            # ``listen()`` returned WITHOUT raising. With real redis this only
            # happens on unsubscribe/close, so treat it as a dropped connection
            # and fall through to the reconnect backoff below — NEVER re-enter
            # ``listen()`` immediately (that busy-loops / starves the event loop
            # when the transport yields nothing).
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — Redis blip → reconnect with backoff
            logger.warning(f"user-events: subscriber error: {exc}")
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001
                    pass
        # Single suspend point covering BOTH a normal listen() return and an
        # error: throttles reconnects and guarantees the loop always yields
        # control (so stop_event / cancellation are honoured).
        if stop_event is None or not stop_event.is_set():
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
