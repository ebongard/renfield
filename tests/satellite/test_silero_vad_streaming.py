"""
Silero VAD: the v5 model's streaming calling convention.

Finding of 2026-09-19: EVERY voice turn in the fleet ended after ~3.3 s (grace
period 2.0 s + 1.2 s silence) -- 53 of 53 recordings on one day, a long sentence
was cut off. Cause: ``SileroVADLite`` handed the v5 model (combined ``state``
input) bare 512-sample frames. Before each frame the model requires the previous
frame's last 64 samples (576 per call); without them it answers ~0.00 for clear
speech -- measured 0 of 118 chunks instead of 114 of 118. So the VAD NEVER
reported speech.

The path had not a single test, and the model is loaded unversioned from
``master`` -- that is how a model change became a silent total failure.
"""

import os

import numpy as np
import pytest
from renfield_satellite.audio.vad import SileroVADLite

CHUNK = 1280  # the satellite's recording chunk (80 ms @ 16 kHz)


class FakeSession:
    """Records what the model actually gets to see."""

    def __init__(self, prob=0.9):
        self.calls = []
        self.prob = prob

    def run(self, _outputs, inputs):
        self.calls.append({k: np.array(v, copy=True) for k, v in inputs.items()})
        return np.array([[self.prob]], dtype=np.float32), inputs["state"] + 1.0


def _vad(prob=0.9) -> tuple:
    vad = SileroVADLite(model_path="/nonexistent/silero_vad.onnx")
    session = FakeSession(prob)
    vad._session = session
    vad._use_new_format = True
    vad._reset_states()
    return vad, session


def _ramp(n: int, start: int = 0) -> bytes:
    """Distinct sample values, so order and gaps can be checked."""
    return ((np.arange(start, start + n) % 30000) + 1).astype(np.int16).tobytes()


@pytest.mark.satellite
def test_modell_bekommt_kontext_plus_frame():
    """The core regression: 64 + 512 = 576 samples, never a bare 512."""
    vad, session = _vad()
    vad.get_speech_probability(_ramp(CHUNK))

    assert [c["input"].shape for c in session.calls] == [(1, 576), (1, 576)]


@pytest.mark.satellite
def test_kontext_ist_das_ende_des_vorherigen_frames():
    vad, session = _vad()
    vad.get_speech_probability(_ramp(CHUNK))
    first, second = session.calls[0]["input"], session.calls[1]["input"]

    assert np.all(first[0, :64] == 0.0), "first frame: context is silence"
    assert np.array_equal(second[0, :64], first[0, -64:])


@pytest.mark.satellite
def test_strom_ist_lueckenlos_ueber_aufrufgrenzen():
    """1280 is not a multiple of 512 -- the remainder belongs in the next call,
    not zero-padded into this one."""
    vad, session = _vad()
    vad.get_speech_probability(_ramp(CHUNK, 0))
    vad.get_speech_probability(_ramp(CHUNK, CHUNK))

    fed = np.concatenate([c["input"][0, 64:] for c in session.calls])
    expected = (np.frombuffer(_ramp(2 * CHUNK), dtype=np.int16).astype(np.float32) / 32768.0)[: len(fed)]

    assert len(session.calls) == 5            # 2560 samples = 5 full frames
    assert np.array_equal(fed, expected), "no sample lost, none invented"
    assert not np.any(fed == 0.0), "no inserted zero samples"


@pytest.mark.satellite
def test_zustand_wird_von_frame_zu_frame_weitergereicht():
    vad, session = _vad()
    vad.get_speech_probability(_ramp(CHUNK))

    assert np.all(session.calls[0]["state"] == 0.0)
    assert np.all(session.calls[1]["state"] == 1.0)


@pytest.mark.satellite
def test_reset_loescht_zustand_kontext_und_rest():
    vad, session = _vad()
    vad.get_speech_probability(_ramp(CHUNK))
    vad.reset()
    session.calls.clear()
    vad.get_speech_probability(_ramp(CHUNK, 5000))

    assert np.all(session.calls[0]["state"] == 0.0)
    assert np.all(session.calls[0]["input"][0, :64] == 0.0)
    assert len(session.calls) == 2, "the remainder of the previous turn must not run along"


@pytest.mark.satellite
def test_zu_kurzer_aufruf_wiederholt_das_letzte_urteil():
    vad, _ = _vad(prob=0.9)
    assert vad.get_speech_probability(_ramp(CHUNK)) == pytest.approx(0.9)
    assert vad.get_speech_probability(_ramp(100)) == pytest.approx(0.9)


@pytest.mark.satellite
def test_inferenzfehler_wird_einmal_gemeldet(capsys):
    """Neutral 0.5 reads as SPEECH at threshold 0.5 -- that must not be silent."""
    vad, session = _vad()
    session.run = lambda *_: (_ for _ in ()).throw(RuntimeError("kaputt"))

    assert vad.get_speech_probability(_ramp(CHUNK)) == 0.5
    assert vad.get_speech_probability(_ramp(CHUNK)) == 0.5
    assert vad.last_call_failed is True
    assert capsys.readouterr().out.count("Silero VAD inference failed") == 1


@pytest.mark.satellite
def test_inferenzfehler_faellt_auf_rms_zurueck_statt_sprache_zu_melden():
    """The neutral 0.5 must never be judged: at threshold 0.5 it would hold
    every turn open until the recording limit."""
    from renfield_satellite.audio.vad import VADBackend, VoiceActivityDetector

    detector = VoiceActivityDetector(backend=VADBackend.RMS, rms_threshold=350.0)
    lite, session = _vad()
    session.run = lambda *_: (_ for _ in ()).throw(RuntimeError("kaputt"))
    detector.backend = VADBackend.SILERO
    detector._use_onnx = True
    detector._silero_onnx = lite

    silence = np.zeros(CHUNK, dtype=np.int16).tobytes()
    loud = (np.ones(CHUNK) * 8000).astype(np.int16).tobytes()

    assert not detector.is_speech(silence), "silence stays silence, despite a broken model"
    assert detector.is_speech(loud)


@pytest.mark.satellite
def test_gesunder_aufruf_loescht_das_fehlerkennzeichen():
    vad, session = _vad()
    good_run = session.run
    session.run = lambda *_: (_ for _ in ()).throw(RuntimeError("kaputt"))
    vad.get_speech_probability(_ramp(CHUNK))
    session.run = good_run

    vad.get_speech_probability(_ramp(CHUNK))
    assert vad.last_call_failed is False


@pytest.mark.satellite
def test_ungerade_bytezahl_ist_kein_fehler():
    vad, session = _vad()
    vad.get_speech_probability(_ramp(CHUNK) + b"\x01")

    assert vad.last_call_failed is False
    assert len(session.calls) == 2


# ---------------------------------------------------------------------------
# Against the real model (skipped when it is not present)
# ---------------------------------------------------------------------------

MODEL = os.environ.get("SILERO_VAD_MODEL", "/opt/renfield-satellite/models/silero_vad.onnx")
SPEECH = os.environ.get("SILERO_VAD_SPEECH_WAV", "")
needs_model = pytest.mark.skipif(
    not (os.path.exists(MODEL) and os.path.exists(SPEECH)),
    reason="SILERO_VAD_MODEL / SILERO_VAD_SPEECH_WAV nicht gesetzt",
)


def _chunks(pcm: np.ndarray):
    for i in range(0, len(pcm) - CHUNK + 1, CHUNK):
        yield pcm[i:i + CHUNK].tobytes()


@needs_model
@pytest.mark.satellite
def test_echtes_modell_erkennt_sprache_und_verwirft_stille():
    import wave

    with wave.open(SPEECH) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    vad = SileroVADLite(model_path=MODEL)
    speech = [vad.is_speech(c) for c in _chunks(pcm)]
    vad.reset()
    silence = [vad.is_speech(c) for c in _chunks(np.zeros(16000 * 3, dtype=np.int16))]

    assert sum(speech) / len(speech) > 0.8, "before the fix: 0 of 118"
    assert sum(silence) == 0
