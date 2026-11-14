"""Kiosk peer-status refresher — the diff-gated ``peer_status_changed`` delta.

Closes the deferred kiosk peer-liveness gap: a backend timer recomputes
federation-peer reachability and pushes only on change, so the wall board's
peer nodes go green/red live instead of only on reconnect.
"""
from __future__ import annotations

import pytest

import api.websocket.kiosk_data as kiosk_data
import api.websocket.kiosk_handler as handler
from api.websocket.kiosk_data import refresh_and_push_peer_status, reset_peer_status_gate

pytestmark = pytest.mark.asyncio


def _stub_peers(monkeypatch, seq):
    """compute_peer_status returns successive values from `seq` (last repeats)."""
    calls = {"i": 0}

    async def _compute():
        v = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return v

    monkeypatch.setattr(kiosk_data, "compute_peer_status", _compute)


async def test_diff_gate_pushes_only_on_change(monkeypatch):
    pushed = []

    async def _capture(ev):
        pushed.append(ev)

    monkeypatch.setattr(handler, "broadcast_kiosk_event", _capture)
    kiosk_data._peer_status_last_pushed = None

    offline = [{"id": 1, "name": "xidra", "last_seen_at": None, "reachable": False}]
    online = [{"id": 1, "name": "xidra", "last_seen_at": "2026-07-13T00:00:00+00:00", "reachable": True}]
    _stub_peers(monkeypatch, [offline, offline, online])

    await refresh_and_push_peer_status()  # first → push
    await refresh_and_push_peer_status()  # unchanged → no push
    await refresh_and_push_peer_status()  # reachability flipped → push

    assert len(pushed) == 2
    assert pushed[0]["type"] == "peer_status_changed"
    assert pushed[0]["peers"] == offline
    assert pushed[1]["peers"] == online


async def test_the_push_carries_the_servers_clock_outside_the_peer_list(monkeypatch):
    """The display measures the age of a `checked_at` against `at` -- the
    server's own clock at the push -- and never against its own wall clock.
    `at` sits beside `peers`, so it cannot defeat the diff gate."""
    from datetime import UTC, datetime

    pushed = []

    async def _capture(ev):
        pushed.append(ev)

    monkeypatch.setattr(handler, "broadcast_kiosk_event", _capture)
    kiosk_data._peer_status_last_pushed = None
    same = [{"id": 1, "name": "x", "last_seen_at": None, "reachable": False}]
    _stub_peers(monkeypatch, [same, same])

    before = datetime.now(UTC)
    await refresh_and_push_peer_status()
    await refresh_and_push_peer_status()  # same peers, a later clock: no push

    assert len(pushed) == 1
    assert set(pushed[0]) == {"type", "at", "peers"}
    at = datetime.fromisoformat(pushed[0]["at"])
    assert at.tzinfo is not None, "an offset, so a browser cannot read it as local time"
    assert before <= at <= datetime.now(UTC)


def _checked(at: str, state: str = "reachable") -> list[dict]:
    return [{
        "id": 1, "name": "x", "last_seen_at": None, "reachable": state == "reachable",
        "reachability": state, "reachability_reason": None, "checked_at": at,
        "recently_active": False,
    }]


async def test_a_later_check_with_the_same_result_is_no_change(monkeypatch):
    """Every check moves `checked_at` (once per 5 minutes and pairing). Nothing
    a person can see has changed, so nothing is pushed -- while a changed
    STATE with the very same times is."""
    from utils.config import settings

    pushed = []

    async def _capture(ev):
        pushed.append(ev)

    monkeypatch.setattr(handler, "broadcast_kiosk_event", _capture)
    monkeypatch.setattr(settings, "federation_reachability_enabled", True)
    kiosk_data._peer_status_last_pushed = None
    _stub_peers(monkeypatch, [
        _checked("2026-11-13T10:00:00"),
        _checked("2026-11-13T10:05:00"),
        _checked("2026-11-13T10:10:00"),
        _checked("2026-11-13T10:10:00", state="unreachable"),
    ])

    for _ in range(4):
        await refresh_and_push_peer_status()

    assert [ev["peers"][0]["checked_at"] for ev in pushed] == [
        "2026-11-13T10:00:00", "2026-11-13T10:10:00"]
    assert [ev["peers"][0]["reachability"] for ev in pushed] == ["reachable", "unreachable"]
    assert all("checked_at" not in peer for peer in kiosk_data._peer_status_last_pushed)


@pytest.mark.parametrize(("flag", "pushes"), [(True, 2), (False, 1)])
async def test_an_unchanged_list_is_reaffirmed_only_with_the_check_on(monkeypatch, flag, pushes):
    """With the signed check on, the display counts a verdict as stale when it
    has heard nothing for 20 minutes. "Nothing changed" must therefore be SAID
    now and then -- every 10 minutes, with the current `checked_at` -- or a
    quiet evening would turn every peer to "unknown". With the check off there
    is no such verdict and the gate is the plain diff gate it was."""
    import time

    from utils.config import settings

    pushed = []

    async def _capture(ev):
        pushed.append(ev)

    monkeypatch.setattr(handler, "broadcast_kiosk_event", _capture)
    monkeypatch.setattr(settings, "federation_reachability_enabled", flag)
    kiosk_data._peer_status_last_pushed = None
    assert kiosk_data._PEER_STATUS_REAFFIRM_SECONDS == 600
    _stub_peers(monkeypatch, [_checked("2026-11-13T10:00:00"), _checked("2026-11-13T10:10:00")])

    await refresh_and_push_peer_status()            # first -> push
    await refresh_and_push_peer_status()            # unchanged, seconds later -> nothing
    assert len(pushed) == 1

    monkeypatch.setattr(kiosk_data, "_peer_status_last_pushed_at", time.monotonic() - 599)
    await refresh_and_push_peer_status()            # not yet
    assert len(pushed) == 1

    monkeypatch.setattr(kiosk_data, "_peer_status_last_pushed_at", time.monotonic() - 601)
    await refresh_and_push_peer_status()
    assert len(pushed) == pushes
    if flag:
        assert pushed[1]["peers"][0]["checked_at"] == "2026-11-13T10:10:00"
        await refresh_and_push_peer_status()        # the beat restarted
        assert len(pushed) == 2


async def test_an_empty_list_is_never_reaffirmed(monkeypatch):
    import time

    from utils.config import settings

    pushed = []

    async def _capture(ev):
        pushed.append(ev)

    monkeypatch.setattr(handler, "broadcast_kiosk_event", _capture)
    monkeypatch.setattr(settings, "federation_reachability_enabled", True)
    kiosk_data._peer_status_last_pushed = None
    _stub_peers(monkeypatch, [[]])
    await refresh_and_push_peer_status()
    monkeypatch.setattr(kiosk_data, "_peer_status_last_pushed_at", time.monotonic() - 3600)
    await refresh_and_push_peer_status()
    assert len(pushed) == 1


async def test_failed_broadcast_does_not_advance_gate(monkeypatch):
    async def _flaky(ev):
        raise RuntimeError("ws down")

    monkeypatch.setattr(handler, "broadcast_kiosk_event", _flaky)
    kiosk_data._peer_status_last_pushed = None
    _stub_peers(monkeypatch, [[{"id": 1, "name": "x", "last_seen_at": None, "reachable": False}]])

    await refresh_and_push_peer_status()  # broadcast raises → gate must NOT advance
    assert kiosk_data._peer_status_last_pushed is None


async def test_reset_gate_forces_repush(monkeypatch):
    kiosk_data._peer_status_last_pushed = [{"id": 1}]
    reset_peer_status_gate()
    assert kiosk_data._peer_status_last_pushed is None
