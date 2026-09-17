"""VOICE_BROWSER_CLIENT_ID — the registry client id the browser sends as
``?client=`` on /ws/voice (browser voice on auth-on instances, e.g. xidra).

Pins: the setting defaults to "" (household byte-identical: no parameter), env
passes through, a value that is not URL-query-safe is rejected at boot, the id
reaches the frontend via ``/api/config/features`` as ``voice_client_id``, and a
voice+auth instance without it warns loudly.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from loguru import logger
from pydantic import ValidationError

from utils.config import Settings, settings


def test_default_is_empty(monkeypatch):
    monkeypatch.delenv("VOICE_BROWSER_CLIENT_ID", raising=False)
    assert Settings(_env_file=None).voice_browser_client_id == ""


def test_env_passthrough(monkeypatch):
    monkeypatch.setenv("VOICE_BROWSER_CLIENT_ID", "xidra")
    assert Settings(_env_file=None).voice_browser_client_id == "xidra"


def test_distinct_from_backend_voice_client_id(monkeypatch):
    """The backend→voice-server header id and the browser id are different
    registry rows on the household (anonymous vs verify) — never aliased."""
    monkeypatch.setenv("VOICE_CLIENT_ID", "renfield")
    monkeypatch.delenv("VOICE_BROWSER_CLIENT_ID", raising=False)
    s = Settings(_env_file=None)
    assert s.voice_client_id == "renfield"
    assert s.voice_browser_client_id == ""


@pytest.mark.parametrize(
    "bad",
    ["Xidra", "xidra&token=evil", "xi dra", "a/b", "x" * 65, "xidra\n"],
)
def test_invalid_id_rejected(monkeypatch, bad):
    monkeypatch.setenv("VOICE_BROWSER_CLIENT_ID", bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("good", ["", "xidra", "reva-prod_2", "x" * 64])
def test_valid_ids_accepted(monkeypatch, good):
    monkeypatch.setenv("VOICE_BROWSER_CLIENT_ID", good)
    assert Settings(_env_file=None).voice_browser_client_id == good


def _capture_warnings():
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


def test_warns_when_voice_and_auth_on_without_id(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("FEATURE_VOICE", "true")
    monkeypatch.delenv("VOICE_BROWSER_CLIENT_ID", raising=False)
    messages, sink_id = _capture_warnings()
    try:
        Settings(_env_file=None)
    finally:
        logger.remove(sink_id)
    assert any("VOICE_BROWSER_CLIENT_ID" in m for m in messages)


@pytest.mark.parametrize(
    ("auth", "voice", "client_id"),
    [("false", "true", ""), ("true", "false", ""), ("true", "true", "xidra")],
)
def test_no_warning_otherwise(monkeypatch, auth, voice, client_id):
    monkeypatch.setenv("AUTH_ENABLED", auth)
    monkeypatch.setenv("FEATURE_VOICE", voice)
    monkeypatch.setenv("VOICE_BROWSER_CLIENT_ID", client_id)
    messages, sink_id = _capture_warnings()
    try:
        Settings(_env_file=None)
    finally:
        logger.remove(sink_id)
    assert not any("VOICE_BROWSER_CLIENT_ID" in m for m in messages)


def _auth_default(app) -> None:
    from models.database import User
    from services.auth_service import get_user_or_default
    app.dependency_overrides[get_user_or_default] = lambda: User(
        id=1, username="t", password_hash="x", is_active=True, role_id=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "xidra"])
async def test_features_exposes_voice_client_id(monkeypatch, value):
    monkeypatch.setattr(settings, "voice_browser_client_id", value)
    from main import app
    _auth_default(app)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/config/features")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["voice_client_id"] == value
