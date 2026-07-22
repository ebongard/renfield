#!/usr/bin/env python3
"""§2 Track A — cross-recording speaker-embedding separation eval (the go/no-go).

Sibling of ``run_fingerprint_calibration.py`` (which reads relabeled meetings
from the DB). This variant runs on PUBLIC, non-sensitive audio + a ground-truth
speaker manifest — AMI Meeting Corpus (CC-BY 4.0, same participants recur across
a scenario series) as the authoritative gate, then a CC podcast as an in-the-wild
cross-check. It isolates the EMBEDDING question (does a person's ECAPA centroid
from recording A match their centroid from recording B, distinctly from other
people?) from diarization, by using the manifest's ground-truth segments instead
of running the pipeline's diarizer.

Embeddings come from the SAME ECAPA the meeting pipeline uses: the voice-server
``POST /api/voice/stt`` returns ``speaker_embedding`` (192-dim, the ONNX ``/stt``
space). A calibration on a different model would be a lie. Run this INSIDE the
cluster (a Job / pod) so ``--voice-url http://voice-server.voice:8080`` resolves.

Manifest (JSON) — reusable for AMI and podcasts alike:
    [
      {"recording": "ES2002a", "audio": "/data/ES2002a.wav",
       "segments": [{"speaker": "MEE068", "start_s": 12.4, "end_s": 15.1}, ...]},
      {"recording": "ES2002b", "audio": "/data/ES2002b.wav",
       "segments": [{"speaker": "MEE068", "start_s": 3.0, "end_s": 8.2}, ...]}
    ]
``speaker`` ids are GLOBAL (stable across recordings) — that is the ground truth
the eval keys on. AMI participant ids (e.g. ``MEE068``) already are; for a podcast
you label the recurring hosts consistently by hand.

GO/NO-GO (design D-A1): a person's centroid is pooled per (recording, speaker)
from their concatenated ground-truth audio (capped, like the live ECAPA path);
PASS iff ``margin = intra_p05 - inter_p95 >= --min-margin`` (default 0.05) with
``>= --min-pairs`` intra-person cross-recording pairs. PASS => build the rest of
Track A on the printed threshold; FAIL/INSUFFICIENT => attribution degrades to
unattributed (the design's escape hatch).

Usage (in-cluster):
    python bin/run_embedding_separation_eval.py \
        --manifest /data/ami_es_series.json \
        --voice-url http://voice-server.voice:8080 \
        [--max-seconds 30] [--min-margin 0.05] [--min-pairs 20] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from itertools import combinations

_SR = 16000  # voice-server /stt works in 16 kHz mono (the ECAPA front-end).


# --- separation math (mirrors run_fingerprint_calibration._evaluate) ----------

def _unit(vec):
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n > 1e-9 else None


def _mean_centroid(embeddings):
    units = [u for e in embeddings if (u := _unit(e)) is not None]
    if not units:
        return None
    dim = len(units[0])
    acc = [0.0] * dim
    for u in units:
        if len(u) == dim:
            for i, x in enumerate(u):
                acc[i] += x
    return _unit(acc)


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def evaluate(centroids, min_margin, min_pairs):
    """centroids: {speaker_id: [(recording_id, centroid), ...]}."""
    intra = [
        _cosine(c1, c2)
        for items in centroids.values()
        for (r1, c1), (r2, c2) in combinations(items, 2)
        if r1 != r2
    ]
    flat = [(sid, c) for sid, items in centroids.items() for (_r, c) in items]
    inter = [_cosine(c1, c2) for (s1, c1), (s2, c2) in combinations(flat, 2) if s1 != s2]
    intra.sort()
    inter.sort()
    result = {
        "speakers": len(centroids),
        "speakers_multi_recording": sum(
            1 for v in centroids.values() if len({r for r, _ in v}) >= 2
        ),
        "intra_pairs": len(intra),
        "inter_pairs": len(inter),
    }
    if len(intra) < min_pairs or not inter:
        result["verdict"] = "INSUFFICIENT"
        result["reason"] = (
            f"need >= {min_pairs} same-speaker cross-recording pairs (have "
            f"{len(intra)}) and >=1 inter pair (have {len(inter)})"
        )
        return result
    intra_p05, inter_p95 = _percentile(intra, 0.05), _percentile(inter, 0.95)
    margin = intra_p05 - inter_p95
    best_t, best_gap, eer = float("nan"), float("inf"), float("nan")
    for t in sorted(set(intra + inter)):
        far = sum(1 for v in inter if v >= t) / len(inter)
        frr = sum(1 for v in intra if v < t) / len(intra)
        if abs(far - frr) < best_gap:
            best_gap, best_t, eer = abs(far - frr), t, (far + frr) / 2
    result.update({
        "intra_mean": sum(intra) / len(intra), "intra_p05": intra_p05,
        "inter_mean": sum(inter) / len(inter), "inter_p95": inter_p95,
        "margin": margin, "suggested_threshold": best_t, "eer": eer,
        "verdict": "PASS" if margin >= min_margin else "FAIL",
        "criterion": f"margin(intra_p05 - inter_p95) >= {min_margin}",
    })
    return result


# --- audio + voice-server ECAPA (the network/IO half) -------------------------

def _pool_speaker_audio(audio_path, segments, max_seconds):
    """Concatenate a speaker's ground-truth segments into one capped 16 kHz mono
    clip (mirrors speaker_service.cap_clip — ECAPA gains nothing past ~30 s)."""
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)  # downmix to mono
    if sr != _SR:
        import librosa
        data = librosa.resample(data, orig_sr=sr, target_sr=_SR)
    cap = int(max_seconds * _SR)
    chunks, total = [], 0
    for seg in segments:
        a, b = int(seg["start_s"] * _SR), int(seg["end_s"] * _SR)
        clip = data[max(0, a):max(0, b)]
        if clip.size == 0:
            continue
        chunks.append(clip)
        total += clip.size
        if total >= cap:
            break
    if not chunks:
        return None
    return np.concatenate(chunks)[:cap]


def _embed_via_voice_server(pcm, voice_url):
    """POST 16 kHz mono PCM as a WAV to /api/voice/stt → its speaker_embedding."""
    import io

    import numpy as np
    import requests
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, pcm.astype(np.float32), _SR, format="WAV", subtype="PCM_16")
    buf.seek(0)
    resp = requests.post(
        f"{voice_url.rstrip('/')}/api/voice/stt",
        files={"audio": ("clip.wav", buf, "audio/wav")},
        timeout=120,
    )
    resp.raise_for_status()
    emb = resp.json().get("speaker_embedding")
    if not emb:
        raise RuntimeError("voice-server returned no speaker_embedding (ECAPA off?)")
    return emb


def build_centroids(manifest, voice_url, max_seconds, log):
    centroids = defaultdict(list)
    for rec in manifest:
        by_speaker = defaultdict(list)
        for seg in rec["segments"]:
            by_speaker[seg["speaker"]].append(seg)
        for speaker, segs in by_speaker.items():
            pcm = _pool_speaker_audio(rec["audio"], segs, max_seconds)
            if pcm is None:
                continue
            try:
                emb = _embed_via_voice_server(pcm, voice_url)
            except Exception as e:  # one bad clip must not sink the whole eval
                log(f"  ! {rec['recording']}/{speaker}: embed failed: {e}")
                continue
            c = _mean_centroid([emb])
            if c is not None:
                centroids[speaker].append((rec["recording"], c))
            log(f"  embedded {rec['recording']}/{speaker} ({len(segs)} segs)")
    return centroids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True, help="ground-truth JSON (see docstring)")
    ap.add_argument("--voice-url", default="http://voice-server.voice:8080")
    ap.add_argument("--max-seconds", type=float, default=30.0)
    ap.add_argument("--min-margin", type=float, default=0.05)
    ap.add_argument("--min-pairs", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    def log(msg):
        if not args.json:
            print(msg, file=sys.stderr)

    with open(args.manifest) as f:
        manifest = json.load(f)

    log(f"embedding {sum(len(r['segments']) for r in manifest)} segments "
        f"across {len(manifest)} recordings via {args.voice_url} ...")
    centroids = build_centroids(manifest, args.voice_url, args.max_seconds, log)
    result = evaluate(centroids, args.min_margin, args.min_pairs)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("=== §2 Track A embedding-separation eval ===")
        for k, v in result.items():
            print(f"  {k}: {round(v, 4) if isinstance(v, float) else v}")
        print(f"\nGO/NO-GO: {result['verdict']}")
    return 1 if result.get("verdict") == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
