"""The backend's VAD floor must be the floor the fleet really has (#1386).

The backend classifies an empty voice turn as `vad_floor` (the VAD saw no
speech after the grace period — the signature of a false wake) when the
recording ends at the earliest moment the satellite allows. It does not get
the satellite's VAD settings over the wire, so it carries them as constants in
`src/backend/ha_glue/services/voice_empty_turn.py`.

🛑 A fleet setting has TWO sources: Ansible group_vars for the Pis and the
ConfigMap in `k8s/satellite-esszimmer.yaml` for the pod. This test reads both,
plus the satellite code default for what neither sets, and then MEASURES the
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
POD_MANIFEST = REPO / "k8s/satellite-esszimmer.yaml"
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


def _pod_config() -> dict:
    for doc in yaml.safe_load_all(POD_MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap":
            for value in (doc.get("data") or {}).values():
                cfg = yaml.safe_load(value)
                if isinstance(cfg, dict) and "vad" in cfg:
                    return cfg
    raise AssertionError("no satellite config with a `vad` block in the pod ConfigMap")


def _fleet_values() -> list[tuple[str, float, int, int]]:
    """(source, min_listening_s, silence_ms, chunk_size) for Pis and the pod."""
    group_vars = yaml.safe_load(GROUP_VARS.read_text())
    template = TEMPLATE.read_text()
    default_min = VADConfig().min_listening_seconds

    # The template renders min_listening only if it names it; otherwise the
    # code default applies on the Pis.
    pi_min = default_min
    if "min_listening_seconds" in template:
        pi_min = float(group_vars["vad_min_listening_seconds"])
    pi_chunk = int(re.search(r"chunk_size:\s*(\d+)", template).group(1))
    pi = ("pis", pi_min, int(group_vars["vad_silence_duration_ms"]), pi_chunk)

    pod_cfg = _pod_config()
    pod = (
        "esszimmer-pod",
        float(pod_cfg["vad"].get("min_listening_seconds", default_min)),
        int(pod_cfg["vad"]["silence_duration_ms"]),
        int(pod_cfg["audio"]["chunk_size"]),
    )
    return [pi, pod]


def _measured_floor_seconds(min_listening_s: float, silence_ms: int, chunk_size: int) -> float:
    """Feed no-speech chunks into the real `_on_audio_chunk` until it ends."""
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
    sat.vad.is_speech = MagicMock(return_value=False)
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


@pytest.mark.satellite
@pytest.mark.parametrize("source,min_s,silence_ms,chunk", _fleet_values())
def test_the_backend_floor_is_the_measured_fleet_floor(source, min_s, silence_ms, chunk):
    consts = _backend_constants()
    floor = (
        int(consts["FLEET_MIN_LISTENING_SECONDS"] * 1000 / (consts["SATELLITE_CHUNK_SECONDS"] * 1000))
        + int(consts["FLEET_SILENCE_DURATION_MS"] / (consts["SATELLITE_CHUNK_SECONDS"] * 1000))
        - 1
    ) * consts["SATELLITE_CHUNK_SECONDS"]
    measured = _measured_floor_seconds(min_s, silence_ms, chunk)
    assert measured == pytest.approx(floor), (
        f"{source}: the satellite ends a speechless turn after {measured:.2f} s, "
        f"the backend classifies against {floor:.2f} s — update voice_empty_turn.py"
    )


@pytest.mark.satellite
def test_the_measured_floor_is_the_one_from_the_issue():
    """Precondition for the test above: the harness really measures 3.12 s
    with the fleet values (the length 133 recordings had in #1386)."""
    assert _measured_floor_seconds(2.0, 1200, 1280) == pytest.approx(3.12)


@pytest.mark.satellite
def test_the_opus_packet_length_matches():
    consts = _backend_constants()
    assert opus_codec.PACKET_SAMPLES / opus_codec.SAMPLE_RATE == pytest.approx(
        consts["OPUS_PACKET_SECONDS"]
    )
