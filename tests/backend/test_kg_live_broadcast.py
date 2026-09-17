"""Security review H3 — the live KG socket must not leak across users.

The broadcast was anonymous + unfiltered: any connected viewer received every
household member's freshly-extracted entity names. The fix authenticates the
socket and scopes each broadcast to the owner of the extracted data when WS auth
is enabled. These unit tests pin the broadcast filter.
"""
from __future__ import annotations

import pytest

import api.websocket.kg_live_handler as kg_live
from api.websocket.kg_live_handler import broadcast_kg_update
from utils.config import settings


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _clear_viewers():
    kg_live._viewers.clear()
    yield
    kg_live._viewers.clear()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_auth_on_only_owner_receives(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    owner_ws, other_ws = _FakeWS(), _FakeWS()
    kg_live._viewers[owner_ws] = 1   # owner
    kg_live._viewers[other_ws] = 2   # different user

    await broadcast_kg_update(
        entities=[{"id": 10, "name": "Geheim", "type": "person"}],
        relations=[],
        owner_user_id=1,
    )

    assert len(owner_ws.sent) == 1
    assert other_ws.sent == []  # the leak is closed


@pytest.mark.backend
@pytest.mark.asyncio
async def test_auth_on_none_user_viewer_gets_nothing(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    anon_ws = _FakeWS()
    kg_live._viewers[anon_ws] = None  # device-token / unidentified

    await broadcast_kg_update(
        entities=[{"id": 1, "name": "X", "type": "person"}],
        relations=[],
        owner_user_id=5,
    )
    assert anon_ws.sent == []


@pytest.mark.backend
@pytest.mark.asyncio
async def test_auth_on_unknown_owner_sends_to_no_one(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    ws = _FakeWS()
    kg_live._viewers[ws] = 1

    await broadcast_kg_update(
        entities=[{"id": 1, "name": "X", "type": "person"}],
        relations=[],
        owner_user_id=None,  # fail-closed
    )
    assert ws.sent == []


@pytest.mark.backend
@pytest.mark.asyncio
async def test_auth_off_broadcasts_to_all(monkeypatch):
    """Single-user/household mode keeps the legacy fan-to-all behavior."""
    monkeypatch.setattr(settings, "auth_enabled", False)
    a, b = _FakeWS(), _FakeWS()
    kg_live._viewers[a] = None
    kg_live._viewers[b] = None

    await broadcast_kg_update(
        entities=[{"id": 1, "name": "X", "type": "person"}],
        relations=[],
        owner_user_id=7,
    )
    assert len(a.sent) == 1 and len(b.sent) == 1


@pytest.mark.backend
@pytest.mark.asyncio
async def test_broken_viewer_is_evicted(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)

    class _BrokenWS(_FakeWS):
        async def send_json(self, msg):
            raise RuntimeError("closed")

    bad = _BrokenWS()
    kg_live._viewers[bad] = None
    await broadcast_kg_update(
        entities=[{"id": 1, "name": "X", "type": "p"}], relations=[]
    )
    assert bad not in kg_live._viewers
