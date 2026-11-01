"""Why a voice turn produced no text is a SIGNAL, not a log line (#1386).

Measured in the household (7 days): 92 of 232 wake events ended without text,
133 recordings ended at exactly 3.120 s — the earliest end the satellite's VAD
allows. Every one ended with "Empty transcription" and a silent idle, so a
false wake (repair: room-specific wake-word negatives) could not be told apart
from speech that was heard but not understood (repair: STT / feedback). And a
voice-server failure came back as "" and was counted among them.

These tests check the classifier, the content-free record, and that both
handlers (satellite, /ws/device) route every no-text exit through it.
"""

from __future__ import annotations

import ast
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_glue.services.voice_empty_turn import (
    EMPTY_TURN_KINDS,
    SATELLITE_VAD_FLOOR_SECONDS,
    classify_empty_turn,
    record_empty_turn,
    recorded_seconds,
    vad_floor_seconds,
)

pytestmark = [pytest.mark.unit, pytest.mark.backend]

FLOOR = SATELLITE_VAD_FLOOR_SECONDS


# ---------------------------------------------------------------------------
# The floor
# ---------------------------------------------------------------------------

def test_the_fleet_floor_is_the_measured_3_12_seconds():
    """39 chunks of 80 ms: the length 133 recordings had in the measurement."""
    assert FLOOR == pytest.approx(3.12)


def test_the_floor_follows_the_settings():
    assert vad_floor_seconds(1.0, 1200) == pytest.approx((12 + 15 - 1) * 0.08)
    assert vad_floor_seconds(2.0, 1500) == pytest.approx((25 + 18 - 1) * 0.08)


def test_recorded_seconds():
    assert recorded_seconds(opus_packets=78) == pytest.approx(3.12)
    assert recorded_seconds(pcm_bytes=32000) == pytest.approx(1.0)
    assert recorded_seconds() == 0.0


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason,seconds,stt_failed,floor,kind", [
    ("silence", FLOOR, False, FLOOR, "vad_floor"),
    ("silence", FLOOR - 0.08, False, FLOOR, "vad_floor"),
    # One chunk more than the floor: the VAD saw speech at least once.
    ("silence", FLOOR + 0.08, False, FLOOR, "vad_heard_speech"),
    ("silence", 7.5, False, FLOOR, "vad_heard_speech"),
    ("silence", FLOOR, False, None, "unknown_end"),
    ("timeout", 60.08, False, FLOOR, "cap_reached"),
    ("button", 1.0, False, FLOOR, "button"),
    ("unknown", 4.0, False, FLOOR, "unknown_end"),
    (None, 4.0, False, FLOOR, "unknown_end"),
    ("silence", 0.0, False, FLOOR, "no_audio"),
    # A failed STT call wins over every length: it is not silence.
    ("silence", FLOOR, True, FLOOR, "stt_error"),
    ("silence", 0.0, True, FLOOR, "stt_error"),
])
def test_classification(reason, seconds, stt_failed, floor, kind):
    assert classify_empty_turn(
        turn_end_reason=reason, audio_seconds=seconds,
        stt_failed=stt_failed, floor_seconds=floor,
    ) == kind


def test_every_kind_the_classifier_returns_is_in_the_vocabulary():
    """A kind outside the primed vocabulary would be a series born at 1."""
    src = inspect.getsource(classify_empty_turn)
    body = ast.parse(src).body[0]
    returned = {
        n.value.value for n in ast.walk(body)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
    }
    assert returned == set(EMPTY_TURN_KINDS)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def _capture_warnings():
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    return lines, lambda: logger.remove(sink)


def test_record_logs_counts_and_returns_the_detail(monkeypatch):
    counted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "utils.metrics.record_voice_empty_turn", lambda s, k: counted.append((s, k)),
    )
    lines, done = _capture_warnings()
    try:
        detail = record_empty_turn(
            source="satellite", session_id="sat-1", room="Kinderbad",
            turn_end_reason="silence", audio_seconds=3.12, floor_seconds=FLOOR,
        )
    finally:
        done()

    assert detail == {"empty_kind": "vad_floor", "audio_seconds": 3.12}
    assert counted == [("satellite", "vad_floor")]
    assert len(lines) == 1
    assert "kind=vad_floor" in lines[0]
    assert "room=Kinderbad" in lines[0]
    assert "turn_end=silence" in lines[0]
    assert "audio=3.12s" in lines[0]


def test_a_metrics_fault_never_raises(monkeypatch):
    def boom(*_):
        raise RuntimeError("registry gone")

    monkeypatch.setattr("utils.metrics.record_voice_empty_turn", boom)
    detail = record_empty_turn(
        source="device", session_id="d", room=None,
        turn_end_reason=None, audio_seconds=0.0,
    )
    assert detail["empty_kind"] == "no_audio"


# ---------------------------------------------------------------------------
# SatelliteManager: the buffer length and the event detail
# ---------------------------------------------------------------------------

def _manager_with_session(**session_fields):
    from ha_glue.services.satellite_manager import (
        SatelliteManager,
        SatelliteSession,
        SatelliteState,
    )

    mgr = SatelliteManager()
    mgr.sessions["s1"] = SatelliteSession(
        session_id="s1", satellite_id="sat-a", room="Bad",
        state=SatelliteState.PROCESSING, **session_fields,
    )
    return mgr


def test_recorded_audio_seconds_counts_opus_packets_and_pcm():
    assert _manager_with_session(opus_packets=[b"x"] * 78).recorded_audio_seconds("s1") == pytest.approx(3.12)
    assert _manager_with_session(audio_chunks=[b"\0" * 16000] * 2).recorded_audio_seconds("s1") == pytest.approx(1.0)
    assert _manager_with_session().recorded_audio_seconds("unknown") == 0.0


async def test_the_session_end_event_carries_the_detail():
    mgr = _manager_with_session()
    mgr.set_turn_end_reason("s1", "silence")
    await mgr.end_session(
        "s1", reason="empty_transcription",
        detail={"empty_kind": "vad_floor", "audio_seconds": 3.12},
    )
    events = [e for e in mgr._satellite_history["sat-a"] if e["type"] == "session_end"]
    assert len(events) == 1
    data = events[0]["details"]
    assert data["reason"] == "empty_transcription"
    assert data["turn_end_reason"] == "silence"
    assert data["empty_kind"] == "vad_floor"
    assert data["audio_seconds"] == 3.12
    assert "s1" not in mgr.sessions


# ---------------------------------------------------------------------------
# WhisperService: a failed STT call is no longer an empty transcription
# ---------------------------------------------------------------------------

@pytest.fixture
def _failing_voice_server(monkeypatch):
    from services.voice_server_client import VoiceServerError
    from utils.config import settings

    monkeypatch.setattr(settings, "voice_server_url", "http://voice-server.test:8080")
    monkeypatch.setattr("services.whisper_service._get_service_token", lambda: "t")

    async def fail(*_a, **_k):
        raise VoiceServerError("down", status_code=None)

    monkeypatch.setattr("services.voice_server_client.stt", fail)
    monkeypatch.setattr("services.voice_server_client.stt_opus", fail)
    return VoiceServerError


async def test_transcribe_bytes_keeps_its_old_contract(_failing_voice_server):
    from services.whisper_service import WhisperService

    assert await WhisperService().transcribe_bytes(b"\0" * 10, "x.wav") == ""


async def test_transcribe_bytes_raises_when_asked(_failing_voice_server):
    from services.whisper_service import WhisperService

    with pytest.raises(_failing_voice_server):
        await WhisperService().transcribe_bytes(b"\0" * 10, "x.opus", is_opus=True, raise_stt_errors=True)


async def test_with_speaker_keeps_its_old_contract(_failing_voice_server):
    from services.whisper_service import WhisperService

    result = await WhisperService().transcribe_bytes_with_speaker(b"\0" * 10, db_session=object())
    assert result["text"] == ""


async def test_with_speaker_raises_when_asked(_failing_voice_server):
    from services.whisper_service import WhisperService

    with pytest.raises(_failing_voice_server):
        await WhisperService().transcribe_bytes_with_speaker(
            b"\0" * 10, db_session=object(), raise_stt_errors=True,
        )


# ---------------------------------------------------------------------------
# The satellite handler: every no-text exit is classified
# ---------------------------------------------------------------------------

def _handler_tree():
    from ha_glue.api.websocket import satellite_handler

    return ast.parse(inspect.getsource(satellite_handler))


def _end_session_calls(tree) -> dict[str, ast.Call]:
    calls = {}
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "end_session"
        ):
            reason = next((k.value.value for k in n.keywords if k.arg == "reason"
                           and isinstance(k.value, ast.Constant)), None)
            calls[reason] = n
    return calls


@pytest.mark.parametrize("reason", ["no_audio", "empty_transcription", "transcription_error"])
def test_satellite_no_text_exits_carry_the_classified_detail(reason):
    call = _end_session_calls(_handler_tree())[reason]
    detail = next((k.value for k in call.keywords if k.arg == "detail"), None)
    assert detail is not None, f"end_session(reason={reason!r}) passes no detail"
    assert isinstance(detail, ast.Call) and getattr(detail.func, "id", None) == "empty_turn"
    stt_failed = any(k.arg == "stt_failed" for k in detail.keywords)
    assert stt_failed == (reason == "transcription_error")


def test_satellite_stt_calls_raise_instead_of_returning_empty():
    calls = [
        n for n in ast.walk(_handler_tree())
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("transcribe_bytes", "transcribe_bytes_with_speaker")
    ]
    assert len(calls) == 2
    for c in calls:
        assert any(
            k.arg == "raise_stt_errors" and getattr(k.value, "value", None) is True
            for k in c.keywords
        ), f"{c.func.attr} would swallow a voice-server failure into ''"


def test_the_empty_turn_partial_carries_the_satellite_floor():
    tree = _handler_tree()
    partials = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "partial" and n.args
        and getattr(n.args[0], "id", None) == "record_empty_turn"
    ]
    assert len(partials) == 1
    kw = {k.arg: k.value for k in partials[0].keywords}
    assert getattr(kw["floor_seconds"], "id", None) == "SATELLITE_VAD_FLOOR_SECONDS"
    assert getattr(kw["turn_end_reason"], "id", None) == "reason"


# ---------------------------------------------------------------------------
# The /ws/device handler, driven for real
# ---------------------------------------------------------------------------

class _FakeDeviceManager:
    def __init__(self, audio: bytes | None):
        self._audio = audio
        self.ended: list[str] = []
        self.set_session_state = AsyncMock()
        self.send_transcription = AsyncMock()

    def get_audio_buffer(self, _sid):
        return self._audio

    def get_session(self, _sid):
        return MagicMock(room="Flur")

    async def end_session(self, _sid, reason="completed"):
        self.ended.append(reason)


@pytest.fixture
def device_env(monkeypatch):
    from ha_glue.api.websocket import device_handler
    from utils.config import settings

    counted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "utils.metrics.record_voice_empty_turn", lambda s, k: counted.append((s, k)),
    )
    monkeypatch.setattr(settings, "speaker_recognition_enabled", False)

    class _DB:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(device_handler, "AsyncSessionLocal", lambda: _DB())
    builder = MagicMock()
    builder.build = AsyncMock(return_value="")
    monkeypatch.setattr(
        "services.whisper_prompt_builder.get_whisper_prompt_builder", lambda: builder,
    )
    whisper = MagicMock()
    monkeypatch.setattr(device_handler, "get_whisper_service", lambda: whisper)
    return device_handler, whisper, counted


async def test_device_no_audio_is_classified(device_env):
    handler, _whisper, counted = device_env
    mgr = _FakeDeviceManager(None)
    await handler._process_device_session(MagicMock(), mgr, "d1", turn_end_reason="silence")
    assert mgr.ended == ["no_audio"]
    assert counted == [("device", "no_audio")]


async def test_device_empty_transcription_is_classified(device_env):
    handler, whisper, counted = device_env
    whisper.transcribe_bytes = AsyncMock(return_value="  ")
    mgr = _FakeDeviceManager(b"\0" * 32000)
    await handler._process_device_session(MagicMock(), mgr, "d1", turn_end_reason="button")
    assert mgr.ended == ["empty_transcription"]
    assert counted == [("device", "button")]
    assert whisper.transcribe_bytes.call_args.kwargs["raise_stt_errors"] is True


async def test_device_stt_failure_is_an_stt_error(device_env):
    handler, whisper, counted = device_env
    whisper.transcribe_bytes = AsyncMock(side_effect=RuntimeError("voice-server down"))
    mgr = _FakeDeviceManager(b"\0" * 32000)
    await handler._process_device_session(MagicMock(), mgr, "d1", turn_end_reason="silence")
    assert mgr.ended == ["processing_error"]
    assert counted == [("device", "stt_error")]


async def test_device_failure_after_text_is_not_an_empty_turn(device_env, monkeypatch):
    """Negative control: a fault in the answer stage must not be counted as
    a turn without text — the text was there."""
    handler, whisper, counted = device_env
    whisper.transcribe_bytes = AsyncMock(return_value="Licht an")
    monkeypatch.setattr(handler, "_process_text_input", AsyncMock(side_effect=RuntimeError("llm")))
    mgr = _FakeDeviceManager(b"\0" * 32000)
    await handler._process_device_session(MagicMock(), mgr, "d1", turn_end_reason="silence")
    assert mgr.ended == ["processing_error"]
    assert counted == []
