#!/usr/bin/env python
"""Score wav files exactly as the satellite does: 1280-sample (80 ms) streaming
chunks through openwakeword.Model.predict. Reports peak score, detection EVENTS
(threshold crossings with a refractory window, so one wake is not counted as
many frames), and their timestamps.

Usage: score_wav.py <model.onnx> <wav> [<wav> ...]
"""
import os, sys, wave
import numpy as np
from openwakeword.model import Model

REFRACTORY_S = 2.0   # a real wake blocks re-detection for ~this long
CHUNK = 1280         # 80 ms @ 16 kHz, the satellite's chunk_size

model_path = sys.argv[1]
wavs = sys.argv[2:]
m = Model(wakeword_models=[model_path], inference_framework="onnx")
key = list(m.models.keys())[0]
print(f"== model {os.path.basename(model_path)} (key {key}) ==")

for path in wavs:
    with wave.open(path) as w:
        rate = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    m.reset()
    scores = []
    for i in range(0, len(a) - CHUNK, CHUNK):
        scores.append(m.predict(a[i:i + CHUNK]).get(key, 0.0))
    scores = np.asarray(scores, dtype=np.float32)
    dur_h = len(a) / rate / 3600.0
    step = CHUNK / rate

    print(f"\n  {os.path.basename(path)}  {len(a)/rate:.0f}s  peak {scores.max():.3f}")
    for thr in (0.5, 0.7, 0.8, 0.9):
        events, last = [], -1e9
        for idx in np.flatnonzero(scores >= thr):
            t = idx * step
            if t - last >= REFRACTORY_S:
                events.append(t)
                last = t
        rate_h = len(events) / dur_h if dur_h else 0.0
        stamps = " ".join(f"{t:.0f}s" for t in events[:12])
        more = f" (+{len(events)-12})" if len(events) > 12 else ""
        print(f"    thr {thr}: {len(events):3d} events -> {rate_h:6.1f}/h   {stamps}{more}")
