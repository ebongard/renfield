"""
TTS-Klangprofile (`ha_glue/services/tts_equalizer.py`).

Hintergrund: die Haushaltsstimme traegt ~80 % ihrer Energie in 80-300 Hz und ~2 %
ueber 1 kHz. Ueber eine Anlage klingt dieselbe Datei flach und dumpf, die auf dem
kleinen Satelliten-Lautsprecher ausgewogen wirkt. Die Tests MESSEN die Wirkung
statt nur zu pruefen, dass die Funktion durchlaeuft.
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
    """Basslastig wie die echte Stimme: kraeftiger Grundton, schwache Praesenz."""
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    x = (
        1.00 * np.sin(2 * np.pi * 60 * t)      # unter dem Hochpass
        + 1.00 * np.sin(2 * np.pi * 180 * t)   # Grundtonbereich
        + 0.05 * np.sin(2 * np.pi * 3500 * t)  # Praesenz
    )
    return (x / np.abs(x).max() * 20000).astype("<i2")


def _band_share(data: np.ndarray, sample_rate: int, lo: float, hi: float) -> float:
    spectrum = np.abs(np.fft.rfft(data)) ** 2
    freqs = np.fft.rfftfreq(len(data), 1 / sample_rate)
    return float(spectrum[(freqs >= lo) & (freqs < hi)].sum() / spectrum.sum())


@pytest.mark.unit
def test_hifi_speech_verschiebt_die_energie_messbar():
    """Die Kernaussage: weniger Bass, mehr Praesenz — gemessen, nicht behauptet."""
    original = _wav(_voice_like())
    _, before = _read(original)
    _, after = _read(apply_profile(original, "hifi_speech"))

    assert _band_share(after, SR, 0, 100) < _band_share(before, SR, 0, 100) * 0.5
    assert _band_share(after, SR, 3000, 4000) > _band_share(before, SR, 3000, 4000) * 3


@pytest.mark.unit
def test_frequenzgang_des_shelfs_ist_festgenagelt():
    """Der Bandanteil-Test oben sieht nicht jeden Koeffizientenfehler: ein
    vorzeichenverkehrtes a1 besteht ihn (Review-Befund, per Mutante belegt).
    Deshalb hier der Frequenzgang selbst."""
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
    """Nicht auf Vollausschlag hochziehen — der Pegel gehoert `tts_volume`."""
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
    """Der Shelf verstaerkt — ohne Deckel wuerde eine laute Antwort uebersteuern.
    So kommt die echte Stimme an: Piper liefert Spitzen bei ~0 dBFS."""
    loud = (_voice_like().astype(np.float64) / 20000 * 32767).astype("<i2")
    _, after = _read(apply_profile(_wav(loud), "hifi_speech"))

    ceiling = 32767 * 10 ** (-1 / 20)
    assert np.abs(after).max() == pytest.approx(ceiling, rel=0.001)


@pytest.mark.unit
def test_pegel_der_eingabe_bleibt_erhalten():
    """Unterhalb des Deckels behaelt das Ergebnis die Spitze der Eingabe."""
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
    """Fail-safe: eine gescheiterte Entzerrung gibt die Eingabe zurueck."""
    assert apply_profile(payload, "hifi_speech") == payload


@pytest.mark.unit
def test_nicht_16_bit_wird_uebersprungen():
    eight_bit = _wav(np.full(1000, 128, dtype=np.uint8), sampwidth=1)
    assert apply_profile(eight_bit, "hifi_speech") == eight_bit


@pytest.mark.unit
def test_stille_bleibt_unveraendert():
    """Spitze 0 — die Normalisierung darf nicht durch null teilen."""
    silence = _wav(np.zeros(SR, dtype="<i2"))
    assert apply_profile(silence, "hifi_speech") == silence


@pytest.mark.unit
def test_telefonie_abtastrate_ueberspringt_den_shelf_statt_zu_scheitern():
    """Bei 4 kHz Abtastrate liegt der Shelf (2,5 kHz) ueber Nyquist."""
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
