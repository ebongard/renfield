"""ha_deliver_notification — WS broadcast privacy gate (cross-user leak fix).

A `privacy="personal"`/`"confidential"` notification must NOT WebSocket-broadcast
to the whole household when the presence gate rejects it (or no gate is wired).
Before the fix only TTS was gated; the WS push fell back to all devices when no
room resolved, leaking one user's obligation reminder (kind/amount/legal flag) to
every household display. Pure unit test — device_manager + run_hooks mocked.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _device(device_id: str):
    return SimpleNamespace(
        device_id=device_id,
        capabilities=SimpleNamespace(supports_notifications=True, has_display=True, has_speaker=False),
        websocket=SimpleNamespace(send_json=AsyncMock()),
    )


def _notif(*, privacy: str, room_name=None, room_id=None, target_user_id=5):
    return SimpleNamespace(
        id=1, title="Frist: zahlung", message="Heute fällig.", urgency="info",
        source="obligation_notifier", room_name=room_name, room_id=room_id,
        privacy=privacy, target_user_id=target_user_id, created_at=None,
        tts_delivered=False,
    )


def _wire(monkeypatch, devices, hook_result):
    dm = SimpleNamespace(
        devices={d.device_id: d for d in devices},
        get_devices_in_room=lambda name: list(devices),
        get_devices_in_room_by_id=lambda rid: list(devices),
    )
    monkeypatch.setattr("ha_glue.services.device_manager.get_device_manager", lambda: dm)
    if hook_result is not None:
        monkeypatch.setattr("utils.hooks.run_hooks", AsyncMock(return_value=hook_result))
    return dm


class TestWsPrivacyGate:
    async def test_personal_gate_reject_suppresses_ws_broadcast(self, monkeypatch):
        from ha_glue.services.device_handlers import ha_deliver_notification
        devices = [_device("disp-a"), _device("disp-b")]
        _wire(monkeypatch, devices, hook_result=[False])  # presence gate says no
        out = await ha_deliver_notification(notification=_notif(privacy="personal"), tts=False)
        assert out == []  # nothing delivered
        for d in devices:
            d.websocket.send_json.assert_not_awaited()  # NOT leaked to any device

    async def test_personal_gate_allow_broadcasts(self, monkeypatch):
        from ha_glue.services.device_handlers import ha_deliver_notification
        devices = [_device("disp-a")]
        _wire(monkeypatch, devices, hook_result=[True])  # presence gate allows
        out = await ha_deliver_notification(notification=_notif(privacy="personal", room_id=7), tts=False)
        assert out == ["disp-a"]
        devices[0].websocket.send_json.assert_awaited_once()

    async def test_public_broadcasts_without_gate(self, monkeypatch):
        from ha_glue.services.device_handlers import ha_deliver_notification
        devices = [_device("disp-a")]
        # run_hooks must NOT be consulted for public — wire it to blow up if called.
        boom = AsyncMock(side_effect=AssertionError("gate consulted for public"))
        monkeypatch.setattr("utils.hooks.run_hooks", boom)
        _wire(monkeypatch, devices, hook_result=None)
        out = await ha_deliver_notification(notification=_notif(privacy="public"), tts=False)
        assert out == ["disp-a"]
        boom.assert_not_called()

    async def test_personal_gate_error_fails_closed(self, monkeypatch):
        from ha_glue.services.device_handlers import ha_deliver_notification
        devices = [_device("disp-a")]
        dm = SimpleNamespace(
            devices={d.device_id: d for d in devices},
            get_devices_in_room=lambda name: list(devices),
            get_devices_in_room_by_id=lambda rid: list(devices),
        )
        monkeypatch.setattr("ha_glue.services.device_manager.get_device_manager", lambda: dm)
        monkeypatch.setattr("utils.hooks.run_hooks", AsyncMock(side_effect=RuntimeError("presence down")))
        out = await ha_deliver_notification(notification=_notif(privacy="confidential"), tts=False)
        assert out == []  # gate error → suppress (fail-closed)
        devices[0].websocket.send_json.assert_not_awaited()
