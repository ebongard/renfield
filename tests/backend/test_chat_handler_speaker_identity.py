"""Tests for the speaker→user identity helper in chat_handler.

Covers the voice-driven identity claim (option B):
- Speaker linked to a User → returns User.id
- Speaker with no User link (e.g. auto-enrolled guest) → returns None

The full WS handler integration is exercised by the live cluster
validation; these tests pin the contract on the small lookup helper.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lookup_returns_user_id_when_speaker_linked(monkeypatch) -> None:
    fake_result = MagicMock()
    fake_result.scalar_one_or_none.return_value = 42

    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(return_value=fake_result)

    @asynccontextmanager
    async def fake_session_local():
        yield fake_session

    monkeypatch.setattr(
        "api.websocket.chat_handler.AsyncSessionLocal",
        fake_session_local,
    )

    from api.websocket.chat_handler import _lookup_user_id_for_speaker

    user_id = await _lookup_user_id_for_speaker(speaker_id=7)
    assert user_id == 42
    fake_session.execute.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lookup_returns_none_when_speaker_unlinked(monkeypatch) -> None:
    """Auto-enrolled guest speaker (no User row references it) → None."""
    fake_result = MagicMock()
    fake_result.scalar_one_or_none.return_value = None

    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(return_value=fake_result)

    @asynccontextmanager
    async def fake_session_local():
        yield fake_session

    monkeypatch.setattr(
        "api.websocket.chat_handler.AsyncSessionLocal",
        fake_session_local,
    )

    from api.websocket.chat_handler import _lookup_user_id_for_speaker

    user_id = await _lookup_user_id_for_speaker(speaker_id=999)
    assert user_id is None
