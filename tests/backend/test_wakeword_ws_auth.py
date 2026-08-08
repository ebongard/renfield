"""Security test for the /ws/wakeword authentication gate (audit).

The server-side wake-word WebSocket used to accept() with NO authentication —
any client could stream audio into the OpenWakeWord service (compute-DoS). It
now runs authenticate_websocket before accept, like every other WS endpoint.
This asserts the reject path: WS auth on + no token → close, never accept.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wakeword_ws_rejects_unauthenticated(monkeypatch):
    import main
    from utils.config import settings as cfg

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.headers = {}  # no Authorization header either

    monkeypatch.setattr(cfg, "ws_auth_enabled", True, raising=False)

    # No token + WS auth on → authenticate_websocket returns None (no DB touched)
    # → the endpoint must close_unauthorized and NOT accept the socket.
    await main.wakeword_websocket(ws, token=None)

    ws.accept.assert_not_called()
    ws.close.assert_awaited()  # close_unauthorized(ws) → ws.close(code=4001)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wakeword_ws_skips_auth_when_disabled(monkeypatch):
    """WS auth OFF (single-user) must NOT reject on the auth gate — the endpoint
    proceeds to accept() (its wake-word service handling is out of scope here)."""
    import main
    from utils.config import settings as cfg

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive_bytes = AsyncMock(side_effect=Exception("stop after accept"))
    ws.headers = {}

    monkeypatch.setattr(cfg, "ws_auth_enabled", False, raising=False)

    # Should not raise before accept(); we don't care how the service loop ends.
    try:
        await main.wakeword_websocket(ws, token=None)
    except Exception:
        pass

    ws.accept.assert_awaited()  # auth skipped → connection accepted
