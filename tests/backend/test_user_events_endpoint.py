"""Endpoint tests for /ws/user — auth branches + registry lifecycle.

The fan-out/delivery mechanics are covered in test_user_events.py; here we verify
the endpoint's connection handling: unauth is rejected, auth-off registers under
the _ALL bucket, an authed user registers under their user_id, a userless token is
rejected, and a disconnect cleans up.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.websocket.user_events_handler as handler
import services.user_events as ue

pytestmark = [pytest.mark.backend]


@pytest.fixture
def app_client(monkeypatch):
    ue.get_registry()._clients.clear()

    async def _not_admin(_uid):
        return False

    monkeypatch.setattr(handler, "_is_admin", _not_admin)

    app = FastAPI()
    app.include_router(handler.router)
    return TestClient(app)


def _patch_auth(monkeypatch, result):
    async def _fake_auth(_ws, _token=None):
        return result

    monkeypatch.setattr(handler, "authenticate_websocket", _fake_auth)


def _wait_count(expected, tries=50):
    for _ in range(tries):
        if ue.get_registry().client_count() == expected:
            return True
        time.sleep(0.01)
    return ue.get_registry().client_count() == expected


def test_unauthenticated_is_rejected(app_client, monkeypatch):
    _patch_auth(monkeypatch, None)
    with pytest.raises(Exception):  # noqa: B017 — starlette raises on server-side close pre-accept
        with app_client.websocket_connect("/ws/user"):
            pass
    assert ue.get_registry().client_count() == 0


def test_auth_off_registers_under_all(app_client, monkeypatch):
    _patch_auth(monkeypatch, {"authenticated": True, "auth_skipped": True})
    reg = ue.get_registry()
    with app_client.websocket_connect("/ws/user") as ws:
        assert _wait_count(1)
        # registered under the _ALL bucket → a None-target event reaches it
        assert reg._clients.get(ue.ALL)
        ws.send_text("ping")  # heartbeat, ignored
    assert _wait_count(0)


def test_auth_on_registers_under_user_id(app_client, monkeypatch):
    _patch_auth(monkeypatch, {"authenticated": True, "user_id": 42, "auth_method": "jwt"})
    reg = ue.get_registry()
    with app_client.websocket_connect("/ws/user") as ws:
        assert _wait_count(1)
        assert reg._clients.get(42)
        assert reg._clients.get(ue.ALL) is None  # non-admin → not in the broadcast bucket
        ws.send_text("ping")
    assert _wait_count(0)


def test_admin_also_registers_under_all(app_client, monkeypatch):
    _patch_auth(monkeypatch, {"authenticated": True, "user_id": 1, "auth_method": "jwt"})

    async def _is_admin(_uid):
        return True

    monkeypatch.setattr(handler, "_is_admin", _is_admin)
    reg = ue.get_registry()
    with app_client.websocket_connect("/ws/user") as ws:
        assert _wait_count(1)
        assert reg._clients.get(1)
        assert reg._clients.get(ue.ALL)  # admin joins the broadcast bucket
        ws.send_text("ping")
    assert _wait_count(0)


def test_userless_token_is_rejected(app_client, monkeypatch):
    # e.g. a device/voice token dict with no user_id
    _patch_auth(monkeypatch, {"authenticated": True, "device_id": "sat-1"})
    with pytest.raises(Exception):  # noqa: B017
        with app_client.websocket_connect("/ws/user"):
            pass
    assert ue.get_registry().client_count() == 0
