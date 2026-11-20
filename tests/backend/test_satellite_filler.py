"""A spoken filler on a slow satellite voice turn (#1576).

Two layers:

* the timer (`SatelliteFiller`): one filler at most, none when the work ends
  first, never cut halfway through its frame, a failure costs only the filler;
* the real `satellite_websocket` turn: a slow action gets the filler BEFORE
  the answer (`is_final=False`, then the answer with `is_final=True`); a fast
  one, a satellite without `tts_queue`, a language without its own voice and
  the switch off get exactly the one answer as before.
"""
from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_glue.api.websocket import satellite_handler as handler
from ha_glue.services import satellite_filler as filler_mod
from ha_glue.services.satellite_filler import (
    PHRASES,
    SatelliteFiller,
    filler_phrase,
    filler_wanted,
)
from ha_glue.services.satellite_manager import SatelliteCapabilities
from ha_glue.utils.config import ha_glue_settings
from services.ollama_service import OllamaService
from utils.config import settings

# ---------------------------------------------------------------------------
# The timer
# ---------------------------------------------------------------------------


def _timer(after: float = 0.05, *, render=None, send=None) -> tuple[SatelliteFiller, list[bytes]]:
    sent: list[bytes] = []

    async def _render() -> bytes:
        return b"filler"

    async def _send(audio: bytes) -> None:
        sent.append(audio)

    return SatelliteFiller(after_seconds=after, render=render or _render, send=send or _send), sent


@pytest.mark.unit
async def test_work_that_ends_first_gets_no_filler() -> None:
    timer, sent = _timer(after=0.2)
    timer.start()
    await asyncio.sleep(0.05)
    await timer.stop()
    await asyncio.sleep(0.25)
    assert sent == [] and timer.sent is False


@pytest.mark.unit
async def test_slow_work_gets_exactly_one_filler() -> None:
    timer, sent = _timer(after=0.05)
    timer.start()
    timer.start()  # a second start is ignored
    await asyncio.sleep(0.2)
    await timer.stop()
    assert sent == [b"filler"] and timer.sent is True


@pytest.mark.unit
async def test_answer_ready_while_the_phrase_renders_sends_nothing() -> None:
    rendering = asyncio.Event()

    async def _slow_render() -> bytes:
        rendering.set()
        await asyncio.sleep(0.3)
        return b"filler"

    timer, sent = _timer(after=0.01, render=_slow_render)
    timer.start()
    await rendering.wait()
    await timer.stop()
    assert sent == []


@pytest.mark.unit
async def test_stop_waits_for_a_filler_being_sent() -> None:
    """A filler halfway through its frame is finished, never cancelled."""
    in_send = asyncio.Event()
    finished: list[bool] = []

    async def _slow_send(_audio: bytes) -> None:
        in_send.set()
        await asyncio.sleep(0.1)
        finished.append(True)

    timer, _ = _timer(after=0.01, send=_slow_send)
    timer.start()
    await in_send.wait()
    await timer.stop()
    assert finished == [True] and timer.sent is True


@pytest.mark.unit
async def test_the_turns_own_send_does_not_interleave_with_the_filler() -> None:
    order: list[str] = []
    in_send = asyncio.Event()

    async def _slow_send(_audio: bytes) -> None:
        in_send.set()
        order.append("filler-begin")
        await asyncio.sleep(0.05)
        order.append("filler-end")

    timer, _ = _timer(after=0.01, send=_slow_send)
    timer.start()
    await in_send.wait()
    async with timer.exclusive():
        order.append("action-frame")
    await timer.stop()
    assert order == ["filler-begin", "filler-end", "action-frame"]


@pytest.mark.unit
@pytest.mark.parametrize("where", ["render", "send"])
async def test_a_failing_filler_costs_only_the_filler(where: str) -> None:
    async def _boom(*_a) -> bytes:
        raise RuntimeError("tts down")

    kw = {where: _boom}
    timer, sent = _timer(after=0.01, **kw)
    timer.start()
    await asyncio.sleep(0.1)
    await timer.stop()  # does not raise
    assert sent == [] and timer.sent is False


@pytest.mark.unit
async def test_stop_without_start_is_harmless() -> None:
    timer, sent = _timer()
    await timer.stop()
    await timer.stop()
    assert sent == []


# ---------------------------------------------------------------------------
# Who gets one
# ---------------------------------------------------------------------------


def _info(tts_queue: bool) -> MagicMock:
    info = MagicMock()
    info.capabilities = SatelliteCapabilities(tts_queue=tts_queue)
    return info


@pytest.mark.unit
def test_switch_off_means_no_filler(monkeypatch) -> None:
    monkeypatch.setattr(ha_glue_settings, "satellite_filler_enabled", False)
    assert filler_wanted(_info(True), "de") is None


@pytest.mark.unit
def test_an_older_satellite_gets_none_it_would_drop_the_answer(monkeypatch) -> None:
    monkeypatch.setattr(ha_glue_settings, "satellite_filler_enabled", True)
    assert filler_wanted(_info(False), "de") is None
    assert filler_wanted(None, "de") is None


@pytest.mark.unit
def test_a_language_without_its_own_voice_gets_none(monkeypatch) -> None:
    monkeypatch.setattr(ha_glue_settings, "satellite_filler_enabled", True)
    monkeypatch.setattr(settings, "piper_voices", "de:de_DE-thorsten-high,en:en_US-amy-medium")
    assert filler_wanted(_info(True), "de") == PHRASES["de"]
    assert filler_wanted(_info(True), "en") == PHRASES["en"]
    # Italian has a phrase but no voice: the German fallback voice would read it.
    assert filler_wanted(_info(True), "it") is None
    monkeypatch.setattr(settings, "piper_voices", "de:de_DE-thorsten-high,it:it_IT-paola-medium")
    assert filler_phrase("it") == PHRASES["it"]
    assert filler_phrase("fr") is None


@pytest.mark.unit
async def test_register_reads_the_tts_queue_capability() -> None:
    from ha_glue.services.satellite_manager import SatelliteManager

    mgr = SatelliteManager()
    ws = MagicMock()
    ws.send_json = AsyncMock()
    assert await mgr.register("sat-new", "Küche", ws, {"tts_queue": True})
    assert await mgr.register("sat-old", "Bad", ws, {})
    assert mgr.satellites["sat-new"].capabilities.tts_queue is True
    assert mgr.satellites["sat-old"].capabilities.tts_queue is False


@pytest.mark.unit
async def test_warm_renders_the_phrase_into_the_cache(monkeypatch) -> None:
    piper = MagicMock()
    piper.synthesize_to_bytes = AsyncMock(return_value=b"wav")
    monkeypatch.setattr("services.piper_service.get_piper_service", lambda: piper)
    await filler_mod.warm("de")
    piper.synthesize_to_bytes.assert_awaited_once_with(PHRASES["de"], language="de")
    piper.synthesize_to_bytes.side_effect = RuntimeError("down")
    await filler_mod.warm("de")  # best effort, no raise


@pytest.mark.unit
async def test_warm_in_background_only_where_a_filler_is_used(monkeypatch) -> None:
    calls: list[str | None] = []

    async def _warm(language):
        calls.append(language)

    monkeypatch.setattr(filler_mod, "warm", _warm)
    monkeypatch.setattr(settings, "piper_voices", "de:de_DE-thorsten-high")
    monkeypatch.setattr(ha_glue_settings, "satellite_filler_enabled", False)
    filler_mod.warm_in_background(_info(True), "de")
    monkeypatch.setattr(ha_glue_settings, "satellite_filler_enabled", True)
    filler_mod.warm_in_background(_info(False), "de")
    filler_mod.warm_in_background(_info(True), "de")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == ["de"]


# ---------------------------------------------------------------------------
# The real voice turn
# ---------------------------------------------------------------------------

SAT = "sat-filler"
ANSWER = "Das Licht ist an."


class _Socket:
    def __init__(self, frames: list[dict], app):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.client = MagicMock(host="10.0.0.8")
        self.app = app

    async def accept(self):
        return None

    async def receive(self):
        if self._frames:
            return {"type": "websocket.receive", "text": json.dumps(self._frames.pop(0))}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, **_kw):
        return None


class _Ollama:
    load_conversation_context = OllamaService.load_conversation_context
    save_message = OllamaService.save_message

    async def extract_ranked_intents(self, _text, room_context=None, conversation_history=None):
        return [{"intent": "homeassistant.turn_on", "parameters": {"entity_id": "light.x"}, "confidence": 0.9}]

    async def chat_stream(self, _prompt, history=None):
        yield ANSWER


def _executor_taking(seconds: float):
    class _Executor:
        def __init__(self, **_kw):
            pass

        async def execute(self, _intent, **_kw):
            await asyncio.sleep(seconds)
            return {"success": True, "message": "ok"}

    return _Executor


@pytest.fixture
def run_turn(monkeypatch, own_session_to_test_db):
    from ha_glue.services import satellite_manager as manager_mod

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
    monkeypatch.setattr(settings, "satellite_enrollment_enabled", False)
    monkeypatch.setattr(settings, "piper_voices", "de:de_DE-thorsten-high,en:en_US-amy-medium")
    monkeypatch.setattr(ha_glue_settings, "rooms_auto_create_from_satellite", False)
    monkeypatch.setattr(ha_glue_settings, "presence_enabled", False)
    monkeypatch.setattr(ha_glue_settings, "satellite_filler_after_seconds", 0.3)

    limiter = MagicMock()
    limiter.can_connect.return_value = (True, "")
    monkeypatch.setattr(handler, "get_connection_limiter", lambda: limiter)
    rate = MagicMock()
    rate.check.return_value = (True, "")
    monkeypatch.setattr(handler, "get_rate_limiter", lambda: rate)
    monkeypatch.setattr(handler, "authenticate_websocket", AsyncMock(return_value={"sub": "t"}))
    wakeword = MagicMock()
    wakeword.get_config = AsyncMock(return_value=MagicMock(to_satellite_config=lambda: {}))
    monkeypatch.setattr(handler, "get_wakeword_config_manager", lambda: wakeword)
    monkeypatch.setattr(handler, "_spawn_satellite_extraction", lambda **_kw: None)
    builder = MagicMock()
    builder.build = AsyncMock(return_value="")
    monkeypatch.setattr("services.whisper_prompt_builder.get_whisper_prompt_builder", lambda: builder)
    monkeypatch.setattr(
        "services.whisper_prompt_builder.resolve_first_speaker_from_room", AsyncMock(return_value=None),
    )

    async def _synth(text, language=None):
        return b"WAV:" + text.encode()

    piper = MagicMock()
    piper.synthesize_to_bytes = AsyncMock(side_effect=_synth)
    monkeypatch.setattr("services.piper_service.get_piper_service", lambda: piper)

    async def _run(*, action_seconds: float, enabled: bool = True, tts_queue: bool = True,
                   language: str = "de") -> list[tuple[bytes, bool]]:
        mgr = manager_mod.SatelliteManager()
        monkeypatch.setattr(manager_mod, "get_satellite_manager", lambda: mgr)
        monkeypatch.setattr(ha_glue_settings, "satellite_filler_enabled", enabled)
        monkeypatch.setattr("services.action_executor.ActionExecutor", _executor_taking(action_seconds))
        whisper = MagicMock()
        whisper.transcribe_bytes = AsyncMock(return_value="Mach das Licht an")
        monkeypatch.setattr(handler, "get_whisper_service", lambda: whisper)
        app = MagicMock()
        app.state.ollama = _Ollama()
        app.state.mcp_manager = None
        pcm = base64.b64encode(b"\0" * 32000).decode("ascii")
        caps = {"tts_queue": True} if tts_queue else {}
        frames = [
            {"type": "register", "satellite_id": SAT, "room": "Küche", "language": language,
             "capabilities": caps},
            {"type": "wakeword_detected", "keyword": "hey_renfield", "confidence": 0.9, "session_id": "s1"},
            {"type": "audio", "session_id": "s1", "chunk": pcm, "sequence": 1},
            {"type": "audio_end", "session_id": "s1", "reason": "silence"},
        ]
        sock = _Socket(frames, app)
        await handler.satellite_websocket(sock, token=None)
        return [
            (base64.b64decode(f["audio"]), f["is_final"])
            for f in sock.sent if f.get("type") == "tts_audio"
        ]

    return _run


@pytest.mark.database
async def test_a_slow_action_gets_the_filler_before_the_answer(run_turn) -> None:
    clips = await run_turn(action_seconds=0.8)
    assert clips == [
        (b"WAV:" + PHRASES["de"].encode(), False),
        (b"WAV:" + ANSWER.encode(), True),
    ]


@pytest.mark.database
async def test_a_fast_action_gets_only_the_answer(run_turn) -> None:
    assert await run_turn(action_seconds=0.0) == [(b"WAV:" + ANSWER.encode(), True)]


@pytest.mark.database
@pytest.mark.parametrize("kw", [
    {"enabled": False},
    {"tts_queue": False},
    {"language": "it"},
], ids=["switch-off", "older-satellite", "no-own-voice"])
async def test_no_filler_where_it_does_not_belong(run_turn, kw) -> None:
    clips = await run_turn(action_seconds=0.8, **kw)
    assert len(clips) == 1 and clips[0][1] is True
    assert PHRASES["de"].encode() not in clips[0][0]
