"""Prometheus metrics of the voice-server (#1642).

Intended behaviour:
- METRICS_ENABLED off: nothing is created, no port is bound, recording is a no-op.
- On: the metrics are served on METRICS_PORT, never on the API app.
  - Every bounded label combination is primed at 0.
  - The STT, TTS and speaker services record their duration and outcome.
  - A cancellation counts as `cancelled`, not as `error`.
  - A failure inside the metric code never reaches the voice path.
- Labels are content-free.

Environment: like test_session_cap.py, importing ws_voice pulls in the
voice-server deps, so this runs in the voice-server image.
"""
from __future__ import annotations

import asyncio
import urllib.request
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from voice_server import metrics
from voice_server.config import settings


@pytest.fixture(autouse=True)
def _fresh_metrics(monkeypatch):
    metrics._reset_for_tests()
    yield
    metrics._reset_for_tests()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    assert metrics.init_metrics() is True
    return metrics


def _value(name: str, labels: dict[str, str] | None = None) -> float | None:
    return metrics._registry.get_sample_value(name, labels or {})


# --- flag off -----------------------------------------------------------------


def test_flag_off_creates_nothing_and_binds_no_port(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", False)
    assert metrics.init_metrics() is False
    assert metrics.start_exporter() is None
    assert metrics.enabled() is False


async def test_flag_off_recording_is_a_no_op(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", False)
    with metrics.observe_stt(partial=False):
        pass
    with metrics.observe_tts_sentence("any"):
        pass
    with metrics.observe_speaker_embed():
        pass
    metrics.set_sessions_active(3)
    metrics.record_session_rejected("capacity")
    metrics.set_component_ready("stt", True)
    assert metrics.enabled() is False


def test_api_app_has_no_metrics_route():
    """/metrics must never be on the API port: the Ingress routes all of it."""
    from voice_server.main import app

    assert "/metrics" not in {getattr(r, "path", None) for r in app.routes}


# --- flag on: priming and content-free labels -----------------------------------


def test_every_bounded_label_combination_is_primed(on):
    for p in metrics.STT_PASSES:
        for o in metrics.OUTCOMES:
            assert _value("voice_server_stt_duration_seconds_count", {"pass": p, "outcome": o}) == 0
    for voice in {settings.piper_default_voice_de, settings.piper_default_voice_en}:
        for o in metrics.OUTCOMES:
            assert _value(
                "voice_server_tts_sentence_duration_seconds_count", {"voice": voice, "outcome": o}
            ) == 0
    for o in metrics.OUTCOMES:
        assert _value("voice_server_speaker_embed_duration_seconds_count", {"outcome": o}) == 0
    assert _value("voice_server_sessions_rejected_total", {"reason": "capacity"}) == 0
    assert _value("voice_server_sessions_active") == 0
    for c in ("stt", "speaker"):
        assert _value("voice_server_component_ready", {"component": c}) == 0


def test_meeting_ready_is_exported_only_when_meetings_are_on(monkeypatch):
    """Off, meeting never warms up; a permanent 0 would look broken."""
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "meeting_enabled", False)
    metrics.init_metrics()
    metrics.set_component_ready("meeting", True)
    assert _value("voice_server_component_ready", {"component": "meeting"}) is None

    metrics._reset_for_tests()
    monkeypatch.setattr(settings, "meeting_enabled", True)
    metrics.init_metrics()
    assert _value("voice_server_component_ready", {"component": "meeting"}) == 0
    metrics.set_component_ready("meeting", True)
    assert _value("voice_server_component_ready", {"component": "meeting"}) == 1


def test_labels_are_content_free(on):
    allowed = {"pass", "outcome", "voice", "reason", "component", "le"}
    for family in metrics._registry.collect():
        for sample in family.samples:
            assert set(sample.labels) <= allowed, (family.name, sample.labels)


# --- flag on: recording -------------------------------------------------------------


async def test_outcomes_ok_error_cancelled(on):
    with metrics.observe_stt(partial=True):
        pass
    with pytest.raises(ValueError):
        with metrics.observe_stt(partial=False):
            raise ValueError("boom")
    with pytest.raises(asyncio.CancelledError):
        with metrics.observe_stt(partial=False):
            raise asyncio.CancelledError()

    c = "voice_server_stt_duration_seconds_count"
    assert _value(c, {"pass": "partial", "outcome": "ok"}) == 1
    assert _value(c, {"pass": "final", "outcome": "error"}) == 1
    assert _value(c, {"pass": "final", "outcome": "cancelled"}) == 1
    assert _value(c, {"pass": "final", "outcome": "ok"}) == 0


def test_a_broken_metric_never_reaches_the_voice_path(on, monkeypatch):
    broken = MagicMock()
    broken.labels.side_effect = RuntimeError("registry exploded")
    monkeypatch.setattr(metrics, "_stt_duration", broken)
    monkeypatch.setattr(metrics, "_sessions_active", broken)
    broken.set.side_effect = RuntimeError("registry exploded")

    with metrics.observe_stt(partial=False):
        result = 42
    assert result == 42
    metrics.set_sessions_active(1)  # must not raise


def test_sessions_gauge_and_reject_counter(on):
    metrics.set_sessions_active(2)
    metrics.record_session_rejected("capacity")
    metrics.set_component_ready("speaker", True)
    assert _value("voice_server_sessions_active") == 2
    assert _value("voice_server_sessions_rejected_total", {"reason": "capacity"}) == 1
    assert _value("voice_server_component_ready", {"component": "speaker"}) == 1


# --- the services record --------------------------------------------------------------


async def test_stt_service_records_each_pass(on):
    from voice_server.services.stt_service import STTService

    stt = STTService()
    stt.ready = True
    stt._model = MagicMock()
    stt._model.transcribe.return_value = ([], SimpleNamespace(language="de"))

    audio = np.zeros(1600, dtype=np.float32)
    async for _ in stt.transcribe_stream(audio, partial=True):
        pass
    async for _ in stt.transcribe_stream(audio):
        pass

    c = "voice_server_stt_duration_seconds_count"
    assert _value(c, {"pass": "partial", "outcome": "ok"}) == 1
    assert _value(c, {"pass": "final", "outcome": "ok"}) == 1


async def test_stt_failure_is_an_error_and_still_raises(on):
    from voice_server.services.stt_service import STTService

    stt = STTService()
    stt.ready = True
    stt._model = MagicMock()
    stt._model.transcribe.side_effect = RuntimeError("cuda gone")

    with pytest.raises(RuntimeError, match="cuda gone"):
        async for _ in stt.transcribe_stream(np.zeros(1600, dtype=np.float32)):
            pass
    assert _value(
        "voice_server_stt_duration_seconds_count", {"pass": "final", "outcome": "error"}
    ) == 1


async def test_tts_records_one_observation_per_sentence(on, monkeypatch):
    from voice_server.services.tts_service import TTSService

    tts = TTSService()
    monkeypatch.setattr(tts, "_load_voice", lambda name: object())
    monkeypatch.setattr(tts, "_synth_one_sentence", lambda voice, s: b"RIFF")

    frames = [f async for f in tts.stream_sentences("Eins. Zwei. Drei.", uuid.uuid4(), language="de")]
    assert len(frames) == 3
    assert _value(
        "voice_server_tts_sentence_duration_seconds_count",
        {"voice": settings.piper_default_voice_de, "outcome": "ok"},
    ) == 3


async def test_a_barge_in_mid_synthesis_is_cancelled_not_error(on, monkeypatch):
    """A real cancellation while Piper runs in its thread (the TTS barge-in)."""
    import threading

    from voice_server.services.tts_service import TTSService

    started, release = threading.Event(), threading.Event()

    def slow_synth(voice, sentence):
        started.set()
        release.wait(5)
        return b"RIFF"

    tts = TTSService()
    monkeypatch.setattr(tts, "_load_voice", lambda name: object())
    monkeypatch.setattr(tts, "_synth_one_sentence", slow_synth)

    async def consume():
        async for _ in tts.stream_sentences("Ein langer Satz.", uuid.uuid4(), language="de"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()

    labels = {"voice": settings.piper_default_voice_de}
    c = "voice_server_tts_sentence_duration_seconds_count"
    assert _value(c, {**labels, "outcome": "cancelled"}) == 1
    assert _value(c, {**labels, "outcome": "error"}) == 0


async def test_speaker_embed_records(on, monkeypatch):
    from voice_server.services.speaker_service import SpeakerService

    spk = SpeakerService()
    spk.ready = True
    monkeypatch.setattr(spk, "_embed_sync", lambda audio: np.zeros(192, dtype=np.float32))

    await spk.embed(np.zeros(16000, dtype=np.float32))
    assert _value("voice_server_speaker_embed_duration_seconds_count", {"outcome": "ok"}) == 1


# --- the WS handler counts sessions -------------------------------------------------------


async def test_ws_handler_tracks_sessions_and_capacity_rejects(on, monkeypatch):
    import voice_server.api.ws_voice as wsv

    monkeypatch.setattr(settings, "auth_required", False)

    class _WS:
        def __init__(self):
            self.app = SimpleNamespace(state=SimpleNamespace(stt=MagicMock(), tts=MagicMock(), speaker=MagicMock()))
            self.scope: dict = {}
            self.closed = None
            self.gauge_while_open = None

        async def accept(self):
            pass

        async def close(self, code=None, reason=None):
            self.closed = (code, reason)

        async def receive(self):
            # Captured, not asserted: the handler's `except Exception` would
            # swallow an AssertionError raised in here.
            self.gauge_while_open = _value("voice_server_sessions_active")
            return {"type": "websocket.disconnect"}

        async def send_text(self, _t):
            pass

        async def send_bytes(self, _b):
            pass

    wsv._active_sessions = 0
    try:
        session = _WS()
        await wsv.ws_voice(session, token=None)
        assert session.gauge_while_open == 1
        assert _value("voice_server_sessions_active") == 0

        monkeypatch.setattr(settings, "max_concurrent_sessions", 1)
        wsv._active_sessions = 1
        rejected = _WS()
        await wsv.ws_voice(rejected, token=None)
        assert rejected.closed == (1013, "server at capacity")
        assert _value("voice_server_sessions_rejected_total", {"reason": "capacity"}) == 1
    finally:
        wsv._active_sessions = 0


# --- the exporter ------------------------------------------------------------------------


def test_exporter_serves_on_its_own_port(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_port", 0)  # any free port
    monkeypatch.setattr(settings, "host", "127.0.0.1")
    server = metrics.start_exporter()
    try:
        port = server.server_port
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
        assert "voice_server_stt_duration_seconds_bucket" in body
        assert "voice_server_component_ready" in body
    finally:
        server.shutdown()
