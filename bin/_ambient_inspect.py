#!/usr/bin/env python3
"""On-satellite helper for bin/capture-room-ambient.sh.

Runs ON the satellite (shipped there base64-encoded, so no quoting layers).
Two modes:

  probe          print the live audio config + capture-gain provenance as JSON
  gate <wav>     measure a capture and report per-channel level statistics

Kept dependency-light on purpose: `probe` uses only the stdlib so it also runs
on a satellite whose venv is broken, and `gate` needs numpy only (present in
the satellite venv).
"""
import json
import re
import subprocess
import sys
import wave

CONFIG_PATH = "/opt/renfield-satellite/config/satellite.yaml"

# Capture-chain controls worth recording. A negative captured at a different
# gain than the deployment gain is worthless: the model must be trained on what
# it will actually hear (renfield_de v4 lesson).
MIXER_CONTROLS = (
    "PGA",
    "Capture",
    "ADC HPF Cut-off",
    "ALC Max Gain",
    "Left AGC Target level",
    "Right AGC Target level",
)


def probe() -> dict:
    try:
        cfg = open(CONFIG_PATH).read()
    except OSError as exc:
        return {"error": f"cannot read {CONFIG_PATH}: {exc}"}

    def scalar(key, default=""):
        m = re.search(r'^\s{2}%s:\s*"?([^"#\n]+?)"?\s*(?:#.*)?$' % key, cfg, re.M)
        return m.group(1).strip() if m else default

    beam = re.search(r"beamforming:\s*\n\s+enabled:\s*(\w+)", cfg)
    # mic_spacing is nested under `beamforming:`, so it sits deeper than the
    # 2-space scalars above and needs its own indent-agnostic match.
    spacing = re.search(r"^\s+mic_spacing:\s*([0-9.]+)", cfg, re.M)
    return {
        "device": scalar("device", "default"),
        "channels": int(scalar("channels", "1") or 1),
        "sample_rate": int(scalar("sample_rate", "16000") or 16000),
        "beamforming": (beam.group(1).lower() == "true") if beam else False,
        "combine": scalar("combine", ""),
        "select_channel": scalar("select_channel", ""),
        "mic_spacing": spacing.group(1) if spacing else "",
        "mixer": read_mixer(),
    }


def read_mixer() -> dict:
    out = {}
    for ctl in MIXER_CONTROLS:
        try:
            res = subprocess.run(
                ["amixer", "-c", "0", "sget", ctl],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode != 0:
            continue
        vals = [
            line.strip() for line in res.stdout.splitlines()
            if "Item0" in line or "Front Left" in line or "Mono:" in line
        ]
        if vals:
            out[ctl] = " | ".join(vals)
    return out


# Read in blocks. A 45-minute stereo capture is ~173 MB on disk and ~691 MB as
# float64 — the satellites are Pi Zero 2 W with 512 MB RAM, so loading the whole
# file OOMs exactly at the capture length the commissioning doc recommends.
# Every statistic below is a streaming accumulation.
_GATE_BLOCK_FRAMES = 1 << 19  # ~0.5 M frames, a few MB per block


def gate(path: str) -> dict:
    import numpy as np

    with wave.open(path) as w:
        channels = w.getnchannels()
        rate = w.getframerate()
        frames = w.getnframes()

        total = np.zeros(channels, dtype=np.float64)      # sum(x)
        total_sq = np.zeros(channels, dtype=np.float64)   # sum(x^2)
        peak = np.zeros(channels, dtype=np.float64)
        clipped = np.zeros(channels, dtype=np.int64)
        counted = 0

        while True:
            raw = w.readframes(_GATE_BLOCK_FRAMES)
            if not raw:
                break
            block = np.frombuffer(raw, dtype=np.int16).reshape(-1, channels)
            block_f = block.astype(np.float64)
            total += block_f.sum(axis=0)
            total_sq += (block_f ** 2).sum(axis=0)
            abs_block = np.abs(block_f)
            peak = np.maximum(peak, abs_block.max(axis=0))
            clipped += (abs_block >= 32767).sum(axis=0)
            counted += block.shape[0]

    if counted == 0:
        return {"error": f"{path}: no audio frames"}

    full_scale = 32768.0
    per_channel = []
    for c in range(channels):
        rms = float(np.sqrt(total_sq[c] / counted))
        per_channel.append({
            "channel": c,
            "dc_offset": round(float(total[c] / counted), 1),
            "rms": round(rms, 1),
            "peak": round(float(peak[c]), 1),
            "rms_dbfs": round(float(20 * np.log10(max(rms, 1e-9) / full_scale)), 1),
            "clipped_fraction": round(float(clipped[c]) / counted, 6),
            "crest": round(float(peak[c]) / rms, 1) if rms > 0 else None,
        })

    return {
        "duration_seconds": round(counted / float(rate), 1),
        "channels": channels,
        "sample_rate": rate,
        "per_channel": per_channel,
        "mixer": read_mixer(),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _ambient_inspect.py probe | gate <wav>", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    if mode == "probe":
        result = probe()
    elif mode == "gate":
        if len(sys.argv) < 3:
            print("gate needs a wav path", file=sys.stderr)
            return 2
        result = gate(sys.argv[2])
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    print("AMBIENT_JSON:" + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
