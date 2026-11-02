"""The satellite echoes the backend's OTA run id (#1280).

The backend mints an id per run and sends it in the ``update_request``; it drops
any progress/complete/failed frame that carries a different one. The satellite
has to carry the id of the run that PRODUCED a frame -- and must send no id at
all when the backend sent none (older backend: frames stay byte-identical).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from renfield_satellite.network.websocket_client import ConnectionState, WebSocketClient


def _client() -> WebSocketClient:
    client = WebSocketClient(satellite_id="sat-test", room="Test Room")
    client._send = AsyncMock()
    client._state = ConnectionState.CONNECTED
    client._ws = MagicMock()
    return client


def _sent(client) -> dict:
    return client._send.call_args.args[0]


# ==========================================================================
# 1. Client: request parsing and frames
# ==========================================================================


@pytest.mark.satellite
async def test_update_request_passes_the_run_id_on():
    client = WebSocketClient(satellite_id="sat-test", room="Test Room")
    seen = {}
    client.on_update_request(lambda *a, **k: seen.update(args=a, kwargs=k))

    await client._handle_message({
        "type": "update_request", "target_version": "1.5.0",
        "package_url": "/p", "checksum": "sha256:x", "size_bytes": 1,
        "run_id": "abc123",
    })

    assert seen["kwargs"] == {"run_id": "abc123"}
    assert seen["args"][0] == "1.5.0"


@pytest.mark.satellite
async def test_update_request_without_or_with_bad_run_id_yields_none():
    client = WebSocketClient(satellite_id="sat-test", room="Test Room")
    seen = []
    client.on_update_request(lambda *a, **k: seen.append(k["run_id"]))

    base = {"type": "update_request", "target_version": "1.5.0"}
    await client._handle_message(dict(base))
    await client._handle_message(dict(base, run_id={"x": 1}))

    assert seen == [None, None]


@pytest.mark.satellite
async def test_frames_carry_the_run_id_when_given():
    client = _client()

    await client.send_update_progress("downloading", 10, "x", run_id="r1")
    assert _sent(client)["run_id"] == "r1"

    await client.send_update_complete(True, "1.4", "1.5", run_id="r1")
    assert _sent(client) == {
        "type": "update_complete", "success": True, "old_version": "1.4",
        "new_version": "1.5", "error": None, "run_id": "r1",
    }

    await client.send_update_failed("installing", "boom", True, run_id="r1")
    assert _sent(client)["run_id"] == "r1"


@pytest.mark.satellite
async def test_frames_without_run_id_stay_legacy():
    client = _client()

    await client.send_update_progress("downloading", 10, "x")
    assert _sent(client) == {
        "type": "update_progress", "stage": "downloading", "progress": 10, "message": "x",
    }
    await client.send_update_failed("installing", "boom")
    assert "run_id" not in _sent(client)
    await client.send_update_complete(True, "1.4", "1.5")
    assert "run_id" not in _sent(client)


# ==========================================================================
# 2. Satellite: which id a frame carries
# ==========================================================================


def _satellite(is_updating: bool = False):
    from renfield_satellite.satellite import Satellite

    sat = object.__new__(Satellite)
    sat._ota_run_id = None
    sat.ws_client = MagicMock()
    sat.ws_client.server_url = "ws://backend:8000/ws/satellite"
    sat.ws_client.send_update_progress = MagicMock(return_value=None)
    sat.ws_client.send_update_complete = AsyncMock()
    sat.ws_client.send_update_failed = AsyncMock()
    sat.update_manager = MagicMock()
    sat.update_manager.is_updating = is_updating
    sat.update_manager.last_error = "boom"
    sat.update_manager.rolled_back = True
    sat._schedule_async = MagicMock()
    return sat


@pytest.mark.satellite
def test_request_hands_its_run_id_to_the_start_without_claiming_the_run():
    """The id becomes the RUN's id only once the start has accepted it."""
    sat = _satellite()
    sat._start_update = MagicMock(return_value=None)
    sat._on_update_request("1.5.0", "/p", "sha256:x", 1, run_id="r1")

    assert sat._start_update.call_args.kwargs["run_id"] == "r1"
    assert sat._ota_run_id is None


@pytest.mark.satellite
async def test_accepted_start_labels_the_progress_frames_of_its_run():
    from renfield_satellite.update.update_manager import UpdateStage

    sat = _satellite()

    async def _run(**_kwargs):
        # What the real manager does while it works: report progress.
        sat._on_update_progress(UpdateStage.DOWNLOADING, 10, "x")
        return True

    sat.update_manager.start_update = AsyncMock(side_effect=_run)

    await sat._start_update("1.5.0", "/p", "sha256:x", 1, "http://b", run_id="r1")

    assert sat._ota_run_id == "r1"
    assert sat.ws_client.send_update_progress.call_args.kwargs["run_id"] == "r1"
    assert sat.ws_client.send_update_complete.call_args.kwargs["run_id"] == "r1"


@pytest.mark.satellite
async def test_request_during_a_running_update_is_refused_under_its_own_id():
    """Neither relabels the running run's frames nor reports ITS state."""
    from renfield_satellite.satellite import UPDATE_ALREADY_IN_PROGRESS
    from renfield_satellite.update.update_manager import UpdateStage

    sat = _satellite(is_updating=True)
    sat._ota_run_id = "running"
    sat.update_manager.current_stage = UpdateStage.INSTALLING
    sat.update_manager.start_update = AsyncMock(return_value=False)

    await sat._start_update("1.5.0", "/p", "sha256:x", 1, "http://b", run_id="second")

    sat.update_manager.start_update.assert_not_called()
    assert sat._ota_run_id == "running"
    assert sat.ws_client.send_update_failed.call_args.kwargs == {
        "stage": "idle",
        "error": UPDATE_ALREADY_IN_PROGRESS,
        "rolled_back": False,
        "run_id": "second",
    }
    sat._on_update_progress(UpdateStage.INSTALLING, 70, "x")
    assert sat.ws_client.send_update_progress.call_args.kwargs["run_id"] == "running"


@pytest.mark.satellite
async def test_start_update_answers_with_its_own_run_id():
    for success in (True, False):
        sat = _satellite()
        sat.update_manager.start_update = AsyncMock(return_value=success)

        await sat._start_update("1.5.0", "/p", "sha256:x", 1, "http://b", run_id="r9")

        sent = sat.ws_client.send_update_complete if success else sat.ws_client.send_update_failed
        assert sent.call_args.kwargs["run_id"] == "r9"
