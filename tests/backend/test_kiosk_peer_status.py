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
