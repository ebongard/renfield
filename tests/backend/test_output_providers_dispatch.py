"""Phase 3b: generic-provider dispatch in InternalToolService._play_via_provider
+ bounded power-on poll. Uses a fake provider (duck-typed to the OutputProvider
Protocol); asyncio.sleep is patched so the poll loop runs instantly.

These cover the agent media path's registry branch without a real Samsung MCP.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from ha_glue.services.internal_tools import InternalToolService
from ha_glue.services.output_providers import (
    ControlResult,
    OutputProviderError,
    PlayResult,
    TargetStatus,
)

pytestmark = pytest.mark.asyncio


class FakeProvider:
    """Configurable stand-in for an McpOutputProvider."""

    def __init__(self, *, power=True, boot_timeout=4.0, status_seq=None, play=None,
                 on_raises=False, unsupported=(), control_result=None, status_value=None):
        self.key = "samsung"
        self.boot_timeout = boot_timeout
        self._caps = {"video", "audio"} | ({"power"} if power else set())
        self._status_seq = list(status_seq or [])
        self._play = play if play is not None else PlayResult(ok=True, state="playing")
        self._on_raises = on_raises
        self._unsupported = set(unsupported)        # actions that raise (unmapped)
        self._control_result = control_result        # override control() return
        self._status_value = status_value            # fixed status() for control tests
        self.calls = []

    def has_capability(self, cap):
        return cap in self._caps

    async def status(self, target_id):
        self.calls.append(("status", target_id))
        if self._status_value is not None:
            return self._status_value
        if not self._status_seq:
            return TargetStatus(state="unknown")
        nxt = self._status_seq.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def control(self, target_id, action, value=None):
        self.calls.append(("control", action, value))
        if action in self._unsupported:
            raise OutputProviderError(f"no control mapping for action '{action}'")
        if action == "on" and self._on_raises:
            raise OutputProviderError("WoL send failed")
        if self._control_result is not None:
            return self._control_result
        return ControlResult(ok=True)

    async def play(self, target_id, items, mode="now"):
        self.calls.append(("play", items[0].url, mode))
        if isinstance(self._play, Exception):
            raise self._play
        return self._play


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


def _data():
    return {"output_target_id": "192.168.1.47", "device_name": "Living Room TV", "room_name": "Wohnzimmer"}


async def _play(provider):
    svc = InternalToolService()
    return await svc._play_via_provider(
        provider, _data(), media_url="http://x/a.mp4", title="Movie",
        room_name="Wohnzimmer", params={},
    )


async def test_already_on_plays_directly_no_power_on():
    p = FakeProvider(status_seq=[TargetStatus(state="playing")])
    res = await _play(p)
    assert res["success"] is True
    assert "Playing on Living Room TV" in res["message"]
    # status checked once; no control(on) because not off
    assert ("control", "on", None) not in p.calls
    assert any(c[0] == "play" for c in p.calls)


async def test_asleep_powers_on_then_plays():
    # pre-play status raises (asleep) → power on → poll: off, then ready → play
    p = FakeProvider(
        status_seq=[
            OutputProviderError("unreachable"),   # pre-play probe
            TargetStatus(state="off"),            # poll 1
            TargetStatus(state="stopped"),        # poll 2 → ready
        ]
    )
    res = await _play(p)
    assert res["success"] is True
    assert ("control", "on", None) in p.calls
    assert any(c[0] == "play" for c in p.calls)


async def test_never_wakes_returns_honest_error():
    # always off/unreachable through the bounded poll → no play, honest failure
    p = FakeProvider(
        boot_timeout=4.0,
        status_seq=[OutputProviderError("unreachable")] + [TargetStatus(state="off")] * 10,
    )
    res = await _play(p)
    assert res["success"] is False
    assert "Could not wake Living Room TV" in res["message"]
    assert not any(c[0] == "play" for c in p.calls)  # never played


async def test_power_on_failure_returns_error():
    p = FakeProvider(status_seq=[OutputProviderError("unreachable")], on_raises=True)
    res = await _play(p)
    assert res["success"] is False
    assert "Could not power on Living Room TV" in res["message"]


async def test_play_not_ok_returns_failure():
    p = FakeProvider(
        status_seq=[TargetStatus(state="playing")],
        play=PlayResult(ok=False, message="renderer 404"),
    )
    res = await _play(p)
    assert res["success"] is False
    assert "Playback failed on Living Room TV: renderer 404" in res["message"]


async def test_play_raises_returns_failure():
    p = FakeProvider(status_seq=[TargetStatus(state="playing")], play=OutputProviderError("boom"))
    res = await _play(p)
    assert res["success"] is False
    assert "Playback failed" in res["message"]


async def test_non_power_provider_skips_power_on():
    p = FakeProvider(power=False)
    res = await _play(p)
    assert res["success"] is True
    # no status / control at all — straight to play
    assert all(c[0] == "play" for c in p.calls)


async def test_poll_ready_bounded_returns_false_on_timeout():
    p = FakeProvider(boot_timeout=4.0)  # 4/2 = 2 polls
    p._status_seq = [TargetStatus(state="off"), TargetStatus(state="off")]
    svc = InternalToolService()
    assert await svc._poll_provider_ready(p, "t1") is False


async def test_poll_ready_returns_true_when_not_off():
    p = FakeProvider(boot_timeout=10.0)
    p._status_seq = [TargetStatus(state="off"), TargetStatus(state="playing")]
    svc = InternalToolService()
    assert await svc._poll_provider_ready(p, "t1") is True


# --- _media_control_via_provider --------------------------------------------

def _ctl_data():
    return {"output_target_id": "192.168.1.47", "device_name": "Living Room TV", "room_name": "Wohnzimmer"}


async def _ctl(provider, action, **params):
    svc = InternalToolService()
    return await svc._media_control_via_provider(action, _ctl_data(), "Wohnzimmer", params, provider)


async def test_control_status_returns_state():
    p = FakeProvider(status_value=TargetStatus(state="playing", position="00:01:23"))
    res = await _ctl(p, "status")
    assert res["success"] is True
    assert res["data"]["state"] == "playing"
    assert res["data"]["position"] == "00:01:23"


async def test_control_stop_maps_to_control():
    p = FakeProvider()
    res = await _ctl(p, "stop")
    assert res["success"] is True
    assert ("control", "stop", None) in p.calls


async def test_control_volume_absolute_passes_int():
    p = FakeProvider()
    res = await _ctl(p, "volume", volume=30)
    assert res["success"] is True
    assert ("control", "volume", 30) in p.calls


async def test_control_volume_relative_only_is_graceful():
    p = FakeProvider()
    res = await _ctl(p, "volume", volume_step=-10)  # no absolute volume
    assert res["success"] is False
    assert "Relative volume" in res["message"]
    assert not any(c[0] == "control" for c in p.calls)


async def test_control_unsupported_action_is_graceful():
    p = FakeProvider(unsupported={"next"})
    res = await _ctl(p, "next")
    assert res["success"] is False
    assert "isn't supported" in res["message"]


async def test_control_not_ok_returns_failure():
    p = FakeProvider(control_result=ControlResult(ok=False, message="renderer down"))
    res = await _ctl(p, "pause")
    assert res["success"] is False
    assert "renderer down" in res["message"]
