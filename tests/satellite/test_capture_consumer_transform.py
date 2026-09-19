"""
Tests for moving stereo->mono beamforming off the capture read loop.

Background (2026-06-23): the PyAudio capture read loop did beamforming inline,
violating its own contract ("must never block on anything except stream.read()"
— an I2S buffer overflow can crash the kernel on Pi Zero 2 W). The beamforming /
mono-downmix now runs in the CONSUMER thread via ``_consumer_transform`` so the
read loop only reads + queues raw audio.
"""

import queue
import threading

import numpy as np

from renfield_satellite.audio.capture import AudioCapture


def _bare_capture(channels, beamformer=None, combine="select", select_channel=0):
    """An AudioCapture without hardware: only what ``_stereo_to_mono`` reads.

    ``combine``/``select_channel`` arrived after these tests were written. Without
    them ``_stereo_to_mono`` raised AttributeError — which the consumer loop
    swallows by design, so the loop test never saw its callback and spun forever.
    """
    cap = AudioCapture.__new__(AudioCapture)
    cap.channels = channels
    cap._beamformer = beamformer
    cap.combine = combine
    cap.select_channel = select_channel
    return cap


def _run_consumer_loop(cap, timeout=2.0):
    """Run the consumer loop with a deadline.

    The loop only ends when the callback clears ``_running``. If the callback is
    never reached (the loop swallows transform errors on purpose — right for a
    live capture thread), a direct call hangs the whole suite instead of failing.
    """
    worker = threading.Thread(target=cap._audio_consumer_loop, daemon=True)
    worker.start()
    worker.join(timeout)
    hung = worker.is_alive()
    cap._running = False  # let a stuck loop exit on its next queue timeout
    assert not hung, "consumer loop never reached the callback — see 'Audio consumer error' above"


def test_stereo_to_mono_passthrough_for_mono():
    cap = _bare_capture(channels=1)
    data = np.array([1, 2, 3, 4], dtype=np.int16).tobytes()
    assert cap._stereo_to_mono(data) == data


def test_stereo_to_mono_extracts_channel0_without_beamformer():
    cap = _bare_capture(channels=2, beamformer=None)
    # Interleaved L/R: L0,R0,L1,R1 -> mono should be [L0, L1]
    stereo = np.array([10, 20, 30, 40], dtype=np.int16).tobytes()
    mono = np.frombuffer(cap._stereo_to_mono(stereo), dtype=np.int16)
    assert list(mono) == [10, 30]


def test_stereo_to_mono_uses_beamformer_when_present():
    sentinel = b"beamformed"
    beamformer = type("BF", (), {"process_bytes": staticmethod(lambda b: sentinel)})()
    cap = _bare_capture(channels=2, beamformer=beamformer, combine="beamform")
    assert cap._stereo_to_mono(b"\x00\x00\x00\x00") == sentinel


def test_stereo_to_mono_select_ignores_a_present_beamformer():
    """combine=select keeps the chosen channel even when a beamformer exists."""
    beamformer = type("BF", (), {"process_bytes": staticmethod(lambda b: b"beamformed")})()
    cap = _bare_capture(channels=2, beamformer=beamformer, combine="select", select_channel=1)
    stereo = np.array([10, 20, 30, 40], dtype=np.int16).tobytes()
    assert list(np.frombuffer(cap._stereo_to_mono(stereo), dtype=np.int16)) == [20, 40]


def test_stereo_to_mono_passthrough_mode_keeps_multichannel_bytes():
    cap = _bare_capture(channels=2, combine="passthrough")
    data = np.array([1, 2, 3, 4], dtype=np.int16).tobytes()
    assert cap._stereo_to_mono(data) == data


def test_consumer_loop_applies_transform_before_callback():
    """The consumer thread must apply _consumer_transform, not pass raw stereo."""
    cap = _bare_capture(channels=2)
    cap._running = True
    cap._audio_queue = queue.Queue()
    cap._consumer_transform = cap._stereo_to_mono

    received = []

    def cb(chunk):
        received.append(chunk)
        cap._running = False  # stop the loop after one chunk (deterministic)

    cap._callback = cb

    stereo = np.array([10, 20, 30, 40], dtype=np.int16).tobytes()
    cap._audio_queue.put(stereo)
    _run_consumer_loop(cap)

    assert len(received) == 1
    assert list(np.frombuffer(received[0], dtype=np.int16)) == [10, 30]


def test_consumer_loop_without_transform_passes_through():
    """Backends that produce mono directly (arecord) set no transform."""
    cap = AudioCapture.__new__(AudioCapture)
    cap._running = True
    cap._audio_queue = queue.Queue()
    cap._consumer_transform = None

    received = []

    def cb(chunk):
        received.append(chunk)
        cap._running = False

    cap._callback = cb

    mono = np.array([7, 8, 9], dtype=np.int16).tobytes()
    cap._audio_queue.put(mono)
    _run_consumer_loop(cap)

    assert received == [mono]
