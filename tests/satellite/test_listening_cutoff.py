"""
Aufnahme-Abschneidung im LISTENING-Zustand.

Deckt die beiden Abbruchgruende ab, die einen Sprachzug beenden:

* ``silence``  — VAD meldet nach der Karenzzeit durchgehend Stille
* ``timeout``  — die Aufnahme ueberschreitet ``vad.max_recording_seconds``

Kernregression (2026-09-19): beide Pruefungen lasen ``len(self._audio_buffer)``,
der bei ``MAX_AUDIO_BUFFER_CHUNKS`` (= 500 Chunks = 40.0 s) gedeckelt ist. Sobald
``max_recording_seconds`` ueber 40 s angehoben wurde, konnte die Laengenpruefung
NIE mehr ausloesen — der Satellit beendete den Zug nicht mehr selbst, und der Ton
verfiel still im 120-s-Aufraeumlauf des Backends, ohne Antwort an den Sprecher.
Der Zug wird deshalb ueber ``_recorded_chunks`` gemessen, nicht ueber den
gedeckelten Puffer.
"""

import ast
import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from renfield_satellite.satellite import (
    MAX_AUDIO_BUFFER_CHUNKS,
    Satellite,
    SatelliteState,
)

CHUNK_SIZE = 1280
SAMPLE_RATE = 16000
CHUNK_SECONDS = CHUNK_SIZE / SAMPLE_RATE  # 0.08 s
SILENCE_MS = 1200
MIN_LISTENING_S = 1.0


def _make_satellite(max_recording_seconds: float) -> Satellite:
    """Ein Satellit ohne Hardware: nur, was ``_on_audio_chunk`` wirklich anfasst."""
    sat = object.__new__(Satellite)

    sat.config = MagicMock()
    sat.config.audio.chunk_size = CHUNK_SIZE
    sat.config.audio.sample_rate = SAMPLE_RATE
    sat.config.vad.min_listening_seconds = MIN_LISTENING_S
    sat.config.vad.silence_duration_ms = SILENCE_MS
    sat.config.vad.max_recording_seconds = max_recording_seconds

    sat._state = SatelliteState.LISTENING
    sat._audio_buffer = []
    sat._silence_chunks = 0
    sat._recorded_chunks = 0
    sat._session_id = None          # kein Streaming -> kein ws_client noetig
    sat._current_audio_rms = 0.0
    sat._current_audio_db = 0.0
    sat._current_is_speech = False

    sat.wakeword = MagicMock()
    sat.wakeword.active_stop_words = []   # Stopwort-Zweig aus
    sat.preprocessor = MagicMock()
    sat.preprocessor.normalize = lambda b: b
    sat.vad = MagicMock()
    sat.ws_client = MagicMock()

    # _end_listening ist async; als MagicMock entsteht keine nie erwartete Koroutine.
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
# Laengenabschneidung — die Regression
# ---------------------------------------------------------------------------

@pytest.mark.satellite
def test_laengenabbruch_feuert_jenseits_der_puffergrenze():
    """60 s Limit > 40 s Puffergrenze: der Abbruch MUSS trotzdem kommen."""
    sat = _make_satellite(max_recording_seconds=60.0)
    # 751 Chunks = 60.08 s — die erste Position echt oberhalb von 60 s.
    _feed(sat, 751, speech=True)

    assert len(sat._audio_buffer) == MAX_AUDIO_BUFFER_CHUNKS, (
        "Vorbedingung: der Puffer MUSS gedeckelt sein, sonst prueft der Test nichts"
    )
    assert sat._recorded_chunks == 751
    assert "timeout" in _reasons(sat)


@pytest.mark.satellite
def test_laengenabbruch_nicht_vor_dem_limit():
    """Exakt 60.0 s ist nicht ueber 60 s — noch kein Abbruch."""
    sat = _make_satellite(max_recording_seconds=60.0)
    _feed(sat, 750, speech=True)   # 750 * 0.08 = 60.0 s

    assert sat._recorded_chunks == 750
    assert "timeout" not in _reasons(sat)


@pytest.mark.satellite
def test_zaehler_laeuft_unabhaengig_von_der_puffergrenze_weiter():
    """Der gedeckelte Puffer darf den Zug-Zaehler nicht einfrieren."""
    sat = _make_satellite(max_recording_seconds=3600.0)
    _feed(sat, MAX_AUDIO_BUFFER_CHUNKS + 120, speech=True)

    assert len(sat._audio_buffer) == MAX_AUDIO_BUFFER_CHUNKS
    assert sat._recorded_chunks == MAX_AUDIO_BUFFER_CHUNKS + 120


@pytest.mark.satellite
def test_kurzes_limit_unterhalb_der_puffergrenze_feuert_weiterhin():
    """Das alte Verhalten (20 s < 40 s) bleibt unveraendert."""
    sat = _make_satellite(max_recording_seconds=20.0)
    _feed(sat, 251, speech=True)   # 251 * 0.08 = 20.08 s

    assert "timeout" in _reasons(sat)


# ---------------------------------------------------------------------------
# Stille-Abschneidung + Karenzzeit
# ---------------------------------------------------------------------------

@pytest.mark.satellite
def test_stille_innerhalb_der_karenzzeit_beendet_nicht():
    """Vor Ablauf der Karenzzeit zaehlt Stille nicht."""
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

    _feed(sat, grace_chunks, speech=True)      # Karenzzeit mit Sprache fuellen
    _feed(sat, silence_needed, speech=False)

    assert "silence" in _reasons(sat)


@pytest.mark.satellite
def test_sprache_setzt_den_stillezaehler_zurueck():
    """Eine Pause mitten im Satz darf den Zug nicht beenden."""
    sat = _make_satellite(max_recording_seconds=60.0)
    grace_chunks = int(MIN_LISTENING_S * 1000 / (CHUNK_SECONDS * 1000))
    silence_needed = int(SILENCE_MS / (CHUNK_SECONDS * 1000))

    _feed(sat, grace_chunks, speech=True)
    _feed(sat, silence_needed - 1, speech=False)   # knapp unter der Schwelle
    _feed(sat, 1, speech=True)                     # Sprecher setzt wieder ein

    assert sat._silence_chunks == 0
    assert "silence" not in _reasons(sat)


# ---------------------------------------------------------------------------
# Invariante: der Zaehler wird ueberall zurueckgesetzt
# ---------------------------------------------------------------------------

@pytest.mark.satellite
def test_zaehler_wird_an_jeder_ruecksetzstelle_genullt():
    """Wo ``_silence_chunks`` genullt wird, MUSS ``_recorded_chunks`` mitgehen.

    Ein neuer Zug, der den Zaehler des vorherigen erbt, wuerde sofort
    abgeschnitten — diese Invariante ist der Grund, warum der Zaehler
    ueberhaupt gefahrlos den Puffer ersetzen kann.
    """
    # Ueber das Modul aufloesen, nicht ueber einen cwd-relativen Pfad: die Suite
    # laeuft auch im Container aus einem anderen Arbeitsverzeichnis.
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
        """Der Produzent selbst — er zaehlt hoch, er setzt nicht zurueck."""
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
            # ``_on_audio_chunk`` nullt ``_silence_chunks`` mitten im Zug (Sprecher
            # setzt wieder ein). Das ist KEINE Zug-Ruecksetzung — der Zaehler der
            # Aufnahmelaenge muss dort gerade weiterlaufen.
            continue
        names = zeroed_names(func)
        if "_silence_chunks" in names:
            checked.append(func.name)
            if "_recorded_chunks" not in names:
                offenders.append(func.name)

    # Ohne diese Untergrenze bestuende der Test nach einer Umbenennung leer.
    assert len(checked) >= 4, (
        f"Erwartet: __init__, Weckwort, Reset, Trennung, Server-Zustand — gefunden: {checked}"
    )
    assert offenders == [], (
        f"Diese Methoden nullen _silence_chunks, aber nicht _recorded_chunks: {offenders}"
    )



# ---------------------------------------------------------------------------
# Zug zu Zug: ein neuer Zug erbt nichts vom vorherigen
# ---------------------------------------------------------------------------

def _end_turn_like_production(sat: Satellite) -> None:
    """``_end_listening`` setzt NICHT zurueck — der Zaehler bleibt stehen."""
    sat._state = SatelliteState.PROCESSING


@pytest.mark.satellite
async def test_neuer_weckwort_zug_beginnt_bei_null():
    """Nach einem 60-s-Zug darf der naechste nicht beim ersten Chunk enden."""
    sat = _make_satellite(max_recording_seconds=60.0)
    _feed(sat, 751, speech=True)
    assert "timeout" in _reasons(sat)
    sat._end_listening.reset_mock()

    # Zurueck nach IDLE, OHNE den Zaehler von Hand zu nullen — genau das soll
    # der echte Weckwort-Pfad leisten.
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
    _feed(sat, 1, speech=True)
    assert _reasons(sat) == []


@pytest.mark.satellite
def test_server_befohlener_zug_beginnt_bei_null():
    """``state: listening`` vom Server ist ein Zugbeginn wie das Weckwort."""
    sat = _make_satellite(max_recording_seconds=60.0)
    _feed(sat, 751, speech=True)
    sat._end_listening.reset_mock()
    _end_turn_like_production(sat)
    sat._set_state = lambda st: setattr(sat, "_state", st)

    Satellite._on_server_state_change(sat, "listening")

    assert sat._state == SatelliteState.LISTENING
    assert sat._recorded_chunks == 0
    assert sat._silence_chunks == 0
    _feed(sat, 1, speech=True)
    assert _reasons(sat) == []
