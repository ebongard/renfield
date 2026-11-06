"""Speech heard but not understood gets an LED cue; a false wake gets none (#1386).

Two halves of one satellite change (v1.4.13):

* ``audio_end`` carries ``vad_speech_seen``: whether the VAD saw speech in ANY
  chunk of the turn, the grace period included. The backend used to infer it
  from the recording length, which cannot see a short command spoken entirely
  inside the grace period (it ends at the floor, like a false wake).
* the backend answers an empty turn of heard speech with
  ``{"type": "feedback_cue", "cue": "not_understood"}``; the satellite shows a
  short distinct LED pattern while idle and then returns to the idle pattern.
  No sound. The LED brightness stays the controller's, so night dimming
  applies to the cue as to every other pattern.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from renfield_satellite import satellite as satellite_mod
from renfield_satellite.hardware import led as led_mod
from renfield_satellite.hardware.led import (
    COLORS,
    CUE_COLOR,
    GPIOLEDController,
    LEDController,
    LEDPattern,
    XVF3800LEDController,
    cue_blink_on,
)
from renfield_satellite.network.websocket_client import WebSocketClient
from renfield_satellite.satellite import Satellite, SatelliteState

pytestmark = pytest.mark.satellite

CHUNK_SIZE = 1280
SAMPLE_RATE = 16000
MIN_LISTENING_S = 2.0
SILENCE_MS = 1200
GRACE_CHUNKS = 25
SILENCE_CHUNKS = 15


# ---------------------------------------------------------------------------
# The VAD verdict of a turn
# ---------------------------------------------------------------------------

def _listening_satellite() -> Satellite:
    """A satellite without hardware, LISTENING, with the fleet VAD settings."""
    sat = object.__new__(Satellite)
    sat.config = MagicMock()
    sat.config.audio.chunk_size = CHUNK_SIZE
    sat.config.audio.sample_rate = SAMPLE_RATE
    sat.config.vad.min_listening_seconds = MIN_LISTENING_S
    sat.config.vad.silence_duration_ms = SILENCE_MS
    sat.config.vad.max_recording_seconds = 60
    sat._state = SatelliteState.LISTENING
    sat._silence_chunks = 0
    sat._recorded_chunks = 0
    sat._vad_speech_seen = False
    sat._session_id = None
    sat.wakeword = MagicMock()
    sat.wakeword.active_stop_words = []
    sat.preprocessor = MagicMock()
    sat.preprocessor.normalize = lambda b: b
    sat.vad = MagicMock()
    sat._end_listening = MagicMock(return_value=None)
    sat._schedule_async = MagicMock()
    return sat


def _feed(sat: Satellite, verdicts: list[bool]) -> None:
    audio = b"\x01\x00" * (CHUNK_SIZE // 2)
    for speech in verdicts:
        sat.vad.is_speech = MagicMock(return_value=speech)
        sat._on_audio_chunk(audio)


def _end_reasons(sat: Satellite) -> list[str]:
    return [c.args[0] for c in sat._end_listening.call_args_list]


def test_a_turn_without_any_speech_reports_none():
    """The false-wake signature: silence throughout, end at the floor."""
    sat = _listening_satellite()
    _feed(sat, [False] * (GRACE_CHUNKS + SILENCE_CHUNKS))
    assert "silence" in _end_reasons(sat)
    assert sat._vad_speech_seen is False


def test_speech_only_inside_the_grace_period_is_reported():
    """The case the length rule cannot see: the turn ends AT the floor (the
    grace period resets the silence count anyway), yet the VAD heard speech."""
    sat = _listening_satellite()
    _feed(sat, [True] * 5 + [False] * (GRACE_CHUNKS - 5 + SILENCE_CHUNKS - 1))
    assert sat._recorded_chunks == GRACE_CHUNKS + SILENCE_CHUNKS - 1, "ends at the floor"
    assert "silence" in _end_reasons(sat)
    assert sat._vad_speech_seen is True


def test_speech_after_the_grace_period_is_reported():
    sat = _listening_satellite()
    _feed(sat, [False] * GRACE_CHUNKS + [True] * 3 + [False] * SILENCE_CHUNKS)
    assert "silence" in _end_reasons(sat)
    assert sat._vad_speech_seen is True


async def test_speech_during_the_state_change_survives_the_reset():
    """Review P3: `_set_state(LISTENING)` can block the loop (stopping the LED
    animation thread joins it for up to 0.5 s) while the audio thread already
    streams LISTENING chunks. The turn state must be reset BEFORE the state
    change, or a chunk processed meanwhile loses its count and its speech
    verdict — exactly the grace-period region the report exists for.

    The real `_set_state` runs; the fake LED controller plays one audio chunk
    with speech from inside `set_pattern`, as the audio thread would."""
    sat = _listening_satellite()
    sat._state = SatelliteState.IDLE
    sat._wakeword_pending = True
    sat._last_wakeword = None
    sat._session_counter = MagicMock()
    sat.ws_client = MagicMock(is_connected=True)
    sat.ws_client.send_wakeword_detected = AsyncMock(return_value="s2")
    sat.camera = None
    sat.display = None
    sat._vad_speech_seen = True    # left over from the previous turn
    sat._recorded_chunks = 99

    class _LedsThatYield:
        def set_pattern(self, pattern):
            if pattern == LEDPattern.LISTENING:
                _feed(sat, [True])

    sat.leds = _LedsThatYield()
    await sat._on_wakeword_detected("hey_renfield", 0.9)
    assert sat._state == SatelliteState.LISTENING
    assert sat._recorded_chunks == 1, "the chunk streamed during the state change counts"
    assert sat._vad_speech_seen is True, "its speech verdict survives"


async def test_a_wake_word_turn_starts_without_speech_seen():
    """Reset at every LISTENING entry, like `_recorded_chunks`: a verdict must
    never carry over from the previous turn."""
    sat = object.__new__(Satellite)
    sat._state = SatelliteState.IDLE
    sat._last_wakeword = None
    sat._session_counter = MagicMock()
    sat.ws_client = MagicMock(is_connected=True)
    sat.ws_client.send_wakeword_detected = AsyncMock(return_value="s2")
    sat.camera = None
    sat.vad = MagicMock()
    sat.wakeword = MagicMock()
    sat._set_state = MagicMock(side_effect=lambda s: setattr(sat, "_state", s))
    sat._vad_speech_seen = True  # left over from the previous turn
    await sat._on_wakeword_detected("hey_renfield", 0.9)
    assert sat._state == SatelliteState.LISTENING
    assert sat._vad_speech_seen is False


def test_a_server_commanded_turn_starts_without_speech_seen():
    sat = object.__new__(Satellite)
    sat.vad = MagicMock()
    sat._set_state = MagicMock()
    sat._schedule_async = MagicMock()
    sat._vad_speech_seen = True
    sat._on_server_state_change("listening")
    assert sat._vad_speech_seen is False


@pytest.mark.parametrize("seen", [True, False])
async def test_end_listening_sends_the_verdict(seen):
    sat = object.__new__(Satellite)
    sat._state = SatelliteState.LISTENING
    sat._set_state = MagicMock()
    sat._pending_snapshot = None
    sat._session_id = "s1"
    sat._vad_speech_seen = seen
    sat.ws_client = MagicMock()
    sat.ws_client.send_audio_end = AsyncMock()
    await sat._end_listening("silence")
    sat.ws_client.send_audio_end.assert_awaited_once_with(
        "s1", "silence", image_b64=None, vad_speech_seen=seen,
    )


# ---------------------------------------------------------------------------
# The wire: audio_end field, feedback_cue dispatch
# ---------------------------------------------------------------------------

def _client() -> WebSocketClient:
    client = WebSocketClient(
        satellite_id="sat-test", room="TestRoom", server_url="wss://x/ws/satellite",
    )
    client._ws = MagicMock()
    client._send = AsyncMock()
    return client


@pytest.mark.parametrize("seen", [True, False])
async def test_audio_end_carries_the_field(seen, monkeypatch):
    client = _client()
    monkeypatch.setattr(WebSocketClient, "is_connected", property(lambda self: True))
    await client.send_audio_end("s1", "silence", vad_speech_seen=seen)
    msg = client._send.await_args.args[0]
    assert msg == {"type": "audio_end", "session_id": "s1", "reason": "silence",
                   "vad_speech_seen": seen}
    assert json.loads(json.dumps(msg))["vad_speech_seen"] is seen, "a real JSON boolean"


async def test_audio_end_without_a_verdict_leaves_the_field_out(monkeypatch):
    """The cancel path sends no verdict; an absent field is the old wire."""
    client = _client()
    monkeypatch.setattr(WebSocketClient, "is_connected", property(lambda self: True))
    await client.send_audio_end("s1", "canceled")
    assert "vad_speech_seen" not in client._send.await_args.args[0]


async def test_a_feedback_cue_reaches_the_callback():
    client = _client()
    got: list[str] = []
    client.on_feedback_cue(got.append)
    await client._handle_message({"type": "feedback_cue", "cue": "not_understood"})
    assert got == ["not_understood"]


@pytest.mark.parametrize("frame", [
    {"type": "feedback_cue"},
    {"type": "feedback_cue", "cue": 3},
    {"type": "feedback_cue", "cue": None},
])
async def test_a_malformed_cue_is_ignored(frame):
    client = _client()
    got: list = []
    client.on_feedback_cue(got.append)
    await client._handle_message(frame)
    assert got == []


async def test_a_cue_without_a_callback_does_not_raise():
    await _client()._handle_message({"type": "feedback_cue", "cue": "not_understood"})


# ---------------------------------------------------------------------------
# The satellite's cue handler
# ---------------------------------------------------------------------------

class _FakeLeds:
    def __init__(self, pattern=LEDPattern.IDLE):
        self.current_pattern = pattern
        self.calls: list[LEDPattern] = []

    def set_pattern(self, pattern):
        self.calls.append(pattern)
        self.current_pattern = pattern


def _cue_satellite(state=SatelliteState.IDLE) -> Satellite:
    sat = object.__new__(Satellite)
    sat._state = state
    sat._cue_generation = 0
    sat.leds = _FakeLeds()
    scheduled: list = []
    sat._schedule_async = scheduled.append
    sat._scheduled = scheduled
    return sat


async def _run_scheduled(sat):
    for coro in sat._scheduled:
        await coro
    sat._scheduled.clear()


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(satellite_mod, "NOT_UNDERSTOOD_CUE_SECONDS", 0)


async def test_cue_shows_the_pattern_then_returns_to_idle(no_wait):
    sat = _cue_satellite()
    sat._on_feedback_cue("not_understood")
    assert sat.leds.calls == [LEDPattern.NOT_UNDERSTOOD]
    await _run_scheduled(sat)
    assert sat.leds.calls == [LEDPattern.NOT_UNDERSTOOD, LEDPattern.IDLE]


async def test_cue_waits_its_duration_before_idle(monkeypatch):
    waited: list[float] = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(satellite_mod.asyncio, "sleep", fake_sleep)
    sat = _cue_satellite()
    sat._on_feedback_cue("not_understood")
    await _run_scheduled(sat)
    assert waited == [satellite_mod.NOT_UNDERSTOOD_CUE_SECONDS]
    assert 0.6 <= satellite_mod.NOT_UNDERSTOOD_CUE_SECONDS <= 3.0, "short, but long enough for both blinks"


@pytest.mark.parametrize("state", [
    SatelliteState.LISTENING, SatelliteState.PROCESSING, SatelliteState.SPEAKING,
])
async def test_no_cue_outside_idle(state, no_wait):
    """A new turn has priority: a cue arriving late must not paint over it."""
    sat = _cue_satellite(state)
    sat._on_feedback_cue("not_understood")
    assert sat.leds.calls == []
    assert sat._scheduled == []


async def test_an_unknown_cue_is_ignored(no_wait):
    sat = _cue_satellite()
    sat._on_feedback_cue("applause")
    assert sat.leds.calls == []
    assert sat._scheduled == []


async def test_a_new_turn_during_the_cue_keeps_its_pattern(no_wait):
    """Wake word during the cue: LISTENING owns the LEDs; the cue's end must
    not reset them to IDLE."""
    sat = _cue_satellite()
    sat._on_feedback_cue("not_understood")
    sat._state = SatelliteState.LISTENING
    sat.leds.set_pattern(LEDPattern.LISTENING)
    await _run_scheduled(sat)
    assert sat.leds.current_pattern == LEDPattern.LISTENING


async def test_another_pattern_during_the_cue_is_kept(no_wait):
    """Still idle, but something else took the LEDs (e.g. an error flash)."""
    sat = _cue_satellite()
    sat._on_feedback_cue("not_understood")
    sat.leds.set_pattern(LEDPattern.ERROR)
    await _run_scheduled(sat)
    assert sat.leds.current_pattern == LEDPattern.ERROR


async def test_a_stale_cue_end_does_not_cut_a_newer_cue(no_wait):
    sat = _cue_satellite()
    sat._on_feedback_cue("not_understood")
    first_end = sat._scheduled.pop(0)
    sat._on_feedback_cue("not_understood")
    await first_end
    assert sat.leds.current_pattern == LEDPattern.NOT_UNDERSTOOD
    await _run_scheduled(sat)
    assert sat.leds.current_pattern == LEDPattern.IDLE


def test_the_satellite_wires_the_cue_callback():
    """`_setup_callbacks` registers the handler on the client."""
    import inspect

    assert "on_feedback_cue(self._on_feedback_cue)" in inspect.getsource(Satellite._setup_callbacks)


# ---------------------------------------------------------------------------
# The LED pattern: distinct, short, dimmed with the rest
# ---------------------------------------------------------------------------

def test_the_cue_blinks_twice_then_stays_dark():
    lit = [cue_blink_on(f) for f in range(24)]
    assert lit[:16] == [True] * 4 + [False] * 4 + [True] * 4 + [False] * 4
    assert not any(lit[16:])
    assert cue_blink_on(-1) is False


def _rgb(c):
    return (c.r, c.g, c.b)


def test_the_cue_colour_is_no_selectable_colour():
    """Distinct from everything a device can show otherwise. `led.idle_color`
    may name ANY entry of `COLORS` (`COLORS.get(idle_color, blue)`; purple is
    one of them, the Esszimmer pod runs yellow), and the state patterns use
    entries of `COLORS` too — so the cue colour must be outside `COLORS`."""
    assert _rgb(CUE_COLOR) not in {_rgb(c) for c in COLORS.values()}


def test_the_configured_idle_colours_are_all_in_colors():
    """The invariant above only holds if every idle colour the fleet actually
    configures goes through `COLORS`: the Ansible template default (host_vars
    are gitignored; they can only name `COLORS` entries or fall back to blue).
    Idle is blue everywhere since 1.4.16; yellow is PROCESSING."""
    import pathlib
    import re

    repo = pathlib.Path(__file__).resolve().parents[2]
    template = (repo / "src/satellite/provisioning/templates/satellite.yaml.j2").read_text()
    names = set(re.findall(r"led_idle_color \| default\('(\w+)'\)", template))
    assert "blue" in names, names  # the template default; proves the pattern still matches
    assert names <= set(COLORS), names


def _one_frame(ctrl, monkeypatch):
    """Run exactly one frame of the controller's animation loop."""
    def stop(_s):
        ctrl._running = False

    monkeypatch.setattr(led_mod.time, "sleep", stop)
    ctrl._pattern = LEDPattern.NOT_UNDERSTOOD
    ctrl._running = True
    ctrl._animation_loop()


def test_apa102_cue_is_the_cue_colour_at_the_dimmed_brightness(monkeypatch):
    """Night dimming: the cue is capped by the controller's brightness, which
    the backend lowers at night (`led_config`)."""
    ctrl = LEDController(num_leds=3, brightness=20)
    ctrl.brightness = 5  # a night push
    _one_frame(ctrl, monkeypatch)
    assert [(c.r, c.g, c.b, c.brightness) for c in ctrl._colors] == [
        (CUE_COLOR.r, CUE_COLOR.g, CUE_COLOR.b, 5),
    ] * 3


def test_apa102_cue_is_dark_between_blinks(monkeypatch):
    ctrl = LEDController(num_leds=3, brightness=20)

    calls = {"n": 0}

    def stop_after(_s):
        calls["n"] += 1
        if calls["n"] >= 5:  # frame 4 = the first dark frame
            ctrl._running = False

    monkeypatch.setattr(led_mod.time, "sleep", stop_after)
    ctrl._pattern = LEDPattern.NOT_UNDERSTOOD
    ctrl._running = True
    ctrl._animation_loop()
    assert all((c.r, c.g, c.b) == (0, 0, 0) for c in ctrl._colors)


def test_gpio_cue_is_the_cue_colour_scaled_by_brightness(monkeypatch):
    ctrl = GPIOLEDController(brightness=31)
    ctrl.brightness = 5
    colors: list[tuple[int, int, int]] = []
    monkeypatch.setattr(ctrl, "_set_color", lambda r, g, b: colors.append((r, g, b)))
    _one_frame(ctrl, monkeypatch)
    assert colors == [_rgb(CUE_COLOR)]


def test_gpio_cue_is_dark_between_blinks(monkeypatch):
    ctrl = GPIOLEDController(brightness=31)
    colors: list[tuple[int, int, int]] = []
    monkeypatch.setattr(ctrl, "_set_color", lambda r, g, b: colors.append((r, g, b)))
    calls = {"n": 0}

    def stop_after(_s):
        calls["n"] += 1
        if calls["n"] >= 5:
            ctrl._running = False

    monkeypatch.setattr(led_mod.time, "sleep", stop_after)
    ctrl._pattern = LEDPattern.NOT_UNDERSTOOD
    ctrl._running = True
    ctrl._animation_loop()
    assert colors[-1] == (0, 0, 0)


def test_xvf3800_cue_is_a_distinct_solid_colour():
    """The XMOS has no blink effect; the cue is a colour no state uses, at
    the chip's (night-dimmed) LED_BRIGHTNESS."""
    ctrl = XVF3800LEDController()
    ctrl._run = MagicMock()
    ctrl.set_pattern(LEDPattern.NOT_UNDERSTOOD)
    calls = [c.args for c in ctrl._run.call_args_list]
    hex_cue = (CUE_COLOR.r << 16) | (CUE_COLOR.g << 8) | CUE_COLOR.b
    assert calls == [("LED_EFFECT", "3"), ("LED_COLOR", f"0x{hex_cue:06x}")]
    others = {color for p, (_e, color) in ctrl._PATTERN_MAP.items() if p != LEDPattern.NOT_UNDERSTOOD}
    assert hex_cue not in others


def test_every_pattern_is_mapped_on_the_xvf3800():
    assert set(XVF3800LEDController._PATTERN_MAP) == set(LEDPattern)


def test_asyncio_is_the_module_used_for_the_wait():
    # Sanity for the monkeypatch in test_cue_waits_its_duration_before_idle.
    assert satellite_mod.asyncio is asyncio
