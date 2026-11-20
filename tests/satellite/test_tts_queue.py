"""TTS clips queue on the satellite instead of the second being dropped (#1576).

The backend may send a short filler ("Moment, ich schaue nach.") with
``is_final=False`` before the answer. Before 1.4.22 the player refused a clip
while another one played, so the answer behind a filler was lost. Now the
clips play one after the other, in arrival order, and the session resets only
after the final one. The device advertises this as ``tts_queue``.
"""

from __future__ import annotations

import asyncio

import pytest
from renfield_satellite.network.websocket_client import WebSocketClient
from renfield_satellite.satellite import Satellite

pytestmark = pytest.mark.satellite


class _Player:
    """A player that, like the real one, refuses a clip while one plays."""

    def __init__(self) -> None:
        self.playing = False
        self.played: list[bytes] = []
        self.refused: list[bytes] = []

    async def play_wav(self, data: bytes) -> bool:
        if self.playing:
            self.refused.append(data)
            return False
        self.playing = True
        try:
            await asyncio.sleep(0.05)
            self.played.append(data)
            return True
        finally:
            self.playing = False


def _satellite(player: _Player) -> tuple[Satellite, list[str]]:
    sat = object.__new__(Satellite)
    sat.audio_playback_async = player
    sat._tts_lock = None
    resets: list[str] = []

    async def _reset(reason: str = "reset") -> None:
        resets.append(f"{reason}@{len(player.played)}")

    sat._reset_session = _reset
    return sat, resets


async def test_a_second_clip_queues_behind_the_first() -> None:
    player = _Player()
    sat, resets = _satellite(player)
    filler = asyncio.create_task(sat._play_tts_and_reset(b"filler", False))
    await asyncio.sleep(0)  # the filler is playing
    answer = asyncio.create_task(sat._play_tts_and_reset(b"answer", True))
    await asyncio.gather(filler, answer)

    assert player.refused == []
    assert player.played == [b"filler", b"answer"]
    # One reset, after the answer -- never after the filler.
    assert resets == ["tts_complete@2"]


async def test_negative_control_without_the_lock_the_answer_is_dropped() -> None:
    """The fixture player reproduces the drop the queue prevents."""
    player = _Player()
    filler = asyncio.create_task(player.play_wav(b"filler"))
    await asyncio.sleep(0)
    assert await player.play_wav(b"answer") is False
    await filler
    assert player.refused == [b"answer"]


async def test_a_single_final_clip_plays_and_resets_as_before() -> None:
    player = _Player()
    sat, resets = _satellite(player)
    await sat._play_tts_and_reset(b"answer", True)
    assert player.played == [b"answer"]
    assert resets == ["tts_complete@1"]


async def test_register_frame_advertises_tts_queue() -> None:
    client = object.__new__(WebSocketClient)
    sent: list[dict] = []
    client.satellite_id = "sat-test"
    client.room = "Testraum"
    client.language = "de"
    client._capabilities = {}
    client._protocol_version = "1.0"
    client._enrollment_token = None
    client._requested_audio_codec = "pcm"

    async def _send(message: dict) -> None:
        sent.append(message)
        raise RuntimeError("stop after the register frame")

    client._send = _send
    with pytest.raises(RuntimeError):
        await client._register()
    assert sent[0]["capabilities"]["tts_queue"] is True
