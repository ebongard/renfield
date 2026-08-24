"""WebSocket hardening (login audit #1116, Tier-2 Batch B).

#6 — inbound WS frame size cap. The prod uvicorn launch
(``k8s/backend.yaml`` args + the Dockerfile CMD) passes
``--ws-max-size 1000000`` so an oversized frame is dropped at the protocol
layer before any /ws handler parses it; ``main.py``'s direct uvicorn.run uses
``settings.ws_max_message_size``. All legitimate inbound frames are small
(chat = text + attachment IDs; attachments upload via REST; audio streams in
~KB chunks), so 1 MB is generous.
"""
from utils.config import settings


def test_ws_max_message_size_value():
    """Guard the value the launch args hardcode: the k8s/Dockerfile
    ``--ws-max-size`` is a literal 1000000 (CLI args can't read the config), so
    if this config changes the launch args (k8s/backend.yaml + Dockerfile) must
    be updated in lockstep. main.py's uvicorn.run reads the config directly."""
    assert settings.ws_max_message_size == 1_000_000


# #7 — CSWSH Origin allowlist on the WS handshake.

def _fake_ws(origin=None):
    from unittest.mock import MagicMock
    m = MagicMock()
    m.headers = {"origin": origin} if origin else {}
    return m


def test_ws_origin_allowlist(monkeypatch):
    """#1116: browser Origin must be in cors_origins; missing Origin (non-browser
    satellite/device) is allowed; cors_origins='*' skips the check."""
    from services import websocket_auth as wa

    # dev/permissive: allow everything
    monkeypatch.setattr(wa.settings, "cors_origins", "*")
    assert wa._ws_origin_allowed(_fake_ws("https://evil.example")) is True

    # pinned (xidra-style): SPA origin allowed, cross-origin rejected, no-Origin exempt
    monkeypatch.setattr(wa.settings, "cors_origins", "https://x-ren.local")
    assert wa._ws_origin_allowed(_fake_ws("https://x-ren.local")) is True
    assert wa._ws_origin_allowed(_fake_ws("https://evil.example")) is False
    assert wa._ws_origin_allowed(_fake_ws(None)) is True  # satellite/device: no Origin

    # comma-separated allowlist
    monkeypatch.setattr(wa.settings, "cors_origins", "https://a.local, https://b.local")
    assert wa._ws_origin_allowed(_fake_ws("https://b.local")) is True
    assert wa._ws_origin_allowed(_fake_ws("https://c.local")) is False
