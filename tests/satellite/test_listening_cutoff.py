"""
Recording cutoff in the LISTENING state.

Covers the two reasons that end a voice turn:

* ``silence``  -- after the grace period the VAD reports silence throughout
* ``timeout``  -- the recording exceeds ``vad.max_recording_seconds``

The core regression (2026-09-19): both checks read the length of a buffer that
was capped at 500 chunks (= 40.0 s). As soon as ``max_recording_seconds`` was
raised above 40 s, the length check could NEVER fire again -- the satellite no
longer ended the turn itself, and the audio expired silently in the backend's
120 s cleanup pass, with no answer to the speaker. The turn is therefore
measured via ``_recorded_chunks``. The buffer itself was never read and was
removed in v1.4.9 -- ``OLD_BUFFER_CAP_CHUNKS`` keeps the old limit only as a
marker for this test.
"""

import ast
import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from renfield_satellite.satellite import Satellite, SatelliteState

CHUNK_SIZE = 1280
SAMPLE_RATE = 16000
CHUNK_SECONDS = CHUNK_SIZE / SAMPLE_RATE  # 0.08 s
SILENCE_MS = 1200
MIN_LISTENING_S = 1.0
# The removed buffer's limit (40.0 s). Beyond it the old measure froze.
OLD_BUFFER_CAP_CHUNKS = 500


def _make_satellite(max_recording_seconds: float) -> Satellite:
    """A satellite without hardware: only what ``_on_audio_chunk`` really touches."""
    sat = object.__new__(Satellite)

    sat.config = MagicMock()
    sat.config.audio.chunk_size = CHUNK_SIZE
    sat.config.audio.sample_rate = SAMPLE_RATE
    sat.config.vad.min_listening_seconds = MIN_LISTENING_S
    sat.config.vad.silence_duration_ms = SILENCE_MS
    sat.config.vad.max_recording_seconds = max_recording_seconds

    sat._state = SatelliteState.LISTENING
    sat._silence_chunks = 0
    sat._recorded_chunks = 0
    sat._session_id = None          # no streaming -> no ws_client needed
    sat._current_audio_rms = 0.0
    sat._current_audio_db = 0.0
    sat._current_is_speech = False

    sat.wakeword = MagicMock()
    sat.wakeword.active_stop_words = []   # stop-word branch off
    sat.preprocessor = MagicMock()
    sat.preprocessor.normalize = lambda b: b
    sat.vad = MagicMock()
    sat.ws_client = MagicMock()

    # _end_listening is async; as a MagicMock no never-awaited coroutine arises.
    sat._end_listening = MagicMock(return_value=None)
    sat._schedule_async = MagicMock()
    return sat


def _feed(sat: Satellite, chunks: int, *, speech: bool) -> None:
    sat.vad.is_speech = MagicMock(return_value=speech)
    audio = b"\x01\x00" * (CHUNK_SIZE // 2)
    for _ in range(chunks):
        sat._on_audio_chunk(audio)


def _reasons(sat: Satellite) -> list:
    return [c.args[0] for c in sat._end_listening.call_args_list]


# ---------------------------------------------------------------------------
# Length cutoff -- the regression
# ---------------------------------------------------------------------------

@pytest.mark.satellite
def test_laengenabbruch_feuert_jenseits_der_40s_grenze():
    """A 60 s limit > 40 s (the old buffer cap): the cutoff MUST still come."""
    sat = _make_satellite(max_recording_seconds=60.0)
    # 751 chunks = 60.08 s -- the first position genuinely above 60 s.
    _feed(sat, 751, speech=True)

    assert 751 > OLD_BUFFER_CAP_CHUNKS, "der Test MUSS jenseits der alten Grenze liegen"
    assert sat._recorded_chunks == 751
    assert "timeout" in _reasons(sat)


@pytest.mark.satellite
def test_laengenabbruch_nicht_vor_dem_limit():
    """Exactly 60.0 s is not above 60 s -- no cutoff yet."""
    sat = _make_satellite(max_recording_seconds=60.0)
    _feed(sat, 750, speech=True)   # 750 * 0.08 = 60.0 s

    assert sat._recorded_chunks == 750
    assert "timeout" not in _reasons(sat)


@pytest.mark.satellite
def test_zaehler_laeuft_jenseits_der_alten_grenze_weiter():
    """The turn counter has no upper bound -- it freezes nowhere."""
    sat = _make_satellite(max_recording_seconds=3600.0)
    _feed(sat, OLD_BUFFER_CAP_CHUNKS + 120, speech=True)

    assert sat._recorded_chunks == OLD_BUFFER_CAP_CHUNKS + 120
    assert _reasons(sat) == []


@pytest.mark.satellite
def test_kein_gedeckelter_audiopuffer_mehr():
    """The satellite holds no audio back -- it is streamed chunk by chunk.

    The removed buffer was the cause of the regression: nobody read it, only
    its length served as a clock. Whoever reintroduces it should trip here.
    """
    src = pathlib.Path(inspect.getsourcefile(Satellite)).read_text()
    assert "self._audio_buffer" not in src
    assert "MAX_AUDIO_BUFFER_CHUNKS" not in src


@pytest.mark.satellite
def test_kurzes_limit_unterhalb_der_40s_grenze_feuert_weiterhin():
    """The old behaviour (20 s < 40 s) stays unchanged."""
    sat = _make_satellite(max_recording_seconds=20.0)
    _feed(sat, 251, speech=True)   # 251 * 0.08 = 20.08 s

    assert "timeout" in _reasons(sat)


# ---------------------------------------------------------------------------
# Silence cutoff + grace period
# ---------------------------------------------------------------------------

@pytest.mark.satellite
def test_stille_innerhalb_der_karenzzeit_beendet_nicht():
    """Before the grace period runs out, silence does not count."""
    sat = _make_satellite(max_recording_seconds=60.0)
    grace_chunks = int(MIN_LISTENING_S * 1000 / (CHUNK_SECONDS * 1000))
    _feed(sat, grace_chunks - 1, speech=False)

    assert sat._silence_chunks == 0
    assert _reasons(sat) == []


@pytest.mark.satellite
def test_stille_nach_der_karenzzeit_beendet_den_zug():
    sat = _make_satellite(max_recording_seconds=60.0)
    grace_chunks = int(MIN_LISTENING_S * 1000 / (CHUNK_SECONDS * 1000))
    silence_needed = int(SILENCE_MS / (CHUNK_SECONDS * 1000))

    _feed(sat, grace_chunks, speech=True)      # fill the grace period with speech
    _feed(sat, silence_needed, speech=False)

    assert "silence" in _reasons(sat)


@pytest.mark.satellite
def test_sprache_setzt_den_stillezaehler_zurueck():
    """A pause in mid-sentence must not end the turn."""
    sat = _make_satellite(max_recording_seconds=60.0)
    grace_chunks = int(MIN_LISTENING_S * 1000 / (CHUNK_SECONDS * 1000))
    silence_needed = int(SILENCE_MS / (CHUNK_SECONDS * 1000))

    _feed(sat, grace_chunks, speech=True)
    _feed(sat, silence_needed - 1, speech=False)   # just under the threshold
    _feed(sat, 1, speech=True)                     # the speaker starts again

    assert sat._silence_chunks == 0
    assert "silence" not in _reasons(sat)


# ---------------------------------------------------------------------------
# Invariant: the counter is reset everywhere
# ---------------------------------------------------------------------------

@pytest.mark.satellite
def test_zaehler_wird_an_jeder_ruecksetzstelle_genullt():
    """Wherever ``_silence_chunks`` is zeroed, ``_recorded_chunks`` MUST go too.

    A new turn that inherits the previous one's counter would be cut off
    immediately -- this invariant is the reason the counter can replace the
    buffer safely at all.
    """
    # Resolve via the module, not via a cwd-relative path: the suite also runs
    # in the container from a different working directory.
    src = pathlib.Path(inspect.getsourcefile(Satellite)).read_text()
    tree = ast.parse(src)

    def zeroed_names(node) -> set:
        found = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Assign):
                continue
            if not (isinstance(child.value, ast.Constant) and child.value.value == 0):
                continue
            for target in child.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    if target.value.id == "self":
                        found.add(target.attr)
        return found

    def increments_recorded(node) -> bool:
        """The producer itself -- it counts up, it does not reset."""
        return any(
            isinstance(child, ast.AugAssign)
            and isinstance(child.target, ast.Attribute)
            and child.target.attr == "_recorded_chunks"
            for child in ast.walk(node)
        )

    offenders = []
    checked = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if increments_recorded(func):
            # ``_on_audio_chunk`` zeroes ``_silence_chunks`` in mid-turn (the
            # speaker starts again). That is NOT a turn reset -- the recording
            # length counter has to keep running precisely there.
            continue
        names = zeroed_names(func)
        if "_silence_chunks" in names:
            checked.append(func.name)
            if "_recorded_chunks" not in names:
                offenders.append(func.name)

    # Without this lower bound the test would pass empty after a rename.
    assert len(checked) >= 4, (
        f"Erwartet: __init__, Weckwort, Reset, Trennung, Server-Zustand — gefunden: {checked}"
    )
    assert offenders == [], (
        f"Diese Methoden nullen _silence_chunks, aber nicht _recorded_chunks: {offenders}"
    )



# ---------------------------------------------------------------------------
# Turn to turn: a new turn inherits nothing from the previous one
# ---------------------------------------------------------------------------

def _end_turn_like_production(sat: Satellite) -> None:
    """``_end_listening`` does NOT reset -- the counter stays where it is."""
    sat._state = SatelliteState.PROCESSING


@pytest.mark.satellite
async def test_neuer_weckwort_zug_beginnt_bei_null():
    """After a 60 s turn the next one must not end on the first chunk."""
    sat = _make_satellite(max_recording_seconds=60.0)
    _feed(sat, 751, speech=True)
    assert "timeout" in _reasons(sat)
    sat._end_listening.reset_mock()

    # Back to IDLE, WITHOUT zeroing the counter by hand -- that is exactly what
    # the real wake-word path is supposed to do.
    sat._state = SatelliteState.IDLE
    sat._session_counter = MagicMock()
    sat._wakeword_pending = True
    sat._pending_snapshot = None
    sat.camera = None
    sat.leds = MagicMock()
    sat._set_state = lambda st: setattr(sat, "_state", st)
    sat.ws_client.is_connected = True
    sat.ws_client.send_wakeword_detected = AsyncMock(return_value=None)

    await Satellite._on_wakeword_detected(sat, "renfield", 0.9)

    assert sat._state == SatelliteState.LISTENING
    assert sat._recorded_chunks == 0
    # The VAD is a streaming model: otherwise its state comes from the END of
    # the previous turn.
    sat.vad.reset.assert_called_once()
    _feed(sat, 1, speech=True)
    assert _reasons(sat) == []


@pytest.mark.satellite
def test_server_befohlener_zug_beginnt_bei_null():
    """``state: listening`` from the server starts a turn like the wake word does."""
    sat = _make_satellite(max_recording_seconds=60.0)
    _feed(sat, 751, speech=True)
    sat._end_listening.reset_mock()
    _end_turn_like_production(sat)
    sat._set_state = lambda st: setattr(sat, "_state", st)

    Satellite._on_server_state_change(sat, "listening")

    assert sat._state == SatelliteState.LISTENING
    assert sat._recorded_chunks == 0
    assert sat._silence_chunks == 0
    sat.vad.reset.assert_called_once()
    _feed(sat, 1, speech=True)
    assert _reasons(sat) == []
