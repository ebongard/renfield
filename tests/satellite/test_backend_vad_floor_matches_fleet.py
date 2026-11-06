"""The backend's VAD floor must be the floor the fleet really has (#1386).

The backend classifies an empty voice turn as `vad_floor` (the VAD saw no
speech after the grace period — the signature of a false wake) when the
recording ends at the earliest moment the satellite allows. It does not get
the satellite's VAD settings over the wire, so it carries them as constants in
`src/backend/ha_glue/services/voice_empty_turn.py`.

The fleet's settings come from Ansible group_vars (every satellite, the Orange
Pis included, is provisioned from it since 2026-11-06). This test reads them,
plus the satellite code default for what they do not set, and then MEASURES the
floor on the real `_on_audio_chunk` — not a reconstruction of its arithmetic.

It lives here and not in tests/backend because the backend test copy on the
build box does not carry `src/satellite`.
"""

from __future__ import annotations

import ast
import pathlib
import re
from unittest.mock import MagicMock

import pytest
import yaml
from renfield_satellite.audio import opus_codec
from renfield_satellite.config import VADConfig
from renfield_satellite.satellite import Satellite, SatelliteState

REPO = pathlib.Path(__file__).resolve().parents[2]
BACKEND_MODULE = REPO / "src/backend/ha_glue/services/voice_empty_turn.py"
GROUP_VARS = REPO / "src/satellite/provisioning/group_vars/satellites.yml"
TEMPLATE = REPO / "src/satellite/provisioning/templates/satellite.yaml.j2"


def _backend_constants() -> dict[str, float]:
    """The module-level numeric constants of the backend classifier, read
    from the assignments themselves (no backend imports here)."""
    out: dict[str, float] = {}
    for node in ast.parse(BACKEND_MODULE.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            try:
                out[name] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return out


def _fleet_values() -> list[tuple[str, float, int, int]]:
    """(source, min_listening_s, silence_ms, chunk_size) for the fleet."""
    group_vars = yaml.safe_load(GROUP_VARS.read_text())
    template = TEMPLATE.read_text()
    default_min = VADConfig().min_listening_seconds

    # The template renders min_listening only if it names it; otherwise the
    # code default applies.
    pi_min = default_min
    if "min_listening_seconds" in template:
        pi_min = float(group_vars["vad_min_listening_seconds"])
    pi_chunk = int(re.search(r"chunk_size:\s*(\d+)", template).group(1))
    return [("fleet", pi_min, int(group_vars["vad_silence_duration_ms"]), pi_chunk)]


def _measured_floor_seconds(
    min_listening_s: float, silence_ms: int, chunk_size: int, speech: list[bool] | None = None,
) -> float:
    """Feed chunks into the real `_on_audio_chunk` until it ends on silence.
    ``speech`` is the VAD verdict per chunk (default: never speech)."""
    sat = object.__new__(Satellite)
    sat.config = MagicMock()
    sat.config.audio.chunk_size = chunk_size
    sat.config.audio.sample_rate = 16000
    sat.config.vad.min_listening_seconds = min_listening_s
    sat.config.vad.silence_duration_ms = silence_ms
    sat.config.vad.max_recording_seconds = 60
    sat._state = SatelliteState.LISTENING
    sat._silence_chunks = 0
    sat._recorded_chunks = 0
    sat._session_id = None
    sat._current_audio_rms = 0.0
    sat._current_audio_db = 0.0
    sat._current_is_speech = False
    sat.wakeword = MagicMock()
    sat.wakeword.active_stop_words = []
    sat.preprocessor = MagicMock()
    sat.preprocessor.normalize = lambda b: b
    sat.vad = MagicMock()
    verdicts = iter(speech or [])
    sat.vad.is_speech = MagicMock(side_effect=lambda _b: next(verdicts, False))
    sat.ws_client = MagicMock()
    sat._end_listening = MagicMock(return_value=None)
    sat._schedule_async = MagicMock()

    audio = b"\x01\x00" * (chunk_size // 2)
    for n in range(1, 2000):
        sat._on_audio_chunk(audio)
        if sat._end_listening.call_args_list:
            assert sat._end_listening.call_args_list[0].args[0] == "silence"
            return n * chunk_size / 16000
    raise AssertionError("the turn never ended on silence")


#: The floor the backend classifies against. 🛑 The SAME literal is pinned on
#: the backend side (`test_the_fleet_floor_is_the_measured_3_12_seconds` in
#: tests/backend/test_voice_empty_turn.py asserts `vad_floor_seconds()` is it).
#: Neither test recomputes the other side's formula: a fleet config change
#: breaks this test, a change to the backend constants breaks that one.
BACKEND_FLOOR_SECONDS = 3.12


@pytest.mark.satellite
@pytest.mark.parametrize("source,min_s,silence_ms,chunk", _fleet_values())
def test_the_fleet_ends_a_speechless_turn_at_the_backend_floor(source, min_s, silence_ms, chunk):
    measured = _measured_floor_seconds(min_s, silence_ms, chunk)
    assert measured == pytest.approx(BACKEND_FLOOR_SECONDS), (
        f"{source}: the satellite ends a speechless turn after {measured:.2f} s, "
        f"the backend classifies against {BACKEND_FLOOR_SECONDS:.2f} s — update "
        "the FLEET_* constants in voice_empty_turn.py and both pinned literals"
    )


@pytest.mark.satellite
def test_the_harness_sees_a_changed_setting():
    """Negative control: the measurement follows the settings, so the test
    above is not green merely because the harness always returns 3.12."""
    assert _measured_floor_seconds(2.0, 1500, 1280) != pytest.approx(BACKEND_FLOOR_SECONDS)
    assert _measured_floor_seconds(1.0, 1200, 1280) != pytest.approx(BACKEND_FLOOR_SECONDS)


@pytest.mark.satellite
def test_any_speech_after_the_grace_period_ends_later_than_the_floor():
    """The classifier's boundary from the other side: ONE speech chunk at the
    first counted position (chunk 25) already pushes the end one chunk past
    the floor (3.20 s). This is why the backend's slack is half a chunk, not
    one: a full chunk would swallow the shortest turn in which the VAD did
    hear speech."""
    first_counted = [False] * 24 + [True]
    assert _measured_floor_seconds(2.0, 1200, 1280, speech=first_counted) == pytest.approx(3.20)


@pytest.mark.satellite
def test_speech_inside_the_grace_period_still_ends_at_the_floor():
    """Why `vad_floor` is a signature, not proof: a command spoken entirely
    inside the 2 s grace period ends at the floor too."""
    inside_grace = [True] * 24
    assert _measured_floor_seconds(2.0, 1200, 1280, speech=inside_grace) == pytest.approx(
        BACKEND_FLOOR_SECONDS
    )


@pytest.mark.satellite
def test_the_opus_packet_length_matches():
    consts = _backend_constants()
    assert opus_codec.PACKET_SAMPLES / opus_codec.SAMPLE_RATE == pytest.approx(
        consts["OPUS_PACKET_SECONDS"]
    )
