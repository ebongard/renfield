"""Device (browser/web) liveness after the dead sweep was removed (BL-0502, #1277).

``DeviceManager.cleanup_stale`` never had a caller in production code; scheduling
it unmeasured would have evicted devices the household tolerates today, so it
was deleted instead. What holds now: a device leaves the roster when its socket
closes (``device_handler`` → ``unregister``), and nothing in the manager expires
devices or sessions on a clock. These tests keep the removal honest.
"""
from unittest.mock import AsyncMock

import pytest

from ha_glue.services.device_manager import DeviceManager, DeviceSession


@pytest.mark.unit
class TestNoClockBasedEviction:
    def test_manager_has_no_sweep(self):
        # A resurrected sweep would have to be scheduled AND calibrated first.
        assert not hasattr(DeviceManager, "cleanup_stale")
        assert not hasattr(DeviceManager(), "heartbeat_timeout")

    def test_session_carries_no_max_duration(self):
        # The only reader of that field was the sweep; a leftover would be a
        # silent invitation to reintroduce a wall-clock cut-off.
        assert "max_duration_seconds" not in DeviceSession.__dataclass_fields__


@pytest.mark.unit
class TestDisconnectIsTheOnlyEviction:
    async def test_unregister_removes_device_and_ends_its_session(self):
        mgr = DeviceManager()
        ws = AsyncMock()
        await mgr.register(
            device_id="web-1", websocket=ws, device_type="web", room="Küche",
            capabilities={"has_microphone": True},
        )
        assert "web-1" in mgr.devices
        sid = await mgr.start_session("web-1")
        assert sid is not None and mgr.devices["web-1"].current_session_id == sid
        assert sid in mgr.sessions

        await mgr.unregister("web-1")

        assert "web-1" not in mgr.devices
        assert sid not in mgr.sessions

    async def test_unregister_unknown_device_is_a_noop(self):
        mgr = DeviceManager()
        await mgr.unregister("never-registered")
        assert mgr.devices == {}


@pytest.mark.unit
class TestFastReconnectKeepsTheLiveSocket:
    """With socket close as the only eviction, the dying socket's ``finally``
    must not evict a NEW connection that already re-registered the same id."""

    async def _reconnected(self) -> tuple[DeviceManager, AsyncMock, AsyncMock]:
        mgr = DeviceManager()
        old_ws, new_ws = AsyncMock(), AsyncMock()
        for ws in (old_ws, new_ws):
            await mgr.register(
                device_id="web-1", websocket=ws, device_type="web", room="Küche",
                capabilities={"has_microphone": True},
            )
        assert mgr.devices["web-1"].websocket is new_ws
        return mgr, old_ws, new_ws

    async def test_stale_socket_does_not_evict_the_new_connection(self):
        mgr, old_ws, new_ws = await self._reconnected()
        sid = await mgr.start_session("web-1")

        await mgr.unregister("web-1", websocket=old_ws)

        assert mgr.devices["web-1"].websocket is new_ws
        assert sid in mgr.sessions

    async def test_owning_socket_still_unregisters(self):
        mgr, _old_ws, new_ws = await self._reconnected()
        await mgr.unregister("web-1", websocket=new_ws)
        assert "web-1" not in mgr.devices

    async def test_no_socket_keeps_the_unconditional_behaviour(self):
        mgr, _old_ws, _new_ws = await self._reconnected()
        await mgr.unregister("web-1")
        assert "web-1" not in mgr.devices

    def test_handler_finally_passes_its_socket(self):
        # The guard only protects when the handler identifies itself.
        import inspect

        from ha_glue.api.websocket import device_handler

        src = inspect.getsource(device_handler)
        assert "device_manager.unregister(device_id, websocket=websocket)" in src
        assert "device_manager.unregister(device_id)" not in src
