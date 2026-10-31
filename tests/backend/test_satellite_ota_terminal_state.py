"""OTA run state: terminal stages, late messages, the time limit (#1209).

During the v1.4.7 rollout a satellite stayed permanently on
``in_progress``/``rolling_back``, with ``update_error: null`` -- a hanging
update with no discernible reason. Three gaps sat behind it, and each gets its
cases here:

* The progress branch treated ``failed``/``rolling_back`` as "still running",
  although both end a run.
* In doing so it overwrote ``update_error`` with ``None`` and thereby deleted
  the cause the satellite had supplied.
* ``cleanup_stale`` -- the only place with a time limit -- had no caller in
  production code at all.

Runs on the build box .159 (CI is deliberately non-functional), see
``memory/reference_test_runner_159.md``.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _manager_with_satellite(status=None, stage=None, error=None):
    """A manager with exactly one registered satellite in the wanted run."""
    from ha_glue.services.satellite_manager import (
        SatelliteCapabilities,
        SatelliteInfo,
        SatelliteManager,
    )

    mgr = SatelliteManager()
    mgr.satellites["sat-1"] = SatelliteInfo(
        satellite_id="sat-1",
        room="Fitnessraum",
        websocket=MagicMock(),
        capabilities=SatelliteCapabilities(),
        version="1.4.6",
    )
    if status is not None:
        mgr.set_update_status("sat-1", status, stage=stage, progress=0, error=error)
    return mgr


# ==========================================================================
# 1. Terminal stages end the run
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_rolling_back_terminates_the_run():
    """``rolling_back`` is the last message of a rolled-back run -- after it
    the status must not keep claiming "running"."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0,
                              message="Rolling back...")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert sat.update_stage == "rolling_back"


@pytest.mark.backend
@pytest.mark.unit
def test_failed_stage_keeps_the_reported_cause():
    """The reason sits in the satellite's message. That is exactly what was
    missing in the failure picture (``update_error: null``)."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress(
        "sat-1", stage="failed", progress=0,
        message="Unknown packages in requirements: ['opuslib>=3.0.1']",
    )

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert "opuslib" in sat.update_error


@pytest.mark.backend
@pytest.mark.unit
def test_rollback_after_failure_does_not_erase_the_cause():
    """The observed order: first ``failed`` with a reason, then
    ``rolling_back`` without one. The reason has to survive the second
    message."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress("sat-1", stage="failed", progress=0,
                              message="Unknown packages in requirements")
    # VERBATIM as it goes over the wire: the satellite ALWAYS sends this
    # text (update_manager.py:762), never an empty one. The earlier version of
    # this test passed "" and was green for that reason, while the code was in
    # truth overwriting the cause on EVERY rollback.
    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0,
                              message="Rolling back...")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert sat.update_error == "Unknown packages in requirements"


# ==========================================================================
# 2. A finished run is not brought back
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_late_progress_does_not_resurrect_a_failed_run():
    """The satellite schedules progress messages separately while it waits
    for the final one -- a trailing message is the normal case."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite(status=US.FAILED, stage="failed",
                                     error="Installer abgebrochen")
    mgr.apply_update_progress("sat-1", stage="installing", progress=80, message="")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert sat.update_stage == "failed"
    assert sat.update_error == "Installer abgebrochen"


@pytest.mark.backend
@pytest.mark.unit
def test_late_progress_does_not_resurrect_a_completed_run():
    """The same clamp in the other direction: a successful run stays
    successful, even if a ``restarting`` still trickles in afterwards."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite(status=US.COMPLETED, stage="completed")
    mgr.apply_update_progress("sat-1", stage="restarting", progress=90, message="")

    assert mgr.get_satellite("sat-1").update_status == US.COMPLETED


@pytest.mark.backend
@pytest.mark.unit
def test_a_new_update_may_start_after_a_failed_one():
    """The guard may affect ONLY progress messages. A new run starts through
    ``set_update_status`` and has to keep being able to start -- otherwise the
    fix repairs the display and destroys the rollout."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite(status=US.FAILED, stage="failed", error="alt")
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage="downloading", progress=20, message="")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.IN_PROGRESS
    assert sat.update_stage == "downloading"


@pytest.mark.backend
@pytest.mark.unit
def test_normal_progress_still_advances():
    """The unremarkable case stays unchanged."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage="downloading", progress=40, message="")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.IN_PROGRESS
    assert sat.update_progress == 40


@pytest.mark.backend
@pytest.mark.unit
def test_progress_for_unknown_satellite_is_ignored():
    mgr = _manager_with_satellite()
    mgr.apply_update_progress("gibt-es-nicht", stage="downloading", progress=1)
    assert "gibt-es-nicht" not in mgr.satellites


# ==========================================================================
# 3. The time limit in the sweep
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_stale_fails_a_run_without_terminal_state():
    """The safety net: a run whose final report never arrives."""
    import time

    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    sat = mgr.get_satellite("sat-1")
    sat.update_started_at = time.time() - 100_000  # far beyond any limit

    await mgr.cleanup_stale()

    assert sat.update_status == US.FAILED
    assert "Endzustand" in sat.update_error
    assert sat.update_started_at is None  # does not fire again


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_stale_leaves_a_young_run_alone():
    """The sweep must not take down a running update."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="downloading", progress=10)

    await mgr.cleanup_stale()

    assert mgr.get_satellite("sat-1").update_status == US.IN_PROGRESS


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_stale_keeps_a_cause_the_satellite_gave():
    """Do not invent a reason when one is already there."""
    import time

    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    sat = mgr.get_satellite("sat-1")
    sat.update_error = "Paketquelle nicht erreichbar"
    sat.update_started_at = time.time() - 100_000

    await mgr.cleanup_stale()

    assert sat.update_error == "Paketquelle nicht erreichbar"


@pytest.mark.backend
@pytest.mark.unit
def test_progress_frames_do_not_push_the_deadline_out():
    """The start mark is set ONCE. Otherwise a satellite that reports
    progress endlessly would escape the time limit permanently."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    first = mgr.get_satellite("sat-1").update_started_at

    mgr.apply_update_progress("sat-1", stage="downloading", progress=10)
    mgr.apply_update_progress("sat-1", stage="installing", progress=70)

    assert mgr.get_satellite("sat-1").update_started_at == first


@pytest.mark.backend
@pytest.mark.unit
def test_terminal_state_clears_the_start_mark():
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.set_update_status("sat-1", US.COMPLETED, stage="completed", progress=100)

    assert mgr.get_satellite("sat-1").update_started_at is None


# ==========================================================================
# 4. The missing scheduler -- without it everything above has no effect
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_scheduler_actually_calls_the_sweep():
    """``cleanup_stale`` had NO caller in production code. The test pins down
    that the scheduler exists, and that it takes the manager singleton -- a
    second instance would sweep an empty registry."""
    import ha_glue.bootstrap as bootstrap

    fake = MagicMock()
    fake.cleanup_stale = AsyncMock()

    with patch("ha_glue.utils.config.ha_glue_settings.satellite_cleanup_interval", 0), \
         patch("ha_glue.services.satellite_manager.get_satellite_manager",
               return_value=fake):
        bootstrap._schedule_satellite_cleanup()
        try:
            await asyncio.sleep(0.05)
            assert fake.cleanup_stale.await_count >= 1
        finally:
            for task in bootstrap._ha_glue_tasks:
                task.cancel()
            bootstrap._ha_glue_tasks.clear()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_scheduler_survives_a_failing_sweep():
    """An error in the sweep must not end the scheduler -- otherwise the
    time limit would be dead again after the first hiccup."""
    import ha_glue.bootstrap as bootstrap

    fake = MagicMock()
    fake.cleanup_stale = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("ha_glue.utils.config.ha_glue_settings.satellite_cleanup_interval", 0), \
         patch("ha_glue.services.satellite_manager.get_satellite_manager",
               return_value=fake):
        bootstrap._schedule_satellite_cleanup()
        try:
            await asyncio.sleep(0.05)
            assert fake.cleanup_stale.await_count >= 2  # kept running
            assert not bootstrap._ha_glue_tasks[0].done()
        finally:
            for task in bootstrap._ha_glue_tasks:
                task.cancel()
            bootstrap._ha_glue_tasks.clear()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_startup_hook_wires_the_cleanup_scheduler():
    """The scheduler really has to be created out of the startup path.

    Without this case the tests above only prove the rules are right -- not
    that they ever run. That gap was exactly the original finding: a finished
    sweep function that nobody called.
    """
    import ha_glue.bootstrap as bootstrap

    led = MagicMock()
    led.return_value.initialize = AsyncMock()

    with patch.object(bootstrap, "_schedule_satellite_cleanup") as sched, \
         patch.object(bootstrap, "_schedule_ha_keywords_preload"), \
         patch.object(bootstrap, "_init_zeroconf", new=AsyncMock()), \
         patch("ha_glue.utils.config.ha_glue_settings.presence_enabled", False), \
         patch("ha_glue.services.led_dimming_service.get_led_dimming_service", led):
        await bootstrap.ha_glue_on_startup(app=MagicMock())

    sched.assert_called_once()


# ==========================================================================
# 5. Late TERMINAL STAGES -- the guard applies to both branches
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_late_terminal_stage_does_not_resurrect_a_completed_run():
    """A duplicate ``rolling_back`` message after a successful run must not
    tip ``COMPLETED`` over to ``FAILED``."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=90)
    mgr.set_update_status("sat-1", US.COMPLETED, stage="completed", progress=100)

    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0,
                              message="Rolling back...")

    assert mgr.get_satellite("sat-1").update_status == US.COMPLETED


@pytest.mark.backend
@pytest.mark.unit
def test_a_stray_terminal_frame_still_ends_the_current_run():
    """A KNOWN LIMIT -- recorded here instead of passed over in silence.

    The guard protects a run that has already ENDED. It cannot tell a straggler
    from an old run apart from a genuine message of the run currently in
    flight, because the protocol carries no run id: ``send_update_progress``
    transmits only ``{type, stage, progress, message}``. A late
    ``rolling_back`` that arrives AFTER a new run has started therefore ends
    that new run.

    Narrow in practice: the straggler is scheduled separately on the satellite
    and arrives within milliseconds, while a new run needs an ADMIN click.
    Closing it cleanly would take a run id in the protocol -- a change to the
    satellite firmware, which does not belong here. A heuristic would be
    guesswork and therefore worse than the documented limit.

    This test records what IS. If the id is introduced, it has to be rewritten
    to the expectation wanted then -- it is the place where that surfaces.
    """
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite(status=US.FAILED, stage="failed", error="alt")
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage="downloading", progress=50)

    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0,
                              message="Rolling back...")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED  # the limit, not the wish
    assert sat.update_error == "Rolling back..."


@pytest.mark.backend
@pytest.mark.unit
def test_completed_stage_ends_the_run_as_completed():
    """The satellite reports ``completed`` as progress BEFORE the restart,
    which usually swallows the ``update_complete``. If the run stayed on
    ``in_progress``, the time limit would later mark a SUCCESSFUL update as
    failed."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="restarting", progress=90)
    mgr.apply_update_progress("sat-1", stage="completed", progress=100,
                              message="Updated to 1.4.7")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.COMPLETED
    assert sat.update_error is None
    assert sat.update_started_at is None


@pytest.mark.backend
@pytest.mark.unit
def test_clear_update_status_also_clears_the_start_mark():
    """Otherwise the NEXT run inherits an expired deadline and is struck
    down by the sweep immediately."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.clear_update_status("sat-1")

    assert mgr.get_satellite("sat-1").update_started_at is None


# ==========================================================================
# 6. Hostile or broken devices
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_non_string_fields_do_not_poison_the_admin_list():
    """``stage``/``message`` arrive as JSON from a LAN device. A wrong type
    must not reach the state: the response models type them as ``str``,
    Pydantic does not coerce, and ``list_satellites`` has no per-entry guard --
    ONE poisoned entry takes the whole admin list down."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage={"boom": 1}, progress="viel",
                              message={"auch": "boom"})

    sat = mgr.get_satellite("sat-1")
    assert isinstance(sat.update_stage, str)
    assert isinstance(sat.update_progress, int)


@pytest.mark.backend
@pytest.mark.unit
def test_oversized_message_is_capped():
    """The WS frame allows 1 MB. That much device text must not stay in the
    registry permanently."""
    from ha_glue.services.satellite_manager import SatelliteManager
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress("sat-1", stage="failed", progress=0, message="x" * 100_000)

    sat = mgr.get_satellite("sat-1")
    assert len(sat.update_error) == SatelliteManager._MAX_UPDATE_ERROR_CHARS


# ==========================================================================
# 7. Calibration: the sweep must not clear away anything healthy
# ==========================================================================


def _session(mgr, sat_id="sat-1", state=None, age=0.0):
    """Attaches a session to the satellite in the wanted state and age."""
    import time as _t

    from ha_glue.services.satellite_manager import SatelliteSession, SatelliteState

    sess = SatelliteSession(
        session_id="sess-1",
        satellite_id=sat_id,
        room="Fitnessraum",
        state=state or SatelliteState.LISTENING,
    )
    sess.started_at = _t.time() - age
    mgr.sessions["sess-1"] = sess
    mgr.satellites[sat_id].current_session_id = "sess-1"
    return sess


@pytest.mark.backend
@pytest.mark.asyncio
async def test_recording_timeout_only_hits_a_listening_session():
    """The deadline is a maximum RECORDING duration. It starts at the wake
    word, and the whole turn runs inline in the same receive loop -- if it
    measured the turn, it would destroy the session in the middle of the
    answer, and ``send_tts_audio`` would discard it silently."""
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.PROCESSING, age=100_000)

    await mgr.cleanup_stale()

    assert "sess-1" in mgr.sessions, "a thinking turn must not be cleared away"


@pytest.mark.backend
@pytest.mark.asyncio
async def test_recording_timeout_ends_an_overlong_recording():
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.LISTENING, age=100_000)

    await mgr.cleanup_stale()

    assert "sess-1" not in mgr.sessions


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_satellite_in_a_live_turn_is_not_evicted():
    """Its heartbeats sit unread in the buffer because the turn is processed
    inline -- it is not a dead device."""
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.PROCESSING, age=1.0)
    mgr.satellites["sat-1"].last_heartbeat = 0.0

    await mgr.cleanup_stale()

    assert "sat-1" in mgr.satellites


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_satellite_installing_an_update_is_not_evicted():
    """The installer blocks the Pi's event loop for up to 150 s against a
    60 s deadline. If the sweep cleared it, it would lose the start mark -- and
    thereby disarm the OTA time limit in exactly the case it exists for."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.satellites["sat-1"].last_heartbeat = 0.0

    await mgr.cleanup_stale()

    assert "sat-1" in mgr.satellites
    assert mgr.get_satellite("sat-1").update_started_at is not None


@pytest.mark.backend
@pytest.mark.asyncio
async def test_eviction_closes_the_socket():
    """Without closing the connection the receive loop carries on and keeps
    acknowledging heartbeats: the device sees a healthy line, never registers
    again and is permanently mute."""
    mgr = _manager_with_satellite()
    ws = mgr.satellites["sat-1"].websocket = AsyncMock()
    mgr.satellites["sat-1"].last_heartbeat = 0.0

    with patch("api.websocket.kiosk_handler.broadcast_kiosk_event", new=AsyncMock()):
        await mgr.cleanup_stale()

    assert "sat-1" not in mgr.satellites
    ws.close.assert_awaited()


# ==========================================================================
# 8. The wiring in the WebSocket handler
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_websocket_loop_calls_apply_update_progress():
    """The rules above are worthless if the receive loop never calls them.
    Checked statically, because otherwise the loop would require registration,
    authentication and room resolution (the same approach as
    test_satellite_memory_extraction.py)."""
    import ast
    from pathlib import Path

    import ha_glue.api.websocket.satellite_handler as sh

    tree = ast.parse(Path(sh.__file__).read_text())
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "satellite_websocket"),
        None,
    )
    assert target is not None, "satellite_websocket not found"
    # The call is an attribute (satellite_manager.apply_update_progress),
    # not a free-standing name -- otherwise the test would check nothing.
    called = {
        n.func.attr
        for n in ast.walk(target)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "apply_update_progress" in called, (
        "the progress branch must go through apply_update_progress — otherwise "
        "neither the terminal-stage detection nor the guard against regressions applies"
    )


@pytest.mark.backend
@pytest.mark.unit
def test_non_string_message_on_a_terminal_stage_is_coerced():
    """The error text comes from the device as JSON too. An object must not
    land in ``update_error`` -- the response models type it as ``str``."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress("sat-1", stage="failed", progress=0,
                              message={"boom": [1, 2, 3]})

    assert isinstance(mgr.get_satellite("sat-1").update_error, str)


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_failing_close_does_not_derail_the_sweep():
    """A broken socket must not stop the sweep -- otherwise everything
    behind it (the OTA time limit included) would hang on a single device."""
    mgr = _manager_with_satellite()
    ws = mgr.satellites["sat-1"].websocket = AsyncMock()
    ws.close = AsyncMock(side_effect=RuntimeError("Socket ist weg"))
    mgr.satellites["sat-1"].last_heartbeat = 0.0

    with patch("api.websocket.kiosk_handler.broadcast_kiosk_event", new=AsyncMock()):
        await mgr.cleanup_stale()

    assert "sat-1" not in mgr.satellites


@pytest.mark.backend
@pytest.mark.asyncio
async def test_derostered_heartbeat_is_closed_not_acked():
    """The core of the zombie finding: after an eviction the heartbeat must
    NOT be acknowledged any more, or the device takes the line for healthy."""
    from ha_glue.api.websocket.satellite_handler import _reject_derostered_heartbeat

    manager = MagicMock()
    manager.is_connected.return_value = False
    ws = AsyncMock()

    assert await _reject_derostered_heartbeat(ws, "sat-1", manager) is True
    ws.close.assert_awaited()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_registered_heartbeat_passes_through():
    from ha_glue.api.websocket.satellite_handler import _reject_derostered_heartbeat

    manager = MagicMock()
    manager.is_connected.return_value = True
    ws = AsyncMock()

    assert await _reject_derostered_heartbeat(ws, "sat-1", manager) is False
    ws.close.assert_not_awaited()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_failing_close_in_the_handler_is_swallowed():
    from ha_glue.api.websocket.satellite_handler import _reject_derostered_heartbeat

    manager = MagicMock()
    manager.is_connected.return_value = False
    ws = AsyncMock()
    ws.close = AsyncMock(side_effect=RuntimeError("weg"))

    assert await _reject_derostered_heartbeat(ws, "sat-1", manager) is True


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_broken_scheduler_does_not_break_ha_glue_startup():
    """The scheduler matters, but not more than the startup of the other
    subsystems."""
    import ha_glue.bootstrap as bootstrap

    led = MagicMock()
    led.return_value.initialize = AsyncMock()

    with patch.object(bootstrap, "_schedule_satellite_cleanup",
                      side_effect=RuntimeError("kein Takt")), \
         patch.object(bootstrap, "_schedule_ha_keywords_preload"), \
         patch.object(bootstrap, "_init_zeroconf", new=AsyncMock()), \
         patch("ha_glue.utils.config.ha_glue_settings.presence_enabled", False), \
         patch("ha_glue.services.led_dimming_service.get_led_dimming_service", led):
        await bootstrap.ha_glue_on_startup(app=MagicMock())  # must not raise


@pytest.mark.backend
@pytest.mark.unit
def test_a_terminal_frame_without_a_message_leaves_no_error():
    """If the satellite leaves ``message`` out, ``data.get`` yields an empty
    string. That must not be filed as a reason: the user interface shows the
    error block only when ``update_error`` is set, so an empty text would give
    a visible but contentless box."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress("sat-1", stage="failed", progress=0, message="")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert sat.update_error is None


# ==========================================================================
# 9. Hardening at the ONE writer -- not per caller
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_set_update_status_coerces_regardless_of_caller():
    """Three WS branches feed this writer (update_progress, update_complete,
    update_failed). Only one was hardened -- the other two passed raw device
    JSON through to the response models, where Pydantic does not coerce and
    ``list_satellites``, without a per-entry guard, turns ONE bad entry into a
    500 for the whole list."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.FAILED, stage={"boom": 1},
                          progress="viel", error={"auch": "boom"})

    sat = mgr.get_satellite("sat-1")
    assert isinstance(sat.update_stage, str)
    assert isinstance(sat.update_progress, int)
    assert isinstance(sat.update_error, str)


@pytest.mark.backend
@pytest.mark.unit
def test_set_version_is_bounded_and_never_empty():
    """``update_complete`` wrote ``sat.version`` straight past the writer --
    and on the response models ``version`` is a bare ``str``."""
    mgr = _manager_with_satellite()

    mgr.set_version("sat-1", {"nicht": "ein string"})
    assert isinstance(mgr.get_satellite("sat-1").version, str)

    mgr.set_version("sat-1", "x" * 10_000)
    assert len(mgr.get_satellite("sat-1").version) <= 64

    mgr.set_version("sat-1", None)
    assert mgr.get_satellite("sat-1").version == "unknown"


# ==========================================================================
# 10. Orphaned sessions -- the flip side of narrowing to recordings
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_orphaned_session_is_swept_when_the_satellite_is_gone():
    mgr = _manager_with_satellite()
    _session(mgr, state=None, age=1.0)
    del mgr.satellites["sat-1"]

    await mgr.cleanup_stale()

    assert "sess-1" not in mgr.sessions


@pytest.mark.backend
@pytest.mark.asyncio
async def test_orphaned_session_is_swept_after_a_fast_reconnect():
    """On a fast reconnect ``unregister`` bails out BEFORE tearing the
    session down -- the dying socket's session stays behind. Previously the
    unconditional pass swept it away; since the narrowing to ``listening``
    nobody would, and it holds on to its buffered audio."""
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.PROCESSING, age=1.0)
    # A new socket has registered and started a new session.
    mgr.satellites["sat-1"].current_session_id = "sess-2"

    await mgr.cleanup_stale()

    assert "sess-1" not in mgr.sessions


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_live_session_is_not_swept_as_orphaned():
    """The counter-check: the sweep must not take a live session with it."""
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.PROCESSING, age=1.0)

    await mgr.cleanup_stale()

    assert "sess-1" in mgr.sessions
