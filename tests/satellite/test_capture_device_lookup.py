"""
Microphone lookup: a configured device that is missing is not replaced.

Fitnessraum, 2026-11-09: with the USB XVF3800 unplugged, the ALSA "default"
PCM vanished from PortAudio's list and the only input left was "pulse" -- the
null PCM that /etc/asound.conf installs so PortAudio cannot hang on a missing
PulseAudio server. ``_find_pyaudio_device`` fell back to PortAudio's default
input, i.e. the null PCM. Its reads return at once, so the capture threads ran
at 100 % and 64 % CPU for days on fabricated audio while the satellite looked
healthy.
"""

import pytest
from renfield_satellite.audio.capture import AudioCapture


class _FakePyAudio:
    def __init__(self, inputs, default=None):
        self._devices = [
            {"index": i, "name": name, "maxInputChannels": 2} for i, name in enumerate(inputs)
        ]
        self._default = default

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, i):
        return self._devices[i]

    def get_default_input_device_info(self):
        if self._default is None:
            raise OSError("No Default Input Device Available")
        return self._devices[self._default]


def _capture(device_name, pa):
    cap = AudioCapture.__new__(AudioCapture)
    cap.device_name = device_name
    cap._pyaudio = pa
    return cap


@pytest.mark.satellite
class TestFindPyAudioDevice:
    def test_the_unplugged_xvf3800_finds_no_device_not_the_null_pcm(self):
        pa = _FakePyAudio(["pulse"], default=0)
        assert _capture("default", pa)._find_pyaudio_device() is None

    def test_a_present_default_pcm_is_found_exactly(self):
        pa = _FakePyAudio(["sysdefault", "pulse", "default"], default=1)
        assert _capture("default", pa)._find_pyaudio_device() == 2

    def test_a_substring_match_still_works(self):
        pa = _FakePyAudio(["pulse", "reSpeaker XVF3800 4-Mic Array: USB Audio (hw:1,0)"], default=0)
        assert _capture("xvf3800", pa)._find_pyaudio_device() == 1

    def test_without_a_configured_device_the_default_input_is_used(self):
        pa = _FakePyAudio(["pulse", "mic"], default=1)
        assert _capture(None, pa)._find_pyaudio_device() == 1

    def test_without_a_configured_device_and_no_default_there_is_none(self):
        pa = _FakePyAudio(["pulse"], default=None)
        assert _capture(None, pa)._find_pyaudio_device() is None
