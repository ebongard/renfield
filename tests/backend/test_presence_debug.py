"""Presence diagnostics: the payload builder, the ``/ws/presence-debug`` gate
and the coalesced push.

  * builder — redaction preserved, the decision settings present, the roster of
    connected satellites carried (so "not heard" is tellable), disabled shape;
  * gate — ADMIN only; unauthenticated, non-admin and a failed lookup refuse;
  * push — no viewer means no work at all; a burst of reports collapses to one
    snapshot per interval; a broken viewer is dropped, not raised.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import ha_glue.api.websocket.presence_debug_handler as handler
import ha_glue.services.presence_debug as hub
from ha_glue.services.presence_service import (
    DeviceSighting,
    PresenceService,
    UserPresence,
)

RAW_MAC = "AA:BB:CC:DD:EE:01"
IRK_LABEL = "somebodys-phone"


@pytest.fixture(autouse=True)
def _reset_hub():
    def reset():
        if hub._flush_task is not None and not hub._flush_task.done():
            hub._flush_task.cancel()
        hub._debug_clients.clear()
        hub._dirty = False
        hub._flush_task = None
        hub._last_push_monotonic = 0.0

    reset()
    yield
    reset()


@pytest.fixture
def service():
    svc = PresenceService()
    svc._mac_to_user = {RAW_MAC: 1}
    svc._irk_label_to_user = {IRK_LABEL: 2}
    svc._user_names = {1: "alice", 2: "bob"}
    svc._room_names = {0: "Hall", 10: "Kitchen"}
    svc._sightings = {
        RAW_MAC: [
            DeviceSighting("sat-kitchen", 10, -55, 1000.0),
            DeviceSighting("sat-hall", 0, -70, 1001.0),
        ],
        "irk:" + IRK_LABEL: [DeviceSighting("sat-hall", 0, -60, 1002.0)],
    }
    svc._presence = {
        1: UserPresence(
            user_id=1, room_id=10, room_name="Kitchen", confidence=0.71234,
            room_rssi_filtered={10: -56.123, 0: -71.0},
        ),
    }
    return svc


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg):
        self.sent.append(msg)


class _BrokenWS:
    async def send_json(self, msg):
        raise RuntimeError("socket gone")


# --------------------------------------------------------------------------
# Payload builder
# --------------------------------------------------------------------------


@pytest.mark.backend
@pytest.mark.unit
class TestPayloadBuilder:
    def test_never_leaks_the_mac_or_the_irk_label(self, service, monkeypatch):
        monkeypatch.setattr(hub, "_connected_satellites", lambda: [])
        payload = hub.build_sightings_payload(service)
        wire = json.dumps(payload)
        assert RAW_MAC not in wire
        assert IRK_LABEL not in wire
        keys = sorted(d["key"] for d in payload["devices"])
        assert keys[0].startswith("irk:") and keys[1].startswith("mac:")

    def test_carries_the_numbers_needed_to_read_it(self, service, monkeypatch):
        monkeypatch.setattr(hub, "_connected_satellites", lambda: [])
        monkeypatch.setattr(hub.ha_glue_settings, "presence_rssi_threshold", -77)
        monkeypatch.setattr(hub.ha_glue_settings, "presence_switch_enter_margin_db", 6.5)
        monkeypatch.setattr(hub.ha_glue_settings, "presence_stale_timeout", 90)
        monkeypatch.setattr(hub.ha_glue_settings, "presence_rssi_filter_enabled", True)
        payload = hub.build_sightings_payload(service)
        assert payload["settings"] == {
            "rssi_floor_dbm": -77,
            "switch_enter_margin_db": 6.5,
            "stale_timeout_s": 90,
            "rssi_filter_enabled": True,
        }
        assert payload["enabled"] is True

    def test_device_shape(self, service, monkeypatch):
        monkeypatch.setattr(hub, "_connected_satellites", lambda: [])
        payload = hub.build_sightings_payload(service)
        dev = next(d for d in payload["devices"] if d["key"].startswith("mac:"))
        assert dev["user_name"] == "alice"
        assert dev["current_room_id"] == 10
        assert dev["current_room_name"] == "Kitchen"
        assert dev["current_confidence"] == 0.712
        # The historical name-keyed map stays; the id-keyed list is additive.
        assert dev["filtered_by_room"] == {"Kitchen": -56.12, "Hall": -71.0}
        assert {"room_id": 10, "room_name": "Kitchen", "rssi": -56.12} in dev["filtered_rooms"]
        # room_id 0 is a room, not "no room".
        assert {"room_id": 0, "room_name": "Hall", "rssi": -71.0} in dev["filtered_rooms"]
        sats = {s["satellite_id"]: s for s in dev["sightings"]}
        assert sats["sat-kitchen"]["rssi"] == -55
        assert sats["sat-hall"]["room_name"] == "Hall"
        assert sats["sat-hall"]["timestamp"] == 1001.0
        assert sats["sat-hall"]["age_s"] == round(payload["now"] - 1001.0, 2)

    def test_device_without_a_placement(self, service, monkeypatch):
        monkeypatch.setattr(hub, "_connected_satellites", lambda: [])
        payload = hub.build_sightings_payload(service)
        dev = next(d for d in payload["devices"] if d["key"].startswith("irk:"))
        assert dev["user_name"] == "bob"
        assert dev["current_room_id"] is None
        assert dev["filtered_rooms"] == []

    def test_roster_lists_connected_satellites(self, service, monkeypatch):
        import ha_glue.services.satellite_manager as sm

        manager = SimpleNamespace(satellites={
            "sat-kitchen": SimpleNamespace(room="Kitchen", room_id=10),
            "sat-attic": SimpleNamespace(room="Attic", room_id=None),
        })
        monkeypatch.setattr(sm, "get_satellite_manager", lambda: manager)
        payload = hub.build_sightings_payload(service)
        assert payload["satellites"] == [
            {"satellite_id": "sat-kitchen", "room_id": 10, "room_name": "Kitchen"},
            {"satellite_id": "sat-attic", "room_id": None, "room_name": "Attic"},
        ]

    def test_a_broken_roster_does_not_fail_the_payload(self, service, monkeypatch):
        import ha_glue.services.satellite_manager as sm

        monkeypatch.setattr(
            sm, "get_satellite_manager", MagicMock(side_effect=RuntimeError("down"))
        )
        payload = hub.build_sightings_payload(service)
        assert payload["satellites"] == []
        assert len(payload["devices"]) == 2

    def test_disabled_shape(self):
        payload = hub.build_sightings_payload(None)
        assert payload["enabled"] is False
        assert payload["devices"] == []
        assert payload["satellites"] == []
        assert "rssi_floor_dbm" in payload["settings"]

    def test_current_payload_honours_the_kill_switch(self, service, monkeypatch):
        import ha_glue.services.presence_service as ps

        monkeypatch.setattr(ps, "get_presence_service", lambda: service)
        monkeypatch.setattr(hub, "_connected_satellites", lambda: [])
        monkeypatch.setattr(hub.ha_glue_settings, "presence_enabled", False)
        assert hub.build_current_payload()["devices"] == []
        monkeypatch.setattr(hub.ha_glue_settings, "presence_enabled", True)
        assert len(hub.build_current_payload()["devices"]) == 2

    @pytest.mark.asyncio
    async def test_rest_route_and_socket_share_the_builder(self, service, monkeypatch):
        import ha_glue.api.routes.presence as route

        monkeypatch.setattr(hub, "_connected_satellites", lambda: [])
        monkeypatch.setattr(hub.time, "time", lambda: 1010.0)
        monkeypatch.setattr(route.ha_glue_settings, "presence_enabled", True)
        monkeypatch.setattr(route, "get_presence_service", lambda: service)
        rest = await route.get_debug_sightings(current_user=MagicMock())
        assert rest == hub.build_sightings_payload(service)


# --------------------------------------------------------------------------
# Socket gate
# --------------------------------------------------------------------------


class _GateWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed_with: dict | None = None
        self.accepted = False
        self.registered_during_receive: bool | None = None

    async def accept(self):
        self.accepted = True

    async def send_json(self, msg):
        self.sent.append(msg)

    async def receive_text(self):
        from fastapi import WebSocketDisconnect

        self.registered_during_receive = self in hub._debug_clients
        raise WebSocketDisconnect()

    async def close(self, **kwargs):
        self.closed_with = kwargs


def _user_with(permissions: list[str]):
    """A real User+Role pair — the gate resolves through Role.has_permission,
    so a mock would prove nothing about the permission semantics."""
    from models.database import Role, User

    usr = User(username="viewer", password_hash="x", role_id=1)
    usr.role = Role(name="role", permissions=permissions, is_system=False)
    return usr


async def _run_gate(monkeypatch, *, auth_result, user):
    monkeypatch.setattr(
        handler, "authenticate_websocket", AsyncMock(return_value=auth_result)
    )
    monkeypatch.setattr(handler, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(
        hub, "build_current_payload", lambda: {"now": 1.0, "devices": []}
    )
    ws = _GateWS()
    await handler.presence_debug_live(ws, token="t")
    return ws


@pytest.mark.backend
@pytest.mark.asyncio
class TestSocketGate:
    async def test_an_admin_gets_a_snapshot_and_is_registered(self, monkeypatch):
        ws = await _run_gate(
            monkeypatch,
            auth_result={"authenticated": True, "user_id": 1},
            user=_user_with(["admin"]),
        )
        assert ws.accepted is True
        assert ws.sent == [{"type": "snapshot", "now": 1.0, "devices": []}]
        assert ws.registered_during_receive is True
        # ... and gone again after the disconnect.
        assert hub.viewer_count() == 0

    async def test_the_default_family_role_is_refused(self, monkeypatch):
        from models.permissions import DEFAULT_ROLES

        family = next(r["permissions"] for r in DEFAULT_ROLES if r["name"] == "Familie")
        ws = await _run_gate(
            monkeypatch,
            auth_result={"authenticated": True, "user_id": 2},
            user=_user_with(list(family)),
        )
        assert ws.accepted is False
        assert ws.sent == []
        assert ws.closed_with is not None

    async def test_rooms_manage_alone_is_not_enough(self, monkeypatch):
        ws = await _run_gate(
            monkeypatch,
            auth_result={"authenticated": True, "user_id": 2},
            user=_user_with(["rooms.manage", "kiosk.view"]),
        )
        assert ws.accepted is False

    async def test_unauthenticated_is_refused(self, monkeypatch):
        ws = await _run_gate(monkeypatch, auth_result=None, user=None)
        assert ws.accepted is False
        assert ws.sent == []

    async def test_a_token_without_a_user_is_refused(self, monkeypatch):
        ws = await _run_gate(
            monkeypatch, auth_result={"authenticated": True}, user=_user_with(["admin"])
        )
        assert ws.accepted is False

    async def test_an_unknown_user_is_refused(self, monkeypatch):
        ws = await _run_gate(
            monkeypatch, auth_result={"authenticated": True, "user_id": 9}, user=None
        )
        assert ws.accepted is False

    async def test_a_lookup_failure_denies(self, monkeypatch):
        monkeypatch.setattr(
            handler, "authenticate_websocket",
            AsyncMock(return_value={"authenticated": True, "user_id": 1}),
        )
        monkeypatch.setattr(
            handler, "get_user_by_id", AsyncMock(side_effect=RuntimeError("db down"))
        )
        ws = _GateWS()
        await handler.presence_debug_live(ws, token="t")
        assert ws.accepted is False
        assert ws.sent == []

    async def test_auth_off_opens_like_the_rest_route(self, monkeypatch):
        ws = await _run_gate(
            monkeypatch,
            auth_result={"authenticated": True, "auth_skipped": True},
            user=None,
        )
        assert ws.accepted is True


# --------------------------------------------------------------------------
# Push: no viewer → no work; coalescing
# --------------------------------------------------------------------------


@pytest.mark.backend
@pytest.mark.asyncio
class TestPush:
    async def test_no_viewer_means_no_work(self, monkeypatch):
        build = MagicMock()
        monkeypatch.setattr(hub, "build_current_payload", build)
        hub.note_ble_report()
        await asyncio.sleep(0)
        assert hub._flush_task is None
        assert hub._dirty is False
        build.assert_not_called()

    async def test_a_report_through_the_service_builds_nothing_unwatched(self, monkeypatch):
        """The hook sits in process_ble_report — prove the real call path stays
        free when nobody watches, and pushes when somebody does."""
        import ha_glue.services.presence_service as ps

        svc = PresenceService()
        svc._mac_to_user = {RAW_MAC: 1}
        monkeypatch.setattr(ps, "get_presence_service", lambda: svc)
        monkeypatch.setattr(hub.ha_glue_settings, "presence_enabled", True)
        monkeypatch.setattr(hub, "_connected_satellites", lambda: [])
        calls = []
        real = hub.build_sightings_payload
        monkeypatch.setattr(
            hub, "build_sightings_payload", lambda p: calls.append(1) or real(p)
        )
        report = [{"mac": RAW_MAC, "rssi": -50}]

        await svc.process_ble_report("sat-1", 10, report, room_name="Kitchen")
        await asyncio.sleep(0.01)
        assert calls == []

        ws = _FakeWS()
        hub.register_viewer(ws)
        await svc.process_ble_report("sat-1", 10, report, room_name="Kitchen")
        await hub._flush_task
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "snapshot"
        assert ws.sent[0]["devices"][0]["sightings"][-1]["rssi"] == -50

    async def test_a_burst_collapses_to_one_snapshot_per_interval(self, monkeypatch):
        monkeypatch.setattr(hub, "PUSH_MIN_INTERVAL_SECONDS", 0.2)
        counter = {"n": 0}

        def build():
            counter["n"] += 1
            return {"seq": counter["n"]}

        monkeypatch.setattr(hub, "build_current_payload", build)
        ws = _FakeWS()
        hub.register_viewer(ws)

        # First report after a quiet period goes out at once.
        hub.note_ble_report()
        await asyncio.sleep(0.05)
        assert [m["seq"] for m in ws.sent] == [1]

        # Ten more inside the interval: ONE further snapshot, after the interval.
        for _ in range(10):
            hub.note_ble_report()
            await asyncio.sleep(0.005)
        assert len(ws.sent) == 1
        await hub._flush_task
        assert [m["seq"] for m in ws.sent] == [1, 2]
        # Built at send time, so it reflects the state after the whole burst.
        assert counter["n"] == 2

    async def test_quiet_after_a_push_schedules_nothing(self, monkeypatch):
        monkeypatch.setattr(hub, "build_current_payload", lambda: {})
        ws = _FakeWS()
        hub.register_viewer(ws)
        hub.note_ble_report()
        await hub._flush_task
        assert len(ws.sent) == 1
        await asyncio.sleep(0.05)
        assert hub._flush_task.done()
        assert len(ws.sent) == 1

    async def test_a_broken_viewer_is_dropped_and_the_rest_served(self, monkeypatch):
        monkeypatch.setattr(hub, "build_current_payload", lambda: {})
        good, bad = _FakeWS(), _BrokenWS()
        hub.register_viewer(good)
        hub.register_viewer(bad)
        hub.note_ble_report()
        await hub._flush_task
        assert len(good.sent) == 1
        assert bad not in hub._debug_clients
        assert good in hub._debug_clients

    async def test_a_failing_builder_does_not_kill_the_hub(self, monkeypatch):
        monkeypatch.setattr(
            hub, "build_current_payload", MagicMock(side_effect=RuntimeError("boom"))
        )
        ws = _FakeWS()
        hub.register_viewer(ws)
        hub.note_ble_report()
        await hub._flush_task
        assert ws.sent == []
        monkeypatch.setattr(hub, "build_current_payload", lambda: {"ok": True})
        monkeypatch.setattr(hub, "PUSH_MIN_INTERVAL_SECONDS", 0.0)
        hub.note_ble_report()
        await hub._flush_task
        assert ws.sent == [{"type": "snapshot", "ok": True}]

    async def test_viewer_leaving_mid_wait_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(hub, "PUSH_MIN_INTERVAL_SECONDS", 0.1)
        build = MagicMock(return_value={})
        monkeypatch.setattr(hub, "build_current_payload", build)
        ws = _FakeWS()
        hub.register_viewer(ws)
        hub.note_ble_report()
        await hub._flush_task
        hub.note_ble_report()          # waits out the interval ...
        hub.unregister_viewer(ws)      # ... and the viewer leaves meanwhile
        await hub._flush_task
        assert build.call_count == 1
