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

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_glue.services.voice_empty_turn import (
    EMPTY_TURN_KINDS,
    SATELLITE_VAD_FLOOR_SECONDS,
    classify_empty_turn,
    feedback_cue_for,
    record_empty_turn,
    recorded_seconds,
    vad_floor_seconds,
    vad_speech_seen_from,
)

pytestmark = [pytest.mark.unit, pytest.mark.backend]

FLOOR = SATELLITE_VAD_FLOOR_SECONDS


# ---------------------------------------------------------------------------
# The floor
# ---------------------------------------------------------------------------

def test_the_fleet_floor_is_the_measured_3_12_seconds():
    """39 chunks of 80 ms: the length 133 recordings had in the measurement.

    🛑 The SAME literal is pinned on the satellite side
    (`tests/satellite/test_backend_vad_floor_matches_fleet.py`), measured on
    the real `_on_audio_chunk` with both fleet config sources. Neither side
    recomputes the other's formula; a change on either side breaks one test.
    """
    assert FLOOR == pytest.approx(3.12)
    assert vad_floor_seconds() == FLOOR


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

_CLASSIFICATION_TABLE = [
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
]


# The satellite's own VAD report (v1.4.13+, `vad_speech_seen`) decides a
# silence end, whatever the length says. (reason, seconds, seen, floor, kind)
_REPORTED_TABLE = [
    # A short command spoken entirely inside the grace period ends AT the
    # floor: the length rule calls it a false wake, the report does not.
    ("silence", FLOOR, True, FLOOR, "vad_heard_speech"),
    ("silence", 7.5, True, FLOOR, "vad_heard_speech"),
    ("silence", FLOOR, False, FLOOR, "vad_floor"),
    # Longer than the floor without any speech cannot happen on the device;
    # if it is ever reported, the report still wins over the length.
    ("silence", FLOOR + 0.08, False, FLOOR, "vad_floor"),
    # A source without a known floor (/ws/device) can be split by the report.
    ("silence", 4.0, True, None, "vad_heard_speech"),
    ("silence", 4.0, False, None, "vad_floor"),
    # Only a silence end is split; the other ends keep their kinds.
    ("timeout", 60.08, True, FLOOR, "cap_reached"),
    ("button", 1.0, False, FLOOR, "button"),
    (None, 4.0, True, FLOOR, "unknown_end"),
    ("silence", 0.0, True, FLOOR, "no_audio"),
]


@pytest.mark.parametrize("reason,seconds,seen,floor,kind", _REPORTED_TABLE)
def test_the_reported_vad_verdict_decides_a_silence_end(reason, seconds, seen, floor, kind):
    assert classify_empty_turn(
        turn_end_reason=reason, audio_seconds=seconds,
        floor_seconds=floor, vad_speech_seen=seen,
    ) == kind


def test_a_failed_stt_call_wins_over_the_report():
    assert classify_empty_turn(
        turn_end_reason="silence", audio_seconds=FLOOR, stt_failed=True,
        floor_seconds=FLOOR, vad_speech_seen=True,
    ) == "stt_error"


@pytest.mark.parametrize("value,expected", [
    (True, True),
    (False, False),
    (None, None),
    # Not a JSON boolean: the old satellite's length rule applies, never a
    # truthy reading ("false" is truthy).
    ("false", None),
    ("true", None),
    (0, None),
    (1, None),
    ({}, None),
])
def test_only_a_real_boolean_counts_as_a_report(value, expected):
    assert vad_speech_seen_from(value) is expected


@pytest.mark.parametrize("detail,cue", [
    ({"empty_kind": "vad_heard_speech", "audio_seconds": 5.0}, "not_understood"),
    # A false wake gets NO cue: 92 empty turns a week, most of them false wakes.
    ({"empty_kind": "vad_floor", "audio_seconds": 3.12}, None),
    ({"empty_kind": "no_audio"}, None),
    ({"empty_kind": "stt_error"}, None),
    ({"empty_kind": "cap_reached"}, None),
    ({"empty_kind": "button"}, None),
    ({"empty_kind": "unknown_end"}, None),
    ({}, None),
    (None, None),
])
def test_only_heard_speech_earns_a_cue(detail, cue):
    assert feedback_cue_for(detail) == cue


@pytest.mark.parametrize("reason,seconds,stt_failed,floor,kind", _CLASSIFICATION_TABLE)
def test_classification(reason, seconds, stt_failed, floor, kind):
    assert classify_empty_turn(
        turn_end_reason=reason, audio_seconds=seconds,
        stt_failed=stt_failed, floor_seconds=floor,
    ) == kind


def test_every_kind_is_reachable_and_primed():
    """The table above reaches every primed kind, and nothing else: a kind
    outside the primed vocabulary would be a series born at 1."""
    reached = {
        classify_empty_turn(
            turn_end_reason=r, audio_seconds=s, stt_failed=f, floor_seconds=fl,
        )
        for r, s, f, fl, _kind in _CLASSIFICATION_TABLE
    }
    assert reached == set(EMPTY_TURN_KINDS)


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
    assert "vad_speech=-" in lines[0], "an unreported verdict is shown as unknown"


@pytest.mark.parametrize("seen,text", [(True, "vad_speech=true"), (False, "vad_speech=false")])
def test_record_logs_the_reported_verdict(monkeypatch, seen, text):
    """The log line carries the device's report, so turns that saw speech only
    inside the grace period (report true, length at the floor) can be counted."""
    monkeypatch.setattr("utils.metrics.record_voice_empty_turn", lambda s, k: None)
    lines, done = _capture_warnings()
    try:
        detail = record_empty_turn(
            source="satellite", session_id="sat-1", room="Kinderbad",
            turn_end_reason="silence", audio_seconds=3.12, floor_seconds=FLOOR,
            vad_speech_seen=seen,
        )
    finally:
        done()
    assert detail["empty_kind"] == ("vad_heard_speech" if seen else "vad_floor")
    assert text in lines[0]


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

class _FakeSatelliteSocket:
    """Plays text frames into `satellite_websocket`, then disconnects."""

    def __init__(self, frames: list[dict]):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.client = MagicMock(host="10.0.0.5")
        self.app = MagicMock()

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


class _NoDb:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_a):
        return False


@pytest.fixture
def satellite_env(monkeypatch):
    """The real handler and the real SatelliteManager; only the edges are fakes:
    auth, limits, DB, prompt builder and the STT service."""
    from ha_glue.api.websocket import satellite_handler as handler
    from ha_glue.services import satellite_manager as manager_mod
    from utils.config import settings

    mgr = manager_mod.SatelliteManager()
    monkeypatch.setattr(manager_mod, "get_satellite_manager", lambda: mgr)

    limiter = MagicMock()
    limiter.can_connect.return_value = (True, "")
    monkeypatch.setattr(handler, "get_connection_limiter", lambda: limiter)
    rate = MagicMock()
    rate.check.return_value = (True, "")
    monkeypatch.setattr(handler, "get_rate_limiter", lambda: rate)
    monkeypatch.setattr(handler, "authenticate_websocket", AsyncMock(return_value={"sub": "t"}))
    monkeypatch.setattr(handler, "AsyncSessionLocal", lambda: _NoDb())

    builder = MagicMock()
    builder.build = AsyncMock(return_value="")
    monkeypatch.setattr("services.whisper_prompt_builder.get_whisper_prompt_builder", lambda: builder)
    monkeypatch.setattr(
        "services.whisper_prompt_builder.resolve_first_speaker_from_room", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(settings, "speaker_recognition_enabled", False)

    whisper = MagicMock()
    monkeypatch.setattr(handler, "get_whisper_service", lambda: whisper)

    counted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "utils.metrics.record_voice_empty_turn", lambda s, k: counted.append((s, k)),
    )
    return handler, mgr, whisper, counted


_ABSENT = object()


async def _run_turn(
    handler, mgr, *, audio_seconds: float, reason: str = "silence",
    vad_speech_seen: object = _ABSENT, sent: list | None = None,
):
    """Register a satellite, open session s1, buffer PCM, send audio_end.

    ``vad_speech_seen`` left out = a satellite before v1.4.13 (no field).
    ``sent``, if given, receives every frame sent to the satellite."""
    frame = {"type": "audio_end", "session_id": "s1", "reason": reason}
    if vad_speech_seen is not _ABSENT:
        frame["vad_speech_seen"] = vad_speech_seen
    ws = _FakeSatelliteSocket([frame])
    await mgr.register(satellite_id="sat-bad", room="Kinderbad", websocket=ws, capabilities={})
    await mgr.start_session("sat-bad", "hey_renfield", 0.9, session_id="s1")
    n_bytes = round(audio_seconds * 32000)
    if n_bytes:
        mgr.buffer_audio_bytes("s1", b"\0" * n_bytes, 1)
    await handler.satellite_websocket(ws, token=None)
    ends = [e["details"] for e in mgr._satellite_history["sat-bad"] if e["type"] == "session_end"]
    assert len(ends) == 1
    assert "s1" not in mgr.sessions
    # The satellite returns to idle; a feedback cue, if any, comes after it.
    trailing = [m for m in ws.sent if m.get("type") != "feedback_cue"]
    assert trailing[-1] == {"type": "state", "state": "idle"}, "the satellite must return to idle"
    if sent is not None:
        sent.extend(ws.sent)
    return ends[0]


def _cues(sent: list) -> list:
    return [m for m in sent if m.get("type") == "feedback_cue"]


async def test_satellite_no_audio(satellite_env):
    handler, mgr, _whisper, counted = satellite_env
    end = await _run_turn(handler, mgr, audio_seconds=0)
    assert end["reason"] == "no_audio"
    assert end["empty_kind"] == "no_audio"
    assert end["audio_seconds"] == 0.0
    assert counted == [("satellite", "no_audio")]


async def test_satellite_empty_text_at_the_floor(satellite_env):
    """An old satellite (no field): the length rule. A false wake: no cue."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="")
    sent: list = []
    end = await _run_turn(handler, mgr, audio_seconds=3.12, sent=sent)
    assert end["reason"] == "empty_transcription"
    assert end["turn_end_reason"] == "silence"
    assert end["empty_kind"] == "vad_floor"
    assert end["audio_seconds"] == 3.12
    assert counted == [("satellite", "vad_floor")]
    assert whisper.transcribe_bytes.call_args.kwargs["raise_stt_errors"] is True
    assert _cues(sent) == [], "a false wake must get no cue"


async def test_satellite_old_satellite_after_speech_gets_the_cue(satellite_env):
    """An old satellite (no field) past the floor: the length rule says speech
    was heard, so the cue goes out — after `state: idle`. A satellite before
    v1.4.13 ignores the unknown message type."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="")
    sent: list = []
    end = await _run_turn(handler, mgr, audio_seconds=5.0, sent=sent)
    assert end["empty_kind"] == "vad_heard_speech"
    assert counted == [("satellite", "vad_heard_speech")]
    assert sent[-2:] == [
        {"type": "state", "state": "idle"},
        {"type": "feedback_cue", "cue": "not_understood"},
    ]


async def test_satellite_report_true_at_the_floor_is_heard_speech(satellite_env):
    """v1.4.13+: speech only inside the grace period ends at the floor; the
    length rule would call it a false wake, the report keeps it apart."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="")
    sent: list = []
    end = await _run_turn(handler, mgr, audio_seconds=3.12, vad_speech_seen=True, sent=sent)
    assert end["empty_kind"] == "vad_heard_speech"
    assert counted == [("satellite", "vad_heard_speech")]
    assert _cues(sent) == [{"type": "feedback_cue", "cue": "not_understood"}]
    assert sent[-1]["type"] == "feedback_cue", "the cue comes after `state: idle`"


async def test_satellite_report_false_is_a_false_wake_without_cue(satellite_env):
    """v1.4.13+: the VAD saw no speech. Whatever the length, a false wake:
    no cue."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="")
    sent: list = []
    end = await _run_turn(handler, mgr, audio_seconds=5.0, vad_speech_seen=False, sent=sent)
    assert end["empty_kind"] == "vad_floor"
    assert counted == [("satellite", "vad_floor")]
    assert _cues(sent) == []


async def test_satellite_garbage_report_falls_back_to_the_length_rule(satellite_env):
    """A non-boolean field is no report: the length rule decides (here: the
    floor -> false wake), and the string "true" never means speech."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="")
    sent: list = []
    end = await _run_turn(handler, mgr, audio_seconds=3.12, vad_speech_seen="true", sent=sent)
    assert end["empty_kind"] == "vad_floor"
    assert counted == [("satellite", "vad_floor")]
    assert _cues(sent) == []


@pytest.mark.parametrize("case", ["no_audio", "stt_error", "cap_reached"])
async def test_satellite_no_cue_without_an_empty_turn_of_heard_speech(satellite_env, case):
    """No audio, an STT error and a cut-off turn get no cue, even with a
    report of speech: only `vad_heard_speech` earns one."""
    from services.voice_server_client import VoiceServerError

    handler, mgr, whisper, _counted = satellite_env
    seconds, reason = 5.0, "silence"
    if case == "no_audio":
        seconds = 0
    elif case == "stt_error":
        whisper.transcribe_bytes = AsyncMock(side_effect=VoiceServerError("down", status_code=503))
    else:
        whisper.transcribe_bytes = AsyncMock(return_value="")
        seconds, reason = 60.08, "timeout"
    sent: list = []
    end = await _run_turn(
        handler, mgr, audio_seconds=seconds, reason=reason, vad_speech_seen=True, sent=sent,
    )
    assert end["empty_kind"] == case
    assert _cues(sent) == []


async def test_satellite_a_failing_cue_send_keeps_the_connection(satellite_env):
    """The cue is decoration: a socket fault while sending it is logged, the
    turn stays ended once and the connection is not dropped by the outer
    handler."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="")
    ws = _FakeSatelliteSocket([
        {"type": "audio_end", "session_id": "s1", "reason": "silence", "vad_speech_seen": True},
    ])
    real_send = ws.send_json

    async def send_json(data):
        if data.get("type") == "feedback_cue":
            raise RuntimeError("socket gone")
        await real_send(data)

    ws.send_json = send_json
    await mgr.register(satellite_id="sat-bad", room="Kinderbad", websocket=ws, capabilities={})
    await mgr.start_session("sat-bad", "hey_renfield", 0.9, session_id="s1")
    mgr.buffer_audio_bytes("s1", b"\0" * 99840, 1)
    lines, done = _capture_warnings()
    try:
        await handler.satellite_websocket(ws, token=None)
    finally:
        done()
    assert any("Could not send feedback cue" in m for m in lines)
    assert not any("Satellite WebSocket error" in m for m in lines)
    assert counted == [("satellite", "vad_heard_speech")]
    ends = [e["details"] for e in mgr._satellite_history["sat-bad"] if e["type"] == "session_end"]
    assert [e["reason"] for e in ends] == ["empty_transcription"]


async def test_satellite_empty_text_after_speech_with_speaker_path(satellite_env, monkeypatch):
    from utils.config import settings

    handler, mgr, whisper, counted = satellite_env
    monkeypatch.setattr(settings, "speaker_recognition_enabled", True)
    whisper.transcribe_bytes_with_speaker = AsyncMock(return_value={"text": "  "})
    end = await _run_turn(handler, mgr, audio_seconds=5.0)
    assert end["empty_kind"] == "vad_heard_speech"
    assert counted == [("satellite", "vad_heard_speech")]
    assert whisper.transcribe_bytes_with_speaker.call_args.kwargs["raise_stt_errors"] is True


async def test_satellite_voice_server_error_is_an_stt_error(satellite_env):
    from services.voice_server_client import VoiceServerError

    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(side_effect=VoiceServerError("down", status_code=503))
    end = await _run_turn(handler, mgr, audio_seconds=3.12)
    assert end["reason"] == "transcription_error"
    assert end["empty_kind"] == "stt_error"
    assert counted == [("satellite", "stt_error")]


async def test_satellite_fault_after_the_text_is_no_empty_turn(satellite_env):
    """Negative control (review P3): the text arrived, then something broke.
    That is no turn without text — not counted, and certainly no stt_error."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="Licht an")
    mgr.send_transcription = AsyncMock(side_effect=RuntimeError("socket gone"))
    end = await _run_turn(handler, mgr, audio_seconds=3.12)
    assert end["reason"] == "transcription_error"
    assert "empty_kind" not in end
    assert counted == []


async def test_satellite_fault_while_ending_an_empty_turn_counts_once(satellite_env):
    """The empty turn is counted before the session ends; a fault in ending
    it must not add a second count as an stt_error."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="")
    real_end = mgr.end_session
    calls: list[str] = []

    async def end_once_then_fail(session_id, reason="completed", detail=None):
        calls.append(reason)
        if reason == "empty_transcription":
            raise RuntimeError("history store gone")
        await real_end(session_id, reason, detail)

    mgr.end_session = end_once_then_fail
    ws = _FakeSatelliteSocket([{"type": "audio_end", "session_id": "s1", "reason": "silence"}])
    await mgr.register(satellite_id="sat-bad", room="Kinderbad", websocket=ws, capabilities={})
    await mgr.start_session("sat-bad", "hey_renfield", 0.9, session_id="s1")
    mgr.buffer_audio_bytes("s1", b"\0" * 99840, 1)
    await handler.satellite_websocket(ws, token=None)

    assert counted == [("satellite", "vad_floor")]
    assert calls == ["empty_transcription", "end_failed"]
    ends = [e["details"] for e in mgr._satellite_history["sat-bad"] if e["type"] == "session_end"]
    assert len(ends) == 1, "unregister must not end the turn a second time as completed"
    assert "s1" not in mgr.sessions, "a session left open would later end as `completed`"
    assert ends[0]["reason"] == "end_failed"
    assert ends[0]["success"] is False
    assert ends[0]["empty_kind"] == "vad_floor"
    assert {"type": "state", "state": "idle"} in ws.sent


async def test_satellite_empty_turn_whose_every_end_fails_still_goes_idle(satellite_env):
    """Last resort: both ends fail. The satellite is still told to go idle,
    and nothing is counted twice."""
    handler, mgr, whisper, counted = satellite_env
    whisper.transcribe_bytes = AsyncMock(return_value="")
    ws = _FakeSatelliteSocket([{"type": "audio_end", "session_id": "s1", "reason": "silence"}])
    await mgr.register(satellite_id="sat-bad", room="Kinderbad", websocket=ws, capabilities={})
    await mgr.start_session("sat-bad", "hey_renfield", 0.9, session_id="s1")
    mgr.buffer_audio_bytes("s1", b"\0" * 99840, 1)
    mgr.end_session = AsyncMock(side_effect=RuntimeError("history store gone"))
    lines, done = _capture_warnings()
    try:
        await handler.satellite_websocket(ws, token=None)
    finally:
        done()

    assert not any("Satellite WebSocket error" in m for m in lines), (
        "the fault reached the outer handler, which drops the connection"
    )
    assert counted == [("satellite", "vad_floor")]
    assert mgr.end_session.await_count == 2
    assert ws.sent[-1] == {"type": "state", "state": "idle"}

    # Not deaf: the session is released, and the next wake word starts one.
    assert "s1" not in mgr.sessions
    assert mgr.satellites["sat-bad"].current_session_id is None
    ends = [e["details"] for e in mgr._satellite_history["sat-bad"] if e["type"] == "session_end"]
    assert [(e["reason"], e["success"]) for e in ends] == [("end_failed", False)]
    assert ends[0]["empty_kind"] == "vad_floor"
    assert ends[0]["audio_seconds"] == 3.12
    assert await mgr.start_session("sat-bad", "hey_renfield", 0.9, session_id="s2") == "s2"


def test_force_release_unknown_session_is_a_no_op():
    mgr = _manager_with_session()
    mgr.force_release("unknown", reason="end_failed")  # must not raise
    assert "s1" in mgr.sessions


async def _registered_with_session(monkeypatch, broadcast):
    """A real manager with a registered satellite (room_id 7) in a turn."""
    from ha_glue.services.satellite_manager import SatelliteManager
    from ha_glue.utils.config import ha_glue_settings

    monkeypatch.setattr(ha_glue_settings, "duck_on_listen_enabled", True)
    monkeypatch.setattr("api.websocket.kiosk_handler.broadcast_kiosk_event", broadcast)
    mgr = SatelliteManager()
    ducks: list[tuple] = []
    monkeypatch.setattr(mgr, "_spawn_duck", lambda rid, room, duck: ducks.append((rid, duck)))
    await mgr.register(satellite_id="sat-bad", room="Kinderbad", websocket=_FakeSatelliteSocket([]), capabilities={})
    mgr.set_room_id("sat-bad", 7)
    await mgr.start_session("sat-bad", "hey_renfield", 0.9, session_id="s1")
    ducks.clear()
    return mgr, ducks


async def test_force_release_runs_the_state_funnel(monkeypatch):
    """Kiosk delta and un-ducking happen as on a regular end (review P3)."""
    import asyncio

    deltas: list[dict] = []

    async def broadcast(event):
        deltas.append(event)

    mgr, ducks = await _registered_with_session(monkeypatch, broadcast)
    deltas.clear()
    mgr.force_release("s1", reason="end_failed", detail={"empty_kind": "vad_floor"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ducks == [(7, False)]
    idle = [d for d in deltas if d.get("type") == "satellite_state"]
    assert [d["state"] for d in idle] == ["idle"]
    ends = [e["details"] for e in mgr._satellite_history["sat-bad"] if e["type"] == "session_end"]
    assert len(ends) == 1
    assert ends[0]["empty_kind"] == "vad_floor"
    assert ends[0]["success"] is False


async def test_force_release_survives_a_failing_broadcast(monkeypatch):
    import asyncio

    async def broadcast(_event):
        raise RuntimeError("hub gone")

    mgr, ducks = await _registered_with_session(monkeypatch, broadcast)
    mgr.force_release("s1", reason="end_failed")  # must not raise
    tasks = list(mgr._duck_bg_tasks)
    await asyncio.gather(*tasks)  # the background task must not raise either
    assert "s1" not in mgr.sessions
    assert ducks == [(7, False)]


def test_force_release_survives_a_failing_history(monkeypatch):
    mgr = _manager_with_session()

    def boom(*_a, **_k):
        raise RuntimeError("history store gone")

    monkeypatch.setattr(mgr, "_add_event", boom)
    mgr.force_release("s1", reason="end_failed")  # must not raise
    assert "s1" not in mgr.sessions


async def test_a_retried_end_writes_no_second_session_end(monkeypatch):
    """An end that failed AFTER writing its record, then the fallback end:
    one `session_end`, not two (review side note)."""
    mgr = _manager_with_session()
    real_stats = mgr._update_stats
    failures = iter([RuntimeError("stats store gone")])

    def stats_once_failing(*a, **k):
        err = next(failures, None)
        if err:
            raise err
        return real_stats(*a, **k)

    monkeypatch.setattr(mgr, "_update_stats", stats_once_failing)
    with pytest.raises(RuntimeError):
        await mgr.end_session("s1", reason="empty_transcription", detail={"empty_kind": "vad_floor"})
    assert "s1" in mgr.sessions
    await mgr.end_session("s1", reason="end_failed")

    ends = [e["details"] for e in mgr._satellite_history["sat-a"] if e["type"] == "session_end"]
    assert len(ends) == 1
    assert ends[0]["empty_kind"] == "vad_floor"
    assert "s1" not in mgr.sessions


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
