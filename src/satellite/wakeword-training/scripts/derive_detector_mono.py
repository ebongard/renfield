#!/usr/bin/env python3
"""Turn a raw room-ambient capture into the exact mono the detector hears.

`bin/capture-room-ambient.sh` records RAW multi-channel audio, because that is
the only form from which every downstream representation can be reconstructed.
The wakeword detector, however, never sees those raw channels: the satellite
first collapses them to mono, and HOW it does that differs per satellite.

  beamforming: true   -> Delay-and-Sum over the 2 HAT mics (ReSpeaker 2-Mic)
  combine: select     -> keep one channel verbatim (XVF3800: its processed beam)
  otherwise           -> mean of all channels (plain ALSA-style downmix)

Training on the wrong collapse silently poisons the negative set. On a
beamforming satellite an ALSA mono downmix is a different signal than the one
the model scores, so hard-negatives built from it teach the model to reject
audio it will never encounter.

This script reads the capture's JSON sidecar (written by the capture script) and
replays the satellite's own collapse, using the satellite's own beamformer code
rather than a reimplementation.

Usage:
    python derive_detector_mono.py data/wakeword-ambient/satellite-kinderbad/*.wav
    python derive_detector_mono.py --out-dir /work/ambient <wav> [<wav> ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

# The satellite package holds the authoritative beamformer. Load that ONE module
# by path rather than importing the package: `renfield_satellite.audio` pulls in
# pyaudio/mpv capture and playback, which are absent on a workstation and emit
# hardware warnings. Reimplementing Delay-and-Sum here would be worse — it would
# silently drift from the DSP the satellite actually runs.
_BEAMFORMER_PATH = (Path(__file__).resolve().parents[3]
                    / "satellite/renfield_satellite/audio/beamformer.py")


def _load_beamformer():
    import importlib.util

    if not _BEAMFORMER_PATH.exists():
        raise SystemExit(f"beamformer not found at {_BEAMFORMER_PATH}")
    spec = importlib.util.spec_from_file_location("_beamformer", _BEAMFORMER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BeamformerDAS


def load_sidecar(wav_path: Path) -> dict:
    sidecar = wav_path.with_suffix(".json")
    if not sidecar.exists():
        raise SystemExit(
            f"{wav_path.name}: no sidecar {sidecar.name}. The collapse mode cannot be "
            "guessed — re-capture with bin/capture-room-ambient.sh."
        )
    return json.loads(sidecar.read_text())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path.name}: expected 16-bit PCM")
        rate = w.getframerate()
        channels = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.reshape(-1, channels), rate


def collapse(audio: np.ndarray, cfg: dict, rate: int) -> tuple[np.ndarray, str]:
    """Return (mono int16, human-readable description of the collapse applied)."""
    channels = audio.shape[1]
    if channels == 1:
        return audio[:, 0], "passthrough (already mono)"

    if cfg.get("beamforming"):
        BeamformerDAS = _load_beamformer()

        if channels != 2:
            raise SystemExit(
                f"beamforming needs exactly 2 channels, capture has {channels}"
            )
        spacing = float(cfg.get("mic_spacing") or 0.058)
        bf = BeamformerDAS(mic_spacing=spacing, sample_rate=rate)
        # process_int16 wants (2, samples).
        mono = bf.process_int16(audio.T.copy())
        return mono, f"delay-and-sum beamform (mic_spacing={spacing} m)"

    if cfg.get("combine") == "select":
        idx = int(cfg.get("select_channel") or 0)
        if idx >= channels:
            raise SystemExit(f"select_channel={idx} but capture has {channels} channels")
        return audio[:, idx], f"select channel {idx}"

    mono = audio.mean(axis=1)
    return mono.astype(np.int16), f"mean downmix of {channels} channels"


def write_wav(path: Path, mono: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(mono.astype(np.int16).tobytes())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wavs", nargs="+", type=Path, help="raw captures to convert")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="destination (default: alongside the input, '-mono' suffix)")
    args = ap.parse_args()

    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    for wav in args.wavs:
        if wav.name.endswith("-mono.wav"):
            print(f"  skip {wav.name} (already derived)")
            continue
        sidecar = load_sidecar(wav)
        audio, rate = read_wav(wav)
        cfg = sidecar.get("audio_config", {})
        mono, how = collapse(audio, cfg, rate)

        dest_dir = args.out_dir or wav.parent
        dest = dest_dir / (wav.stem + "-mono.wav")
        write_wav(dest, mono, rate)

        rms = float(np.sqrt((mono.astype(np.float64) ** 2).mean()))
        dbfs = 20 * np.log10(max(rms, 1e-9) / 32768.0)
        print(f"  {wav.name}\n"
              f"    -> {dest}\n"
              f"       {how}; {len(mono) / rate:.0f}s, RMS {rms:.1f} ({dbfs:.1f} dBFS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
