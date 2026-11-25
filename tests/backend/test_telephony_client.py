"""Tests for the telephony client (#1650) against an in-process fake gateway.

Covers: number formatting, the notification mapping (recipients and privacy are
fixed here, never taken from the event), push delivery, resume after reconnect,
epoch change (gateway restart), dedup via done-markers, retry of a failed
delivery, poison events, the auth header, and the dark-by-default lifecycle hook.
"""

import asyncio
import json
from unittest.mock import patch

import pytest
from websockets.asyncio.server import serve

from services import telephony_client as tc

pytestmark = pytest.mark.unit


class MemoryStore:
    def __init__(self, epoch="", seq=0):
        self.epoch, self.seq = epoch, seq
        self.done: set[str] = set()

    async def load(self):
        return self.epoch, self.seq

    async def save(self, epoch, seq):
        self.epoch, self.seq = epoch, seq

    async def is_done(self, key):
        return key in self.done

    async def mark_done(self, key):
        self.done.add(key)


class FakeGateway:
    """Minimal server side of the gateway protocol."""

    def __init__(self, epoch="E1", token="tok"):
        self.epoch = epoch
        self.token = token
        self.events: list[dict] = []
        self.hellos: list[dict] = []
        self.auth_headers: list[str] = []
        self.conns = []
        self._sends: set[asyncio.Future] = set()

    def add(self, **event):
        event["seq"] = len(self.events) + 1
        self.events.append(event)
        for ws in list(self.conns):
            fut = asyncio.ensure_future(ws.send(json.dumps(event)))
            self._sends.add(fut)
            fut.add_done_callback(self._sends.discard)

    def _check(self, connection, request):
        self.auth_headers.append(request.headers.get("Authorization", ""))
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return connection.respond(401, "unauthorized\n")
        return None

    async def handler(self, ws):
        hello = json.loads(await ws.recv())
        self.hellos.append(hello)
        resume = hello["resume_from"] if hello.get("epoch") == self.epoch else 0
        await ws.send(
            json.dumps({"type": "welcome", "instance": "haushalt", "epoch": self.epoch, "last_seq": len(self.events)})
        )
        for e in self.events:
            if e["seq"] > resume:
                await ws.send(json.dumps(e))
        self.conns.append(ws)
        try:
            await ws.wait_closed()
        finally:
            self.conns.remove(ws)

    async def kick_all(self):
        for ws in list(self.conns):
            await ws.close()


@pytest.fixture
async def gateway():
    gw = FakeGateway()
    async with serve(gw.handler, "127.0.0.1", 0, process_request=gw._check) as server:
        gw.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/events"
        yield gw


async def _wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _client(gw, store, delivered, token="tok", fail_first=False, fail_times=0):
    state = {"failed": 0}
    fail_times = fail_times or (1 if fail_first else 0)

    async def deliver(event):
        if state["failed"] < fail_times:
            state["failed"] += 1
            raise RuntimeError("db down")
        delivered.append(event)

    return tc.TelephonyClient(gw.url, token, store, deliver)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(tc.TelephonyClient, "_sleep", staticmethod(lambda s: real_sleep(0.01)))


# --------------------------------------------------------------------------- formatting / mapping


@pytest.mark.parametrize(
    "number,expected",
    [
        ("+491701234567", "01701234567"),
        ("+49301234567", "0301234567"),
        ("+441234567890", "+441234567890"),
        ("anonymous", "Unbekannt"),
        ("", "Unbekannt"),
    ],
)
def test_format_number(number, expected):
    assert tc.format_number(number) == expected


def test_build_notification_never_takes_recipient_or_privacy_from_event():
    event = {
        "type": "incoming_call",
        "call_id": "c1",
        "did": "+49301234567",
        "caller": "+491701234567",
        "target_user_id": 999,
        "privacy": "public",
    }
    kw = tc.build_notification(event)
    assert kw["privacy"] == "personal"
    assert "target_user_id" not in kw
    assert kw["tts"] is False  # no room announcement
    assert kw["dedup"] is False
    assert kw["source"] == "telephony"
    assert kw["event_type"] == "telephony.incoming_call"
    assert kw["title"] == "Anruf von 01701234567 an 0301234567"
    assert kw["message"] == "Eingehender Anruf von 01701234567 an 0301234567"
    assert "target_user_id" not in kw["data"] and "privacy" not in kw["data"]


async def test_notify_users_fans_out_one_personal_notification_per_user():
    calls = []

    async def process(**kw):
        if kw["target_user_id"] == 2:
            raise ValueError("suppressed by feedback rule")
        calls.append(kw)

    event = {"type": "voicemail", "call_id": "v", "did": "d", "caller": "+491701234567", "duration_s": 5}
    assert await tc.notify_users(event, [1, 2, 3], process) == 2
    assert [c["target_user_id"] for c in calls] == [1, 3]
    assert all(c["privacy"] == "personal" and c["tts"] is False for c in calls)


async def test_notify_users_other_errors_propagate():
    async def process(**kw):
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await tc.notify_users({"type": "incoming_call", "call_id": "c"}, [1], process)


# --------------------------------------------------------------------------- push / resume / exactly-once


async def test_push_delivers_incoming_call_only_and_sends_token(gateway):
    store, delivered = MemoryStore(), []
    task = asyncio.create_task(_client(gateway, store, delivered).run())
    try:
        await _wait_for(lambda: gateway.conns)
        gateway.add(type="incoming_call", call_id="c1", did="+49301234567", caller="+491701234567")
        gateway.add(type="call_ended", call_id="c1", cause="Normal Clearing")
        await _wait_for(lambda: store.seq == 2)
        assert [e["call_id"] for e in delivered] == ["c1"]
        assert gateway.auth_headers[0] == "Bearer tok"
        assert gateway.hellos[0] == {"type": "hello", "epoch": "", "resume_from": 0}
    finally:
        task.cancel()


async def test_resume_after_reconnect_replays_only_missed(gateway):
    gateway.add(type="incoming_call", call_id="old", did="d", caller="x")
    store, delivered = MemoryStore(epoch="E1", seq=1), []
    task = asyncio.create_task(_client(gateway, store, delivered).run())
    try:
        await _wait_for(lambda: gateway.conns)
        assert gateway.hellos[0]["resume_from"] == 1
        await gateway.kick_all()
        gateway.add(type="incoming_call", call_id="missed", did="d", caller="x")  # while disconnected
        await _wait_for(lambda: len(gateway.hellos) >= 2 and store.seq == 2)
        assert [e["call_id"] for e in delivered] == ["missed"]
    finally:
        task.cancel()


async def test_new_epoch_resets_position(gateway):
    gateway.add(type="incoming_call", call_id="fresh", did="d", caller="x")  # seq 1 of the NEW epoch
    store, delivered = MemoryStore(epoch="OLD", seq=42), []
    task = asyncio.create_task(_client(gateway, store, delivered).run())
    try:
        await _wait_for(lambda: delivered)
        assert delivered[0]["call_id"] == "fresh"
        assert (store.epoch, store.seq) == ("E1", 1)
    finally:
        task.cancel()


async def test_claimed_call_is_not_announced_twice(gateway):
    gateway.add(type="incoming_call", call_id="dup", did="d", caller="x")
    store, delivered = MemoryStore(), []
    store.done.add("incoming_call:dup")  # e.g. another pod / earlier run already announced it
    task = asyncio.create_task(_client(gateway, store, delivered).run())
    try:
        await _wait_for(lambda: store.seq == 1)
        assert delivered == []
    finally:
        task.cancel()


async def test_failed_delivery_is_retried(gateway):
    gateway.add(type="incoming_call", call_id="c9", did="d", caller="x")
    store, delivered = MemoryStore(), []
    task = asyncio.create_task(_client(gateway, store, delivered, fail_first=True).run())
    try:
        await _wait_for(lambda: delivered)
        assert [e["call_id"] for e in delivered] == ["c9"]
        assert len(gateway.hellos) >= 2  # reconnected and got the replay
    finally:
        task.cancel()


async def test_wrong_token_never_delivers(gateway):
    store, delivered = MemoryStore(), []
    gateway.add(type="incoming_call", call_id="c1", did="d", caller="x")
    task = asyncio.create_task(_client(gateway, store, delivered, token="falsch").run())
    try:
        await _wait_for(lambda: len(gateway.auth_headers) >= 2)
        assert delivered == [] and gateway.hellos == []
    finally:
        task.cancel()


# --------------------------------------------------------------------------- startup guards


async def test_refuses_wss_without_pinned_cert():
    with (
        patch.object(tc.settings, "telephony_gateway_url", "wss://gateway.invalid:8090/v1/events"),
        patch.object(tc.settings, "telephony_gateway_token", tc.settings.telephony_gateway_token.__class__("t")),
        patch.object(tc.settings, "telephony_gateway_ca_pem", ""),
        patch.object(tc.TelephonyClient, "run") as run,
    ):
        await tc.run_telephony_client()
    run.assert_not_called()


async def test_refuses_plain_ws_to_a_non_loopback_host():
    with (
        patch.object(tc.settings, "telephony_gateway_url", "ws://gateway.invalid:8090/v1/events"),
        patch.object(tc.settings, "telephony_gateway_token", tc.settings.telephony_gateway_token.__class__("t")),
        patch.object(tc.TelephonyClient, "run") as run,
    ):
        await tc.run_telephony_client()
    run.assert_not_called()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("ws://127.0.0.1:1/x", True),
        ("ws://localhost:1/x", True),
        ("ws://[::1]:1/x", True),
        ("ws://10.0.0.1/x", False),
        ("ws://gateway.invalid/x", False),
    ],
)
def test_is_loopback(url, expected):
    assert tc._is_loopback(url) is expected


async def test_malformed_frame_is_skipped_not_a_reconnect_loop(gateway):
    store, delivered = MemoryStore(), []
    task = asyncio.create_task(_client(gateway, store, delivered).run())
    try:
        await _wait_for(lambda: gateway.conns)
        for ws in gateway.conns:
            await ws.send("kein json")
            await ws.send(json.dumps({"type": "incoming_call", "call_id": "x"}))  # no seq
        gateway.add(type="incoming_call", call_id="ok", did="d", caller="x")
        await _wait_for(lambda: delivered)
        assert [e["call_id"] for e in delivered] == ["ok"]
        assert len(gateway.hellos) == 1  # stayed connected
    finally:
        task.cancel()


async def test_refuses_without_url_or_token():
    with patch.object(tc.settings, "telephony_gateway_url", ""), patch.object(tc.TelephonyClient, "run") as run:
        await tc.run_telephony_client()
    run.assert_not_called()


def test_lifecycle_flag_off_schedules_nothing():
    from api import lifecycle

    before = len(lifecycle._startup_tasks)
    with patch.object(lifecycle.settings, "telephony_enabled", False):
        lifecycle._schedule_telephony_client()
    assert len(lifecycle._startup_tasks) == before


def test_build_notification_voicemail():
    kw = tc.build_notification(
        {"type": "voicemail", "call_id": "v1", "did": "+49301234568", "caller": "+491701234567", "duration_s": 83}
    )
    assert kw["event_type"] == "telephony.voicemail"
    assert kw["title"] == "Neue Nachricht von 01701234567 an 0301234568"
    assert kw["message"].endswith("(1:23)")
    assert kw["data"]["duration_s"] == 83


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0:00"), (5, "0:05"), (180, "3:00"), ("4.14", "0:04"), ("x", "0:00"), (-3, "0:00"), (None, "0:00")],
)
def test_format_duration(value, expected):
    assert tc.format_duration(value) == expected


async def test_call_and_voicemail_of_same_call_are_both_announced_once(gateway):
    gateway.add(type="incoming_call", call_id="c1", did="d", caller="x")
    gateway.add(type="voicemail", call_id="c1", did="d", caller="x", duration_s=12)
    gateway.add(type="call_ended", call_id="c1", cause="Normal Clearing")
    store, delivered = MemoryStore(), []
    task = asyncio.create_task(_client(gateway, store, delivered).run())
    try:
        await _wait_for(lambda: store.seq == 3)
        assert [e["type"] for e in delivered] == ["incoming_call", "voicemail"]
        await gateway.kick_all()  # replay from an old position must not re-announce
        store.seq = 0
        await _wait_for(lambda: len(gateway.hellos) >= 2 and store.seq == 3)
        assert len(delivered) == 2
    finally:
        task.cancel()


# --------------------------------------------------------------------------- NotificationService(dedup=False)


class _Reached(Exception):
    pass


@pytest.mark.parametrize("dedup,expect", [(True, ValueError), (False, _Reached)])
async def test_process_webhook_dedup_flag(dedup, expect):
    """dedup=False skips hash + semantic duplicate checks; feedback suppression still runs."""
    from services.notification_service import NotificationService

    svc = NotificationService(db=None)
    with (
        patch.object(svc, "_is_duplicate", return_value=True) as is_dup,
        patch.object(svc, "_get_embedding", return_value=[0.1]),
        patch.object(svc, "_is_semantic_duplicate", return_value=True) as is_sem,
        patch.object(svc, "_is_suppressed", side_effect=_Reached),
        patch.object(tc.settings, "proactive_semantic_dedup_enabled", True),
    ):
        with pytest.raises(expect):
            await svc.process_webhook(event_type="t", title="a", message="b", dedup=dedup)
    assert is_dup.called is dedup
    assert not is_sem.called


async def test_event_failing_every_time_is_dropped_and_does_not_block_the_next(gateway):
    gateway.add(type="incoming_call", call_id="poison", did="d", caller="x")
    gateway.add(type="incoming_call", call_id="next", did="d", caller="x")
    store, delivered = MemoryStore(), []
    task = asyncio.create_task(_client(gateway, store, delivered, fail_times=tc.MAX_ATTEMPTS).run())
    try:
        await _wait_for(lambda: delivered)
        assert [e["call_id"] for e in delivered] == ["next"]
        assert "incoming_call:poison" in store.done and store.seq == 2
    finally:
        task.cancel()


async def test_implausible_seq_is_skipped_and_does_not_silence_later_events(gateway):
    store, delivered = MemoryStore(), []
    task = asyncio.create_task(_client(gateway, store, delivered).run())
    try:
        await _wait_for(lambda: gateway.conns)
        for ws in gateway.conns:
            await ws.send(json.dumps({"type": "incoming_call", "call_id": "forged", "seq": 2**62}))
        gateway.add(type="incoming_call", call_id="real", did="d", caller="x")
        await _wait_for(lambda: delivered)
        assert [e["call_id"] for e in delivered] == ["real"] and store.seq == 1
    finally:
        task.cancel()


def test_untrusted_caller_text_never_reaches_the_notification():
    kw = tc.build_notification(
        {"type": "voicemail", "call_id": "c" * 500, "caller": "<b>" + "9" * 300, "did": "x", "duration_s": "1e400"}
    )
    assert kw["title"] == "Neue Nachricht von Unbekannt"  # invalid DID: no "an" part
    assert kw["data"]["caller"] is None and kw["data"]["did"] is None
    assert len(kw["data"]["call_id"]) == 64 and kw["data"]["duration_s"] == 0


async def test_invalid_ca_pem_is_logged_not_a_crash():
    with (
        patch.object(tc.settings, "telephony_gateway_url", "wss://gateway.invalid:8090/v1/events"),
        patch.object(tc.settings, "telephony_gateway_token", tc.settings.telephony_gateway_token.__class__("t")),
        patch.object(
            tc.settings, "telephony_gateway_ca_pem", "-----BEGIN CERTIFICATE-----\nkaputt\n-----END CERTIFICATE-----\n"
        ),
        patch.object(tc.TelephonyClient, "run") as run,
    ):
        await tc.run_telephony_client()
    run.assert_not_called()


async def test_silent_gateway_times_out_waiting_for_welcome(monkeypatch):
    monkeypatch.setattr(tc, "WELCOME_TIMEOUT_SECONDS", 0.05)

    async def mute(ws):
        await ws.wait_closed()

    async with serve(mute, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/events"
        client = tc.TelephonyClient(url, "tok", MemoryStore(), lambda e: None)
        with pytest.raises(TimeoutError):
            await client._session()
