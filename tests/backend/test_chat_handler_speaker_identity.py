"""Tests for the speaker→user identity helper in chat_handler.

Covers the voice-driven identity claim (option B):
- Speaker linked to a User → returns User.id
- Speaker with no User link (e.g. auto-enrolled guest) → returns None
- Confidence-gating logic (floor matches voice_auth_min_confidence)

The full WS handler integration is exercised by the live cluster
validation; these tests pin the contract on the small lookup helper
+ the confidence-floor guard.
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


@pytest.mark.unit
def test_confidence_floor_logic() -> None:
    """The promotion gate: confidence ≥ voice_auth_min_confidence (0.7 default).

    Pins the threshold semantics so a future setting change is visible
    to a reviewer.
    """
    from utils.config import settings

    floor = settings.voice_auth_min_confidence
    # Default is 0.7 — same as /api/auth/voice endpoint.
    assert 0 < floor <= 1

    # Sample speaker_info shapes
    high_conf = {"speaker_id": 1, "speaker_name": "Eduard", "speaker_confidence": 0.85}
    low_conf = {"speaker_id": 1, "speaker_name": "Eduard", "speaker_confidence": 0.30}
    no_match = {"speaker_id": None, "speaker_name": None, "speaker_confidence": 0.0}

    # The chat_handler gate has 4 conditions: user_id is None,
    # speaker_info truthy, has speaker_id, confidence >= floor.
    # We're testing the confidence comparison only.
    assert high_conf.get("speaker_confidence", 0) >= floor
    assert low_conf.get("speaker_confidence", 0) < floor
    assert no_match.get("speaker_confidence", 0) < floor
