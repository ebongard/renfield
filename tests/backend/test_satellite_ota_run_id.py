"""OTA frames carry the run id the backend minted (#1280).

Before, ``update_progress`` / ``update_complete`` / ``update_failed`` carried no
run identifier, so a late terminal frame of an OLD run could end a NEWLY started
one. The backend now mints an id per run, sends it in the ``update_request``,
and drops any frame that carries a different one. A frame WITHOUT an id comes
from firmware that predates the protocol and keeps the legacy behaviour --
that is the path the fleet updates over, so it must not be refused.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _manager_with_satellite():
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
    return mgr


# ==========================================================================
# 1. Minting and matching
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_begin_update_run_mints_a_fresh_id_per_run():
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    first = mgr.begin_update_run("sat-1")
    sat = mgr.get_satellite("sat-1")
    assert isinstance(first, str) and first
    assert sat.update_run_id == first
    assert sat.update_status == US.IN_PROGRESS
    assert sat.update_stage == "initiating"
    assert sat.update_started_at is not None

    mgr.set_update_status("sat-1", US.FAILED, stage="failed")
    second = mgr.begin_update_run("sat-1")
    assert second != first


@pytest.mark.backend
@pytest.mark.unit
def test_begin_update_run_for_unknown_satellite_is_none():
    mgr = _manager_with_satellite()
    assert mgr.begin_update_run("nope") is None


@pytest.mark.backend
@pytest.mark.unit
def test_frame_without_run_id_is_accepted_as_legacy():
    mgr = _manager_with_satellite()
    mgr.begin_update_run("sat-1")
    assert mgr.accepts_update_frame("sat-1", None) is True


@pytest.mark.backend
@pytest.mark.unit
def test_frame_with_the_current_run_id_is_accepted():
    mgr = _manager_with_satellite()
    run_id = mgr.begin_update_run("sat-1")
    assert mgr.accepts_update_frame("sat-1", run_id) is True


@pytest.mark.backend
@pytest.mark.unit
def test_frame_with_another_run_id_is_dropped():
    mgr = _manager_with_satellite()
    old = mgr.begin_update_run("sat-1")
    mgr.begin_update_run("sat-1")
    assert mgr.accepts_update_frame("sat-1", old) is False
    # A non-string id (hostile/broken device) never matches either.
    assert mgr.accepts_update_frame("sat-1", {"id": old}) is False


@pytest.mark.backend
@pytest.mark.unit
def test_frame_with_run_id_but_no_run_on_record_is_dropped():
    """E.g. the backend restarted mid-run: this process never started it."""
    mgr = _manager_with_satellite()
    assert mgr.accepts_update_frame("sat-1", "abc") is False


@pytest.mark.backend
@pytest.mark.unit
def test_frame_with_run_id_from_unknown_satellite_is_dropped():
    mgr = _manager_with_satellite()
    assert mgr.accepts_update_frame("ghost", "abc") is False


# ==========================================================================
# 2. The scenario of the issue
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_a_stray_terminal_frame_of_an_old_run_does_not_end_the_new_run():
    """The wanted behaviour that the legacy test only records as a limit."""
    from ha_glue.api.websocket.satellite_handler import _belongs_to_current_run
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    old = mgr.begin_update_run("sat-1")
    mgr.apply_update_progress("sat-1", stage="failed", progress=0, message="alt")
    new = mgr.begin_update_run("sat-1")
    mgr.apply_update_progress("sat-1", stage="downloading", progress=50)

    straggler = {"type": "update_progress", "stage": "rolling_back",
                 "progress": 0, "message": "Rolling back...", "run_id": old}
    if _belongs_to_current_run(mgr, "sat-1", straggler):
        mgr.apply_update_progress("sat-1", stage=straggler["stage"], progress=0,
                                  message=straggler["message"])

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.IN_PROGRESS
    assert sat.update_stage == "downloading"
    assert sat.update_run_id == new

    # The real frame of the new run still ends it.
    assert _belongs_to_current_run(mgr, "sat-1", {"run_id": new}) is True


@pytest.mark.backend
@pytest.mark.unit
def test_all_three_ota_branches_go_through_the_run_gate():
    """Checked statically (the receive loop needs registration, auth and room
    resolution). Counted as CALL nodes, so prose cannot satisfy it."""
    import ha_glue.api.websocket.satellite_handler as sh

    tree = ast.parse(Path(sh.__file__).read_text())
    loop = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "satellite_websocket"
    )
    gated = set()
    for node in ast.walk(loop):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "msg_type" and isinstance(test.comparators[0], ast.Constant)):
            continue
        msg_type = test.comparators[0].value
        if not str(msg_type).startswith("update_"):
            continue
        first = node.body[0]
        calls = {
            c.func.id for c in ast.walk(first.test)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        } if isinstance(first, ast.If) else set()
        if "_belongs_to_current_run" in calls:
            gated.add(msg_type)
    assert gated == {"update_progress", "update_complete", "update_failed"}


# ==========================================================================
# 3. The id is sent in the update_request
# ==========================================================================


@pytest.mark.backend
async def test_update_request_carries_the_minted_run_id(monkeypatch):
    import ha_glue.services.satellite_update_service as svc_mod
    from ha_glue.services.satellite_update_service import SatelliteUpdateService

    mgr = _manager_with_satellite()
    ws = AsyncMock()
    mgr.get_satellite("sat-1").websocket = ws

    svc = SatelliteUpdateService()
    monkeypatch.setattr(svc, "is_update_available", lambda v: True)
    monkeypatch.setattr(
        svc, "get_package_info",
        lambda: {"version": "9.9.9", "checksum": "sha256:abc", "size": 10},
    )
    monkeypatch.setattr(svc_mod, "get_satellite_manager", lambda: mgr)
    monkeypatch.setattr(
        svc_mod, "ha_glue_settings",
        SimpleNamespace(satellite_ota_require_signature=False),
    )

    res = await svc.initiate_update("sat-1")

    assert res["success"] is True
    sent = ws.send_json.call_args.args[0]
    assert sent["type"] == "update_request"
    assert sent["run_id"] == mgr.get_satellite("sat-1").update_run_id
    assert sent["run_id"]
