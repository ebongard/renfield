"""`announce_in_room` hook handler (ha_glue): room id → spoken text in that room.

Core code (the scan-job return path) addresses rooms by id and cannot import the
room/TTS layer; this handler bridges the two and must stay a public announcement.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit]


class _Session:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *exc):
        return False


async def test_known_room_is_announced_by_name_and_public():
    from ha_glue.services.announce_hooks import ha_announce_in_room

    rooms = MagicMock()
    rooms.get_room = AsyncMock(return_value=SimpleNamespace(name="Arbeitszimmer"))
    tools = MagicMock()
    tools._announce_core = AsyncMock(return_value={"success": True})

    with patch("services.database.AsyncSessionLocal", _Session), \
         patch("ha_glue.services.room_service.RoomService", MagicMock(return_value=rooms)), \
         patch("ha_glue.services.internal_tools.InternalToolService", MagicMock(return_value=tools)):
        result = await ha_announce_in_room(room_id=4, text="Der Scan ist fertig.")

    assert result == {"success": True}
    tools._announce_core.assert_awaited_once_with(
        room_name="Arbeitszimmer", text="Der Scan ist fertig.", audio_bytes=None,
        privacy="public", for_users=[], force=False,
    )


async def test_unknown_room_announces_nothing():
    from ha_glue.services.announce_hooks import ha_announce_in_room

    rooms = MagicMock()
    rooms.get_room = AsyncMock(return_value=None)
    tools = MagicMock()
    tools._announce_core = AsyncMock()

    with patch("services.database.AsyncSessionLocal", _Session), \
         patch("ha_glue.services.room_service.RoomService", MagicMock(return_value=rooms)), \
         patch("ha_glue.services.internal_tools.InternalToolService", MagicMock(return_value=tools)):
        result = await ha_announce_in_room(room_id=99, text="x")

    assert result["success"] is False
    tools._announce_core.assert_not_called()


def test_announce_in_room_is_a_known_hook_event():
    from utils.hooks import HOOK_EVENTS

    assert "announce_in_room" in HOOK_EVENTS
