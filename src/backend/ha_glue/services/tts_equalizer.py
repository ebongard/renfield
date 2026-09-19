"""
TTS sound profiles for room output devices.

A spoken answer is synthesized once and played on very different speakers. The
household voice (Piper ``de_DE-thorsten-high``, 22.05 kHz mono) carries ~80 % of
its energy between 80 and 300 Hz and ~2 % above 1 kHz. A satellite's small
speaker cannot reproduce that low end, so it sounds balanced there; a hi-fi
system plays it in full and the same file sounds flat and muffled.

A profile is a NAME, not a set of knobs: the values below were chosen by ear
against measurements on the real system (2026-09-20 listening test, sample 2),
and a per-device parameter UI would have no such basis. A new profile is one
entry here plus one i18n string — no migration.

``apply_profile`` never raises and never returns less than it was given: an
equalizer that fails must not cost the user their answer.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

import numpy as np
from loguru import logger

PEAK_TARGET = 10 ** (-1.0 / 20)  # -1 dBFS ceiling: the shelf adds gain, so re-level


@dataclass(frozen=True)
class EqProfile:
    """One filter chain. Frequencies in Hz, gains in dB."""

    highpass_hz: float
    shelf_hz: float
    shelf_gain_db: float


TTS_EQ_PROFILES: dict[str, EqProfile] = {
    # High-pass under the voice's fundamental to take the boom out, then a high
    # shelf to bring back the 2-5 kHz presence range that carries intelligibility.
    "hifi_speech": EqProfile(highpass_hz=120.0, shelf_hz=2500.0, shelf_gain_db=7.0),
}


def is_known_profile(name: str | None) -> bool:
    """``None`` (= off) is valid; any other value must name a profile."""
    return name is None or name in TTS_EQ_PROFILES


def _high_shelf(sample_rate: int, f0: float, gain_db: float) -> tuple[np.ndarray, np.ndarray]:
    """RBJ audio-EQ-cookbook high shelf, shelf slope S = 1."""
    amp = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / sample_rate
    cos_w0, sin_w0 = np.cos(w0), np.sin(w0)
    alpha = sin_w0 / 2 * np.sqrt(2)
    sqrt_amp = np.sqrt(amp)
    b = np.array([
        amp * ((amp + 1) + (amp - 1) * cos_w0 + 2 * sqrt_amp * alpha),
        -2 * amp * ((amp - 1) + (amp + 1) * cos_w0),
        amp * ((amp + 1) + (amp - 1) * cos_w0 - 2 * sqrt_amp * alpha),
    ])
    a = np.array([
        (amp + 1) - (amp - 1) * cos_w0 + 2 * sqrt_amp * alpha,
        2 * ((amp - 1) - (amp + 1) * cos_w0),
        (amp + 1) - (amp - 1) * cos_w0 - 2 * sqrt_amp * alpha,
    ])
    return b / a[0], a / a[0]


def _equalize(samples: np.ndarray, sample_rate: int, profile: EqProfile) -> np.ndarray:
    from scipy import signal

    nyquist = sample_rate / 2
    out = samples
    if profile.highpass_hz < nyquist:
        sos = signal.butter(2, profile.highpass_hz, "highpass", fs=sample_rate, output="sos")
        out = signal.sosfilt(sos, out, axis=0)
    # A shelf at or above Nyquist has nothing to act on (8 kHz telephony audio).
    if profile.shelf_hz < nyquist:
        b, a = _high_shelf(sample_rate, profile.shelf_hz, profile.shelf_gain_db)
        out = signal.lfilter(b, a, out, axis=0)
    return out


def apply_profile(wav_bytes: bytes, profile_name: str | None) -> bytes:
    """Return ``wav_bytes`` equalized with the named profile.

    Sample rate, channel count and length are preserved. Returns the input
    unchanged when the profile is ``None`` or unknown, when the audio is not
    16-bit PCM WAV, or when anything at all goes wrong.

    CPU-bound — call it through ``asyncio.to_thread`` from async code.
    """
    if not profile_name or not wav_bytes:
        return wav_bytes
    profile = TTS_EQ_PROFILES.get(profile_name)
    if profile is None:
        logger.warning(f"Unknown TTS sound profile {profile_name!r} — playing unprocessed")
        return wav_bytes

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
            params = reader.getparams()
            frames = reader.readframes(params.nframes)
        if params.sampwidth != 2 or params.comptype != "NONE":
            logger.warning(
                f"TTS sound profile skipped: expected 16-bit PCM, got "
                f"sampwidth={params.sampwidth} comptype={params.comptype}"
            )
            return wav_bytes

        samples = np.frombuffer(frames, dtype="<i2").astype(np.float64)
        if samples.size == 0:
            return wav_bytes
        # Interleaved channels → (frames, channels) so each channel is filtered
        # as its own stream.
        samples = samples.reshape(-1, params.nchannels)

        input_peak = float(np.abs(samples).max())
        processed = _equalize(samples, params.framerate, profile)

        peak = float(np.abs(processed).max())
        if not np.isfinite(peak) or peak <= 0.0:
            return wav_bytes
        # Bring the result back to the INPUT's peak, capped at -1 dBFS. Scaling to
        # a fixed target instead would turn a quiet utterance up to full scale and
        # make the output level vary per answer, against the device's tts_volume.
        processed = processed / peak * min(input_peak, 32767.0 * PEAK_TARGET)

        out = io.BytesIO()
        with wave.open(out, "wb") as writer:
            writer.setnchannels(params.nchannels)
            writer.setsampwidth(2)
            writer.setframerate(params.framerate)
            writer.writeframes(np.round(processed).astype("<i2").tobytes())
        return out.getvalue()

    except Exception as e:  # noqa: BLE001 — fail-safe by design, see module docstring
        logger.warning(f"TTS sound profile {profile_name!r} failed — playing unprocessed: {e}")
        return wav_bytes
