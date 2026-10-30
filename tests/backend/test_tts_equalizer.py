"""
TTS sound profiles (`ha_glue/services/tts_equalizer.py`).

Background: the household voice carries about 80 % of its energy in 80-300 Hz
and about 2 % above 1 kHz. Over a hi-fi system the same file that sounds
balanced on the small satellite speaker sounds flat and muffled. These tests
MEASURE the effect instead of merely checking that the function runs through.
"""

import io
import wave

import numpy as np
import pytest

from ha_glue.services.tts_equalizer import (
    TTS_EQ_PROFILES,
    apply_profile,
    is_known_profile,
)

SR = 22050


def _wav(samples: np.ndarray, sample_rate: int = SR, channels: int = 1, sampwidth: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def _read(wav_bytes: bytes):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        params = w.getparams()
        data = np.frombuffer(w.readframes(params.nframes), dtype="<i2").astype(np.float64)
    return params, data


def _voice_like(seconds: float = 2.0, sample_rate: int = SR) -> np.ndarray:
    """Bass-heavy like the real voice: a strong fundamental, weak presence."""
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    x = (
        1.00 * np.sin(2 * np.pi * 60 * t)      # below the high-pass
        + 1.00 * np.sin(2 * np.pi * 180 * t)   # the fundamental range
        + 0.05 * np.sin(2 * np.pi * 3500 * t)  # presence
    )
    return (x / np.abs(x).max() * 20000).astype("<i2")


def _band_share(data: np.ndarray, sample_rate: int, lo: float, hi: float) -> float:
    spectrum = np.abs(np.fft.rfft(data)) ** 2
    freqs = np.fft.rfftfreq(len(data), 1 / sample_rate)
    return float(spectrum[(freqs >= lo) & (freqs < hi)].sum() / spectrum.sum())


@pytest.mark.unit
def test_hifi_speech_verschiebt_die_energie_messbar():
    """The core claim: less bass, more presence -- measured, not asserted."""
    original = _wav(_voice_like())
    _, before = _read(original)
    _, after = _read(apply_profile(original, "hifi_speech"))

    assert _band_share(after, SR, 0, 100) < _band_share(before, SR, 0, 100) * 0.5
    assert _band_share(after, SR, 3000, 4000) > _band_share(before, SR, 3000, 4000) * 3


@pytest.mark.unit
def test_frequenzgang_des_shelfs_ist_festgenagelt():
    """The band-share test above does not see every coefficient error: an a1
    with the wrong sign passes it (a review finding, evidenced by a mutant).
    Hence the frequency response itself here."""
    from scipy.signal import freqz

    from ha_glue.services.tts_equalizer import _high_shelf

    b, a = _high_shelf(SR, 2500.0, 7.0)
    _, h = freqz(b, a, worN=[500, 2500, 5000, 10000], fs=SR)
    gains_db = 20 * np.log10(np.abs(h))

    assert gains_db == pytest.approx([0.0, 3.5, 6.7, 7.0], abs=0.3)
    assert np.all(np.abs(np.roots(a)) < 1), "stabil: alle Pole im Einheitskreis"


@pytest.mark.unit
def test_hochpass_nimmt_den_bass_unter_dem_grundton_weg():
    from scipy.signal import butter, sosfreqz

    profile = TTS_EQ_PROFILES["hifi_speech"]
    sos = butter(2, profile.highpass_hz, "highpass", fs=SR, output="sos")
    _, h = sosfreqz(sos, worN=[50, 120, 1000], fs=SR)
    gains_db = 20 * np.log10(np.abs(h))

    assert gains_db[0] < -12
    assert gains_db[1] == pytest.approx(-3.0, abs=0.3)
    assert gains_db[2] == pytest.approx(0.0, abs=0.1)


@pytest.mark.unit
def test_leise_eingabe_bleibt_leise():
    """Do not pull it up to full scale -- the level belongs to `tts_volume`."""
    quiet = (_voice_like().astype(np.float64) * 0.05).astype("<i2")
    _, before = _read(_wav(quiet))
    _, after = _read(apply_profile(_wav(quiet), "hifi_speech"))

    assert np.abs(after).max() == pytest.approx(np.abs(before).max(), rel=0.02)


@pytest.mark.unit
def test_format_und_laenge_bleiben_erhalten():
    original = _wav(_voice_like())
    before, _ = _read(original)
    after, _ = _read(apply_profile(original, "hifi_speech"))

    assert (after.nchannels, after.sampwidth, after.framerate, after.nframes) == (
        before.nchannels, before.sampwidth, before.framerate, before.nframes,
    )


@pytest.mark.unit
def test_vollausgesteuerte_eingabe_wird_bei_minus_1_dbfs_gedeckelt():
    """The shelf boosts -- without a ceiling a loud answer would clip. That is
    how the real voice arrives: Piper delivers peaks at about 0 dBFS."""
    loud = (_voice_like().astype(np.float64) / 20000 * 32767).astype("<i2")
    _, after = _read(apply_profile(_wav(loud), "hifi_speech"))

    ceiling = 32767 * 10 ** (-1 / 20)
    assert np.abs(after).max() == pytest.approx(ceiling, rel=0.001)


@pytest.mark.unit
def test_pegel_der_eingabe_bleibt_erhalten():
    """Below the ceiling the result keeps the input's peak."""
    _, before = _read(_wav(_voice_like()))
    _, after = _read(apply_profile(_wav(_voice_like()), "hifi_speech"))

    assert np.abs(after).max() == pytest.approx(np.abs(before).max(), rel=0.001)


@pytest.mark.unit
def test_stereo_wird_kanalweise_gefiltert():
    left = _voice_like()
    right = np.zeros_like(left)
    interleaved = np.column_stack([left, right]).astype("<i2").ravel()

    params, after = _read(apply_profile(_wav(interleaved, channels=2), "hifi_speech"))

    assert params.nchannels == 2
    assert np.abs(after[1::2]).max() <= 1, "der stille Kanal bleibt still — kein Uebersprechen"
    assert np.abs(after[0::2]).max() > 1000


@pytest.mark.unit
@pytest.mark.parametrize("profile", [None, ""])
def test_ohne_profil_byte_identisch(profile):
    original = _wav(_voice_like())
    assert apply_profile(original, profile) is original


@pytest.mark.unit
def test_unbekanntes_profil_spielt_unbearbeitet():
    original = _wav(_voice_like())
    assert apply_profile(original, "gibt_es_nicht") == original


@pytest.mark.unit
@pytest.mark.parametrize("payload", [b"", b"kein wav", b"RIFF\x00\x00\x00\x00WAVEkaputt"])
def test_kaputtes_audio_kostet_nie_die_antwort(payload):
    """Fail-safe: an equalisation that failed returns the input."""
    assert apply_profile(payload, "hifi_speech") == payload


@pytest.mark.unit
def test_nicht_16_bit_wird_uebersprungen():
    eight_bit = _wav(np.full(1000, 128, dtype=np.uint8), sampwidth=1)
    assert apply_profile(eight_bit, "hifi_speech") == eight_bit


@pytest.mark.unit
def test_stille_bleibt_unveraendert():
    """A peak of 0 -- the normalisation must not divide by zero."""
    silence = _wav(np.zeros(SR, dtype="<i2"))
    assert apply_profile(silence, "hifi_speech") == silence


@pytest.mark.unit
def test_telefonie_abtastrate_ueberspringt_den_shelf_statt_zu_scheitern():
    """At a 4 kHz sample rate the shelf (2.5 kHz) lies above Nyquist."""
    low_rate = _wav(_voice_like(sample_rate=4000), sample_rate=4000)
    params, after = _read(apply_profile(low_rate, "hifi_speech"))

    assert params.framerate == 4000
    assert np.isfinite(after).all()


@pytest.mark.unit
def test_is_known_profile():
    assert is_known_profile(None)
    assert is_known_profile("hifi_speech")
    assert not is_known_profile("gibt_es_nicht")
    assert not is_known_profile("")
    assert set(TTS_EQ_PROFILES) == {"hifi_speech"}
