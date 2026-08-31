"""Unit tests for the per-user event substrate (services/user_events.py) and the
/ws/user endpoint. Covers the delivery model (target vs _ALL), fan-out pruning,
coalescing, publish payload, owner resolution + the auth-off target rule, the
subscriber loop, and endpoint auth/registration.

Run on the .159 build box (CI non-functional): memory/reference_test_runner_159.md
"""
from __future__ import annotations

import asyncio
import json

import pytest

import services.user_events as ue

pytestmark = [pytest.mark.backend, pytest.mark.asyncio]


# --------------------------------------------------------------------------- fakes
class FakeWS:
    """Duck-typed WebSocket: records sent events; can be made to fail sends."""

    def __init__(self, *, fail: bool = False, hang: bool = False):
        self.sent: list[dict] = []
        self._fail = fail
        self._hang = hang

    async def send_json(self, event):
        if self._hang:
            await asyncio.sleep(10)  # exceeds the send timeout
        if self._fail:
            raise RuntimeError("socket broken")
        self.sent.append(event)


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, payload):
        self.published.append((channel, payload))


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDB:
    def __init__(self, owner):
        self._owner = owner

    async def execute(self, *_a, **_k):
        return FakeResult(self._owner)


# --------------------------------------------------------------------------- build_event / publish
async def test_build_event_is_content_free():
    assert ue.build_event("documents_changed") == {"type": "documents_changed"}
    assert ue.build_event("documents_changed", "ingested") == {
        "type": "documents_changed",
        "reason": "ingested",
    }


async def test_publish_user_event_payload():
    r = FakeRedis()
    await ue.publish_user_event(r, 42, "documents_changed", "ingested")
    assert len(r.published) == 1
    channel, payload = r.published[0]
    assert channel == ue.USER_EVENTS_CHANNEL
    assert json.loads(payload) == {"target": 42, "type": "documents_changed", "reason": "ingested"}


async def test_publish_user_event_none_target():
    r = FakeRedis()
    await ue.publish_user_event(r, None, "documents_changed")
    assert json.loads(r.published[0][1]) == {"target": None, "type": "documents_changed"}


async def test_publish_user_event_swallows_redis_error():
    class Boom:
        async def publish(self, *_a):
            raise RuntimeError("redis down")

    # Must not raise — emitting an event is never critical-path.
    await ue.publish_user_event(Boom(), 1, "documents_changed")


# --------------------------------------------------------------------------- registry / fan_out
async def test_fan_out_targets_user_only_not_all():
    reg = ue.UserEventRegistry()
    user_ws, other_ws, all_ws = FakeWS(), FakeWS(), FakeWS()
    reg.register(7, user_ws)
    reg.register(8, other_ws)
    reg.register(ue.ALL, all_ws)

    delivered = await reg.fan_out(7, {"type": "documents_changed"})

    assert delivered == 1
    assert user_ws.sent == [{"type": "documents_changed"}]
    assert other_ws.sent == []   # a different user is untouched
    assert all_ws.sent == []     # a targeted event does NOT spam the ALL bucket


async def test_fan_out_none_target_hits_all_bucket():
    reg = ue.UserEventRegistry()
    all_ws, user_ws = FakeWS(), FakeWS()
    reg.register(ue.ALL, all_ws)
    reg.register(7, user_ws)

    delivered = await reg.fan_out(None, {"type": "documents_changed"})

    assert delivered == 1
    assert all_ws.sent == [{"type": "documents_changed"}]
    assert user_ws.sent == []


async def test_fan_out_all_of_a_users_tabs():
    reg = ue.UserEventRegistry()
    tab1, tab2 = FakeWS(), FakeWS()
    reg.register(7, tab1)
    reg.register(7, tab2)
    assert await reg.fan_out(7, {"type": "x"}) == 2


async def test_fan_out_empty_is_noop():
    reg = ue.UserEventRegistry()
    assert await reg.fan_out(99, {"type": "x"}) == 0


async def test_fan_out_prunes_broken_socket():
    reg = ue.UserEventRegistry()
    good, bad = FakeWS(), FakeWS(fail=True)
    reg.register(7, good)
    reg.register(7, bad)

    delivered = await reg.fan_out(7, {"type": "x"})

    assert delivered == 1          # only the good one
    assert good.sent == [{"type": "x"}]
    assert reg.client_count() == 1  # the broken socket was pruned from all keys


async def test_fan_out_prunes_hung_socket_via_timeout(monkeypatch):
    monkeypatch.setattr(ue, "_SEND_TIMEOUT_SECONDS", 0.05)
    reg = ue.UserEventRegistry()
    good, hung = FakeWS(), FakeWS(hang=True)
    reg.register(7, good)
    reg.register(7, hung)

    delivered = await reg.fan_out(7, {"type": "x"})

    assert delivered == 1
    assert reg.client_count() == 1


async def test_unregister_removes_from_all_keys():
    reg = ue.UserEventRegistry()
    ws = FakeWS()
    reg.register(7, ws)
    reg.register(ue.ALL, ws)  # e.g. an admin under both keys
    assert reg.client_count() == 1
    reg.unregister(ws)
    assert reg.client_count() == 0
    assert await reg.fan_out(7, {"type": "x"}) == 0
    assert await reg.fan_out(None, {"type": "x"}) == 0


# --------------------------------------------------------------------------- coalescer
async def test_coalescer_collapses_burst():
    flushed: list[tuple] = []

    async def flush(target, event):
        flushed.append((target, event))

    c = ue.EventCoalescer(0.05, flush)
    for _ in range(200):
        c.submit(7, {"type": "documents_changed", "reason": "ingested"})
    await asyncio.sleep(0.12)

    assert flushed == [(7, {"type": "documents_changed", "reason": "ingested"})]  # 200 → 1


async def test_coalescer_separate_keys_not_merged():
    flushed: list[tuple] = []

    async def flush(target, event):
        flushed.append((target, event.get("type")))

    c = ue.EventCoalescer(0.05, flush)
    c.submit(7, {"type": "documents_changed"})
    c.submit(8, {"type": "documents_changed"})   # different target
    c.submit(7, {"type": "notes_changed"})       # different type, same target
    await asyncio.sleep(0.12)

    assert sorted(flushed) == sorted([(7, "documents_changed"), (8, "documents_changed"), (7, "notes_changed")])


async def test_coalescer_zero_window_flushes_each():
    flushed: list = []

    async def flush(target, event):
        flushed.append(target)

    c = ue.EventCoalescer(0.0, flush)
    c.submit(1, {"type": "x"})
    await asyncio.sleep(0.01)
    c.submit(2, {"type": "x"})
    await asyncio.sleep(0.01)
    assert flushed == [1, 2]


async def test_coalescer_flush_error_does_not_crash():
    calls: list = []

    async def flush(target, _event):
        calls.append(target)
        raise RuntimeError("flush boom")

    c = ue.EventCoalescer(0.02, flush)
    c.submit(1, {"type": "x"})
    c.submit(2, {"type": "y"})
    await asyncio.sleep(0.06)
    assert sorted(calls) == [1, 2]  # a raising flush for one key never blocks the other


# --------------------------------------------------------------------------- owner resolution + emit
async def test_resolve_document_owner_from_atom():
    class Doc:
        atom_id = "abc"

    assert await ue.resolve_document_owner(FakeDB(owner=55), Doc()) == 55


async def test_resolve_document_owner_none_when_atomless():
    class Doc:
        atom_id = None

    assert await ue.resolve_document_owner(FakeDB(owner=55), Doc()) is None


async def test_resolve_document_owner_swallows_db_error():
    class BoomDB:
        async def execute(self, *_a, **_k):
            raise RuntimeError("db down")

    class Doc:
        atom_id = "abc"

    assert await ue.resolve_document_owner(BoomDB(), Doc()) is None


async def test_emit_prefers_explicit_owner(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "ws_auth_enabled", True)
    r = FakeRedis()
    await ue.emit_documents_changed(r, reason="ingested", owner_user_id=9)
    assert json.loads(r.published[0][1])["target"] == 9


async def test_emit_resolves_owner_from_document(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "ws_auth_enabled", True)

    class Doc:
        atom_id = "abc"

    r = FakeRedis()
    await ue.emit_documents_changed(r, reason="paperless", db=FakeDB(owner=33), document=Doc())
    assert json.loads(r.published[0][1])["target"] == 33


async def test_emit_auth_off_forces_none_target(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "ws_auth_enabled", False)
    r = FakeRedis()
    # Even with an explicit owner, auth-off routes to the household _ALL bucket.
    await ue.emit_documents_changed(r, reason="ingested", owner_user_id=9)
    assert json.loads(r.published[0][1])["target"] is None


# --------------------------------------------------------------------------- subscriber
class FakePubSub:
    """Yields the seeded messages, then BLOCKS (like real redis PubSub.listen(),
    which suspends waiting for the next message and only returns on close). This
    keeps the subscriber suspended — never a busy-loop — until it is cancelled."""

    def __init__(self, messages, raise_on_listen: Exception | None = None, block: bool = True):
        self._messages = messages
        self._raise = raise_on_listen
        self._block = block
        self.subscribed = None
        self.closed = False

    async def subscribe(self, channel):
        self.subscribed = channel

    async def listen(self):
        if self._raise is not None:
            raise self._raise
        for m in self._messages:
            yield m
        if self._block:
            await asyncio.Event().wait()  # block for "the next message" (real redis)
        # block=False → listen() RETURNS normally (the busy-loop-regression path)

    async def aclose(self):
        self.closed = True


class FakeRedisWithPubSub:
    """Hands out the given pubsub objects in order (one per (re)connect)."""

    def __init__(self, *pubsubs):
        self._pubsubs = list(pubsubs)

    def pubsub(self):
        return self._pubsubs.pop(0) if len(self._pubsubs) > 1 else self._pubsubs[0]


async def _run_subscriber_until(reg, redis, *, settle=0.1):
    stop = asyncio.Event()
    task = asyncio.create_task(
        ue.run_user_events_subscriber(redis, registry=reg, coalesce_window_seconds=0.0, stop_event=stop)
    )
    await asyncio.sleep(settle)
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_subscriber_fans_out_valid_message():
    reg = ue.UserEventRegistry()
    ws = FakeWS()
    reg.register(7, ws)
    msgs = [
        {"type": "subscribe"},  # non-message frame ignored
        {"type": "message", "data": json.dumps({"target": 7, "type": "documents_changed", "reason": "ingested"})},
    ]
    await _run_subscriber_until(reg, FakeRedisWithPubSub(FakePubSub(msgs)))
    assert ws.sent == [{"type": "documents_changed", "reason": "ingested"}]


async def test_subscriber_none_target_reaches_all_bucket():
    reg = ue.UserEventRegistry()
    ws = FakeWS()
    reg.register(ue.ALL, ws)
    msgs = [{"type": "message", "data": json.dumps({"target": None, "type": "documents_changed"})}]
    await _run_subscriber_until(reg, FakeRedisWithPubSub(FakePubSub(msgs)))
    assert ws.sent == [{"type": "documents_changed"}]


async def test_subscriber_skips_malformed_message():
    reg = ue.UserEventRegistry()
    ws = FakeWS()
    reg.register(7, ws)
    msgs = [
        {"type": "message", "data": "not json{"},
        {"type": "message", "data": json.dumps({"target": 7, "type": "documents_changed"})},
    ]
    await _run_subscriber_until(reg, FakeRedisWithPubSub(FakePubSub(msgs)))
    assert ws.sent == [{"type": "documents_changed"}]  # bad frame skipped, good one delivered


async def test_subscriber_reconnects_after_error():
    # First connect's listen() raises → the subscriber logs, backs off (~1s), and
    # reconnects to the second pubsub, which then delivers. settle > the 1.0s
    # initial backoff so the reconnect completes before we stop it.
    reg = ue.UserEventRegistry()
    ws = FakeWS()
    reg.register(7, ws)
    bad = FakePubSub([], raise_on_listen=RuntimeError("connection dropped"))
    good = FakePubSub([{"type": "message", "data": json.dumps({"target": 7, "type": "documents_changed"})}])
    await _run_subscriber_until(reg, FakeRedisWithPubSub(bad, good), settle=1.3)
    assert bad.closed  # first (failed) pubsub was cleaned up
    assert ws.sent == [{"type": "documents_changed"}]  # reconnected + delivered


async def test_subscriber_normal_listen_return_reconnects_not_busyloop():
    # Regression guard for the busy-loop fix: a listen() that RETURNS normally
    # (block=False) must NOT be re-entered immediately — the subscriber backs off
    # then reconnects. If the busy-loop regressed, this test would hang.
    reg = ue.UserEventRegistry()
    ws = FakeWS()
    reg.register(7, ws)
    empty = FakePubSub([], block=False)  # listen() returns at once, no messages
    good = FakePubSub([{"type": "message", "data": json.dumps({"target": 7, "type": "documents_changed"})}])
    await _run_subscriber_until(reg, FakeRedisWithPubSub(empty, good), settle=1.3)
    assert empty.closed
    assert ws.sent == [{"type": "documents_changed"}]  # backed off, reconnected, delivered


# --------------------------------------------------------------------------- _is_admin helper
class _FakeSessionCtx:
    async def __aenter__(self):
        return "db"

    async def __aexit__(self, *_a):
        return False


def _fake_session_local():
    return _FakeSessionCtx()


async def test_is_admin_true_false_and_error(monkeypatch):
    import api.websocket.user_events_handler as handler
    import services.auth_service as auth_service
    import services.database as database

    monkeypatch.setattr(database, "AsyncSessionLocal", _fake_session_local)

    async def _admins(_db):
        return {1, 5}

    monkeypatch.setattr(auth_service, "active_admin_ids", _admins)
    assert await handler._is_admin(1) is True
    assert await handler._is_admin(2) is False

    async def _boom(_db):
        raise RuntimeError("db down")

    monkeypatch.setattr(auth_service, "active_admin_ids", _boom)
    assert await handler._is_admin(1) is False  # swallowed → treated as not admin
