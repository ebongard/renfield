"""Chat-TTS output routing: a room with a DLNA renderer must get the answer
delivered to the renderer (server-side), not punted to the browser.

Regression: `ha_route_chat_tts_to_device_output` used to gate server-side
delivery on `target_type == "homeassistant"` only, so a room whose output device
is a DLNA renderer (e.g. a HiFiBerry) fell through to `return False` → the
answer played in the BROWSER instead of the room speaker, and the browser's
sentence-chunked voice stream then cut off after sentence 1 (open mic → barge-in
cancelled the rest). `audio_output_service.play_audio` already dispatches DLNA vs
HA by device type; the fix lets `target_type == "dlna"` through too.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_glue.services.chat_voice_handlers import ha_route_chat_tts_to_device_output


def _decision(target_type: str, *, fallback: bool = False, has_device: bool = True):
    d = MagicMock()
    d.target_type = target_type
    d.fallback_to_input = fallback
    d.output_device = MagicMock() if has_device else None
    d.target_id = f"{target_type}-dev"
    d.reason = "device_available"
    return d


@asynccontextmanager
async def _fake_session():
    yield MagicMock()


async def _run(decision, *, play_success: bool = True, tts_bytes: bytes = b"RIFFxxxxWAVE"):
    routing = MagicMock()
    routing.get_audio_output_for_room = AsyncMock(return_value=decision)
    piper = MagicMock()
    piper.synthesize_to_bytes = AsyncMock(return_value=tts_bytes)
    audio_out = MagicMock()
    audio_out.play_audio = AsyncMock(return_value=play_success)
    with patch("services.database.AsyncSessionLocal", _fake_session), \
         patch("ha_glue.services.output_routing_service.OutputRoutingService", return_value=routing), \
         patch("services.piper_service.get_piper_service", return_value=piper), \
         patch("ha_glue.services.audio_output_service.get_audio_output_service", return_value=audio_out):
        result = await ha_route_chat_tts_to_device_output(
            room_context={"room_id": 5, "device_id": 1},
            response_text="Satz eins. Satz zwei. Satz drei.",
        )
    return result, audio_out, piper


@pytest.mark.unit
async def test_dlna_target_delivers_server_side() -> None:
    """The core fix: a DLNA renderer gets the full clip + tts_handled=True."""
    result, audio_out, piper = await _run(_decision("dlna"))
    assert result is True
    audio_out.play_audio.assert_awaited_once()
    # full answer synthesized as one clip (not sentence-chunked)
    piper.synthesize_to_bytes.assert_awaited_once_with("Satz eins. Satz zwei. Satz drei.")


@pytest.mark.unit
async def test_homeassistant_target_still_delivers() -> None:
    result, audio_out, _ = await _run(_decision("homeassistant"))
    assert result is True
    audio_out.play_audio.assert_awaited_once()


@pytest.mark.unit
async def test_renfield_target_falls_back_to_browser() -> None:
    """A renfield device IS the input device → browser plays (return False)."""
    result, audio_out, _ = await _run(_decision("renfield"))
    assert result is False
    audio_out.play_audio.assert_not_awaited()


@pytest.mark.unit
async def test_dlna_play_failure_falls_back_to_browser() -> None:
    result, audio_out, _ = await _run(_decision("dlna"), play_success=False)
    assert result is False
    audio_out.play_audio.assert_awaited_once()


@pytest.mark.unit
async def test_fallback_to_input_does_not_deliver() -> None:
    result, audio_out, _ = await _run(_decision("dlna", fallback=True))
    assert result is False
    audio_out.play_audio.assert_not_awaited()


@pytest.mark.unit
async def test_no_room_returns_none() -> None:
    result = await ha_route_chat_tts_to_device_output(room_context={}, response_text="x")
    assert result is None
