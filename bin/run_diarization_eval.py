#!/usr/bin/env python3
"""Diarization spike / eval harness — meeting transcription §2, spike T1.

Persistent eval harness (eng-review 2026-07-06, D9): the spike IS this script,
and it stays as the regression anchor for every later whisper/pyannote/threshold
change. Gates live in tests/eval/diarization/gates.yaml and were FIXED BEFORE
the first measurement — do not tune them to fit a run. (Hardened fail-closed by
the PR #924 review, still before any measurement — see the gates.yaml header.)

Subcommands
-----------
generate-fixture   Synthesize a multi-speaker meeting wav + reference.json from
                   a script file (macOS `say` or piper voices). Privacy-clean:
                   synthetic voices, placeholder names — committable.
run                Diarize + transcribe + align + (optionally) embed one
                   audio/reference pair. Writes a metrics JSON per run.
probe-live-stt     Latency probe against a running voice-server. Run once for a
                   baseline, once DURING a `run`, feed both to `report`.
report             Evaluate metrics JSON(s) against the gates -> PASS/FAIL.
                   Verdict is per candidate: overall PASS needs at least ONE
                   metrics file passing every hard gate AND a passing live
                   probe pair. Missing hard-gate metrics FAIL (fail-closed).

Typical spike sequence (on a CUDA host, e.g. inside the voice-server image —
see tests/eval/diarization/README.md):

  python bin/run_diarization_eval.py run \
      --audio tests/eval/diarization/fixtures/meeting_synthetic_de.wav \
      --reference tests/eval/diarization/fixtures/meeting_synthetic_de.reference.json \
      --whisper-model large-v3 --ecapa-onnx /models/ecapa.onnx \
      --out /tmp/metrics-large-v3.json
  python bin/run_diarization_eval.py report /tmp/metrics-*.json \
      --gates tests/eval/diarization/gates.yaml \
      --live-baseline /tmp/live-baseline.json --live-during /tmp/live-during.json

Heavy deps (pyannote.audio, faster-whisper, torch, speechbrain, onnxruntime)
are imported lazily inside `run` so that fixture generation and reporting work
on any machine.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Shared data model
#
#   reference.json / hypothesis segments:
#     {"sample_rate": 16000,
#      "segments": [{"speaker": "S1", "start": 0.0, "end": 3.2, "text": "..."}]}
#
# Hand-written references must use NON-OVERLAPPING segments: the frame scorer
# is single-label per 10 ms frame, so overlapping reference speech would be
# silently mis-scored (last segment wins). Overlap robustness is judged
# qualitatively on the attributed transcript, not by this scorer.
# --------------------------------------------------------------------------

FRAME_S = 0.010     # frame size for the frame-level diarization scoring
SNAP_WINDOW_S = 0.5  # uncovered ASR word snaps to a diarization turn this close
MERGE_GAP_S = 1.0    # consecutive same-speaker words merge across gaps up to this


@dataclass
class Segment:
    speaker: str
    start: float
    end: float
    text: str = ""


def load_segments(path: Path) -> list[Segment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Segment(s["speaker"], float(s["start"]), float(s["end"]), s.get("text", ""))
            for s in data["segments"]]


def total_speech(segments: list[Segment]) -> float:
    return sum(s.end - s.start for s in segments)


def _p95(values: list[float]) -> float | None:
    """Nearest-rank (ceil) 95th percentile — shared by the separation and the
    latency probe so the two copies cannot drift. The naive int()-1 variant
    underestimates (returns the minimum at n=2), which would bias BOTH gates
    in the permissive direction."""
    if not values:
        return None
    vals = sorted(values)
    return vals[min(len(vals) - 1, max(0, math.ceil(0.95 * len(vals)) - 1))]


# --------------------------------------------------------------------------
# generate-fixture
# --------------------------------------------------------------------------

def cmd_generate_fixture(args: argparse.Namespace) -> int:
    script_path = Path(args.script)
    out_wav = Path(args.out)
    out_ref = out_wav.parent / (out_wav.stem + ".reference.json")

    voice_map: dict[str, str] = {}
    for pair in (args.voices or "").split(","):
        if "=" in pair:
            name, voice = pair.split("=", 1)
            voice_map[name.strip()] = voice.strip()

    lines: list[tuple[str, str]] = []
    for raw in script_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        if ":" not in raw:
            print(f"skip malformed line (no 'Speaker:'): {raw!r}", file=sys.stderr)
            continue
        speaker, text = raw.split(":", 1)
        lines.append((speaker.strip(), text.strip()))

    speakers = sorted({s for s, _ in lines})
    missing = [s for s in speakers if s not in voice_map]
    if missing:
        print(f"ERROR: no voice mapped for speaker(s) {missing}. "
              f"Pass --voices 'Name=Voice,...'", file=sys.stderr)
        return 2

    sr = args.sample_rate
    audio: list[bytes] = []
    segments: list[Segment] = []
    cursor = 0.0
    gap = args.gap_s

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for i, (speaker, text) in enumerate(lines):
            seg_wav = tmp / f"seg{i:03d}.wav"
            if args.engine == "say":
                aiff = tmp / f"seg{i:03d}.aiff"
                subprocess.run(["say", "-v", voice_map[speaker], "-o", str(aiff), text],
                               check=True)
                subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{sr}",
                                "-c", "1", str(aiff), str(seg_wav)], check=True)
            else:  # piper: voice_map value = path to .onnx voice model
                raw_wav = tmp / f"seg{i:03d}.raw.wav"
                subprocess.run(["piper", "--model", voice_map[speaker],
                                "--output_file", str(raw_wav)],
                               input=text.encode(), check=True)
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_wav),
                                "-ar", str(sr), "-ac", "1", str(seg_wav)], check=True)

            pcm, dur = _read_wav_pcm16(seg_wav, sr)
            audio.append(b"\x00" * int(gap * sr) * 2)
            cursor += gap
            audio.append(pcm)
            segments.append(Segment(speaker, round(cursor, 3), round(cursor + dur, 3), text))
            cursor += dur

    _write_wav_pcm16(out_wav, b"".join(audio), sr)
    out_ref.write_text(json.dumps(
        {"sample_rate": sr, "engine": args.engine,
         "segments": [asdict(s) for s in segments]}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"fixture: {out_wav}  ({cursor:.1f}s, {len(speakers)} speakers, "
          f"{len(segments)} turns)\nreference: {out_ref}")
    return 0


def _read_wav_pcm16(path: Path, expect_sr: int) -> tuple[bytes, float]:
    import wave
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != expect_sr or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected mono PCM16 @{expect_sr}, "
                             f"got {w.getnchannels()}ch {w.getsampwidth() * 8}bit "
                             f"@{w.getframerate()}")
        frames = w.readframes(w.getnframes())
        return frames, w.getnframes() / expect_sr


def _write_wav_pcm16(path: Path, pcm: bytes, sr: int) -> None:
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


# --------------------------------------------------------------------------
# run — the actual pipeline under test
# --------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    audio_path = Path(args.audio)
    ref_segments = load_segments(Path(args.reference)) if args.reference else []

    waveform, sr = _load_audio_mono16k(audio_path)
    audio_len_s = len(waveform) / sr
    metrics: dict = {
        "audio": str(audio_path), "audio_stem": audio_path.stem,
        "audio_seconds": round(audio_len_s, 1),
        "whisper_model": args.whisper_model, "device": args.device,
        "diarization_model": args.diarization_model,
    }

    vram = _VramPoller()
    vram.start()
    torch_peak = _TorchPeak(args.device)

    # -- diarization (model load kept OUT of the timed inference window) -----
    t0 = time.monotonic()
    pipeline = _load_pyannote(args)
    t_load = time.monotonic() - t0

    t0 = time.monotonic()
    diar_segments = _diarize(pipeline, args, waveform, sr)
    t_diar = time.monotonic() - t0
    del pipeline
    _cuda_gc()
    metrics["t_diarization_s"] = round(t_diar, 1)
    metrics["hyp_speaker_count"] = len({s.speaker for s in diar_segments})
    if ref_segments:
        metrics["ref_speaker_count"] = len({s.speaker for s in ref_segments})

    # -- ASR with word timestamps ---------------------------------------------
    t0 = time.monotonic()
    model = _load_whisper(args)
    t_load += time.monotonic() - t0

    t0 = time.monotonic()
    words = _transcribe(model, args, audio_path)
    t_asr = time.monotonic() - t0
    del model
    _cuda_gc()
    metrics["t_asr_s"] = round(t_asr, 1)
    metrics["t_model_load_s"] = round(t_load, 1)

    # -- alignment: word -> speaker -------------------------------------------
    hyp_segments = _align_words(words, diar_segments)
    metrics["hyp_segments"] = [asdict(s) for s in hyp_segments]

    # -- scoring vs reference ----------------------------------------------------
    if ref_segments:
        metrics["diarization_scores"] = _score_frames(ref_segments, hyp_segments)
        der_xcheck = _try_pyannote_der(ref_segments, hyp_segments)
        if der_xcheck is not None:
            metrics["der_pyannote_metrics"] = round(der_xcheck, 4)
        if any(s.text for s in ref_segments):
            wer = _try_wer(" ".join(s.text for s in ref_segments),
                           " ".join(w[2] for w in words))
            if wer is not None:
                metrics["wer_sample"] = round(wer, 3)

    # -- per-cluster ECAPA separation (auto-match gate) --------------------------
    if args.ecapa_onnx:
        metrics["embedding_separation"] = _cluster_separation(
            args, waveform, sr, diar_segments)

    # Inference-only cost; model load/download excluded so runs are comparable
    # across cold/warm caches. Precision 2 so the gate compares the real value
    # (a 30.04 must not round to a passing 30.0).
    metrics["gpu_seconds_per_audio_minute"] = round(
        (t_diar + t_asr) / max(audio_len_s / 60.0, 1e-9), 2)
    vram.stop()
    metrics["gpu_peak_vram_mb"] = vram.max_mb          # whole-GPU, allocator-agnostic
    metrics["gpu_peak_vram_torch_mb"] = torch_peak.read()  # torch-only breakdown

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in metrics.items() if k != "hyp_segments"},
                     ensure_ascii=False, indent=2))
    print(f"\nmetrics written: {out}")
    return 0


def _load_audio_mono16k(path: Path):
    """Load any audio ffmpeg understands as float32 mono 16 kHz."""
    import numpy as np  # noqa: PLC0415
    if shutil.which("ffmpeg"):
        raw = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", str(path),
             "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
            check=True, capture_output=True).stdout
        # frombuffer view avoids a second full copy of hours-long audio; the
        # consumers (torch.from_numpy, slicing) only read it.
        return np.frombuffer(raw, dtype=np.float32), 16000
    pcm, _dur = _read_wav_pcm16(path, 16000)  # strict fallback: already-16k wav
    return (np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0), 16000


def _load_pyannote(args):
    import torch  # noqa: PLC0415
    from pyannote.audio import Pipeline  # noqa: PLC0415

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    pipeline = Pipeline.from_pretrained(args.diarization_model, use_auth_token=token)
    if pipeline is None:
        # pyannote returns None (not an exception) when the gated model's
        # license was never accepted or the token is missing/invalid.
        raise SystemExit(
            f"pyannote could not load {args.diarization_model!r}. Accept the model "
            "license on huggingface.co with the account behind HF_TOKEN, and export "
            "HF_TOKEN (or pre-populate the HF cache for offline hosts).")
    if args.device == "cuda":
        pipeline.to(torch.device("cuda"))
    return pipeline


def _diarize(pipeline, args, waveform, sr) -> list[Segment]:
    import numpy as np  # noqa: PLC0415
    import torch  # noqa: PLC0415

    tensor = torch.from_numpy(np.ascontiguousarray(waveform)).unsqueeze(0)
    kwargs = {}
    if args.num_speakers:
        kwargs["num_speakers"] = args.num_speakers
    annotation = pipeline({"waveform": tensor, "sample_rate": sr}, **kwargs)
    return [Segment(str(label), float(turn.start), float(turn.end))
            for turn, _track, label in annotation.itertracks(yield_label=True)]


def _load_whisper(args):
    from faster_whisper import WhisperModel  # noqa: PLC0415

    compute = "float16" if args.device == "cuda" else "int8"
    return WhisperModel(args.whisper_model, device=args.device, compute_type=compute)


def _transcribe(model, args, audio_path: Path) -> list[tuple[float, float, str]]:
    seg_iter, _info = model.transcribe(str(audio_path), language=args.language,
                                       word_timestamps=True, vad_filter=True)
    words: list[tuple[float, float, str]] = []
    for seg in seg_iter:
        for w in seg.words or []:
            words.append((float(w.start), float(w.end), w.word.strip()))
    return words


def _cuda_gc() -> None:
    """Release CUDA memory between pipeline stages: pyannote (torch), whisper
    (CTranslate2) and the ECAPA ONNX session each use their OWN allocator, so
    torch's cached blocks must actually be freed before the next stage or the
    stages stack up in VRAM."""
    import gc  # noqa: PLC0415
    gc.collect()
    try:
        import torch  # noqa: PLC0415
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def _align_words(words, diar_segments: list[Segment]) -> list[Segment]:
    """Assign each ASR word to the diarization turn with maximal time overlap.

    Two-pointer sweep over the time-sorted streams (ASR words arrive in time
    order; turns are sorted here) — O(W + D) instead of scanning every turn per
    word, which matters at the 4 h ceiling (~40k words x ~3k turns).
    Overlapping turns: max-overlap wins. Uncovered words snap to the nearest
    turn within SNAP_WINDOW_S (full scan — rare path), otherwise speaker 'UNK'.
    Consecutive same-speaker words merge across gaps up to MERGE_GAP_S.
    """
    segs = sorted(diar_segments, key=lambda d: d.start)
    hyp: list[Segment] = []
    j = 0
    for w0, w1, text in words:
        # Turns entirely before this word can never overlap a later word either.
        while j < len(segs) and segs[j].end < w0:
            j += 1
        best, best_ov = None, 0.0
        k = j
        while k < len(segs) and segs[k].start < w1:
            ov = min(w1, segs[k].end) - max(w0, segs[k].start)
            if ov > best_ov:
                best, best_ov = segs[k], ov
            k += 1
        if best is None and segs:
            near = min(segs, key=lambda d: min(abs(d.start - w1), abs(w0 - d.end)))
            if min(abs(near.start - w1), abs(w0 - near.end)) <= SNAP_WINDOW_S:
                best = near
        speaker = best.speaker if best else "UNK"
        if hyp and hyp[-1].speaker == speaker and w0 - hyp[-1].end <= MERGE_GAP_S:
            hyp[-1].end = w1
            hyp[-1].text = f"{hyp[-1].text} {text}".strip()
        else:
            hyp.append(Segment(speaker, w0, w1, text))
    return hyp


def _score_frames(ref: list[Segment], hyp: list[Segment]) -> dict:
    """Frame-level DER-style scoring with optimal (Hungarian) speaker mapping.

    attribution_error_rate is measured only on frames BOTH streams label —
    which is why the report additionally gates der_like and hyp_coverage: a
    hypothesis that labels almost nothing would otherwise score a perfect AER.
    """
    import numpy as np  # noqa: PLC0415
    from scipy.optimize import linear_sum_assignment  # noqa: PLC0415

    def fidx(t: float) -> int:
        return int(round(t / FRAME_S))

    end = max(max(s.end for s in ref), max((s.end for s in hyp), default=0.0))
    n = fidx(end) + 1
    ref_speakers = sorted({s.speaker for s in ref})
    # UNK means "unattributed", not a speaker — keeping it as a matrix column
    # would let Hungarian map a real reference speaker onto an empty column.
    hyp_speakers = sorted({s.speaker for s in hyp if s.speaker != "UNK"})
    ref_idx = {s: i for i, s in enumerate(ref_speakers)}
    hyp_idx = {s: i for i, s in enumerate(hyp_speakers)}

    ref_f = np.full(n, -1, dtype=np.int16)
    hyp_f = np.full(n, -1, dtype=np.int16)
    for s in ref:
        ref_f[fidx(s.start):fidx(s.end)] = ref_idx[s.speaker]
    for s in hyp:
        if s.speaker == "UNK":
            continue
        hyp_f[fidx(s.start):fidx(s.end)] = hyp_idx[s.speaker]

    # confusion matrix over frames where both streams see speech
    both = (ref_f >= 0) & (hyp_f >= 0)
    conf = np.zeros((len(ref_speakers), max(len(hyp_speakers), 1)), dtype=np.int64)
    if both.any():
        np.add.at(conf, (ref_f[both], hyp_f[both]), 1)
    rows, cols = linear_sum_assignment(-conf)
    mapping = {c: r for r, c in zip(rows.tolist(), cols.tolist()) if c < len(hyp_speakers)}

    correct = sum(int(conf[r, c]) for r, c in zip(rows, cols) if c < len(hyp_speakers))
    confused = int(both.sum()) - correct
    missed = int(((ref_f >= 0) & (hyp_f < 0)).sum())
    false_alarm = int(((ref_f < 0) & (hyp_f >= 0)).sum())
    ref_speech = int((ref_f >= 0).sum())

    return {
        "attribution_error_rate": round(confused / max(int(both.sum()), 1), 4),
        "der_like": round((missed + false_alarm + confused) / max(ref_speech, 1), 4),
        "missed_rate": round(missed / max(ref_speech, 1), 4),
        "false_alarm_rate": round(false_alarm / max(ref_speech, 1), 4),
        "hyp_coverage": round(int(both.sum()) / max(ref_speech, 1), 4),
        "speaker_mapping": {hyp_speakers[c]: ref_speakers[r]
                            for c, r in mapping.items()},
    }


def _try_pyannote_der(ref: list[Segment], hyp: list[Segment]):
    """Cross-check the hand-rolled scorer against pyannote.metrics DER when it
    is importable (it ships with pyannote.audio, i.e. in every `run` env).
    Disagreement between der_like and this value on the synthetic fixture
    means the decision instrument itself is buggy — investigate before
    trusting any gate verdict."""
    try:
        from pyannote.core import Annotation, Segment as PSegment  # noqa: PLC0415
        from pyannote.metrics.diarization import DiarizationErrorRate  # noqa: PLC0415
    except ImportError:
        return None
    ref_ann, hyp_ann = Annotation(), Annotation()
    for s in ref:
        ref_ann[PSegment(s.start, s.end)] = s.speaker
    for s in hyp:
        if s.speaker != "UNK":
            hyp_ann[PSegment(s.start, s.end)] = s.speaker
    return float(DiarizationErrorRate()(ref_ann, hyp_ann))


def _try_wer(ref_text: str, hyp_text: str):
    try:
        import jiwer  # noqa: PLC0415
    except ImportError:
        return None
    return float(jiwer.wer(ref_text.lower(), hyp_text.lower()))


def _cluster_separation(args, waveform, sr, diar_segments: list[Segment]) -> dict:
    """Per-cluster ECAPA embeddings in the voice-server ONNX space (D4/D12).

    Splits every cluster's speech into chunks, embeds each chunk, then reports
    same-cluster vs cross-cluster cosine statistics. The auto-match gate is
    separation = same_mean - cross_p95 (gates.yaml: auto_match_separation_min);
    the report treats it as unmeasured below the minimum pair counts.
    Requires the speechbrain feature pipeline — run inside the voice-server
    image (same preprocessing as production; verify parity once per the README
    checklist before trusting the numbers).
    """
    import numpy as np  # noqa: PLC0415
    import onnxruntime as ort  # noqa: PLC0415
    import torch  # noqa: PLC0415
    from speechbrain.lobes.features import Fbank  # noqa: PLC0415
    from speechbrain.processing.features import InputNormalization  # noqa: PLC0415

    fbank = Fbank(n_mels=80)
    norm = InputNormalization(norm_type="sentence", std_norm=False)
    sess = ort.InferenceSession(args.ecapa_onnx, providers=["CUDAExecutionProvider",
                                                            "CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name

    def embed(chunk: "np.ndarray") -> "np.ndarray":
        wav = torch.from_numpy(np.ascontiguousarray(chunk)).unsqueeze(0)
        feats = norm(fbank(wav), torch.ones(1))
        out = sess.run(None, {input_name: feats.numpy()})[0]
        vec = np.squeeze(out).astype(np.float32)
        return vec / (np.linalg.norm(vec) + 1e-9)

    chunk_s, min_s = args.embed_chunk_s, args.embed_min_s
    by_cluster: dict[str, list] = {}
    for seg in diar_segments:
        a, b = int(seg.start * sr), int(seg.end * sr)
        by_cluster.setdefault(seg.speaker, []).append(waveform[a:b])

    cluster_embs: dict[str, "np.ndarray"] = {}
    dropped: list[str] = []
    for speaker, pieces in by_cluster.items():
        speech = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
        n_chunks = int(len(speech) / (chunk_s * sr))
        embs = [embed(speech[i * chunk_s * sr:(i + 1) * chunk_s * sr])
                for i in range(n_chunks)]
        rest = speech[n_chunks * chunk_s * sr:]
        if len(rest) >= min_s * sr:
            embs.append(embed(rest))
        if embs:
            cluster_embs[speaker] = np.stack(embs)
        else:
            dropped.append(speaker)  # too little speech — visible, not silent

    same: list[float] = []
    cross: list[float] = []
    speakers = list(cluster_embs)
    for i, si in enumerate(speakers):
        ei = cluster_embs[si]
        sims = ei @ ei.T  # embeddings are L2-normalized -> dot == cosine
        iu = np.triu_indices(len(ei), k=1)
        same += sims[iu].tolist()
        for sj in speakers[i + 1:]:
            cross += (ei @ cluster_embs[sj].T).ravel().tolist()

    same_mean = round(statistics.fmean(same), 4) if same else None
    cross_p95 = round(_p95(cross), 4) if cross else None
    separation = (round(same_mean - cross_p95, 4)
                  if same_mean is not None and cross_p95 is not None else None)
    return {
        "chunks_per_cluster": {s: int(len(e)) for s, e in cluster_embs.items()},
        "clusters_dropped_too_short": dropped,
        "same_pairs": len(same),
        "cross_pairs": len(cross),
        "same_cluster_cosine_mean": same_mean,
        "cross_cluster_cosine_p95": cross_p95,
        "separation": separation,
    }


class _TorchPeak:
    """torch-only VRAM counter — a BREAKDOWN, not the capacity number: it
    cannot see CTranslate2 (faster-whisper) or onnxruntime allocations."""

    def __init__(self, device: str):
        self._torch = None
        if device == "cuda":
            try:
                import torch  # noqa: PLC0415
                torch.cuda.reset_peak_memory_stats()
                self._torch = torch
            except Exception:
                pass

    def read(self):
        if self._torch is not None:
            return round(self._torch.cuda.max_memory_allocated() / 2**20)
        return None


class _VramPoller:
    """Whole-GPU peak via periodic `nvidia-smi memory.used` sampling in a
    background thread — allocator-agnostic, so it sees torch + CTranslate2 +
    ONNX together. This is the number the D5 deployment decision reads."""

    def __init__(self, interval_s: float = 1.0):
        self.interval_s = interval_s
        self.max_mb: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
            return int(out[0]) if out else None
        except Exception:
            return None

    def _loop(self):
        while not self._stop.is_set():
            mb = self._sample()
            if mb is not None and (self.max_mb is None or mb > self.max_mb):
                self.max_mb = mb
            self._stop.wait(self.interval_s)

    def start(self):
        if self._sample() is None:
            return  # no nvidia-smi — poller stays inert, max_mb stays None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s + 6)


# --------------------------------------------------------------------------
# probe-live-stt — measure live STT latency (baseline vs during a batch run)
# --------------------------------------------------------------------------

def cmd_probe_live_stt(args: argparse.Namespace) -> int:
    import ssl  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    sample = Path(args.sample).read_bytes()
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; "
            f"filename=\"probe.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
            ).encode() + sample + f"\r\n--{boundary}--\r\n".encode()
    if args.ca_bundle:
        ctx = ssl.create_default_context(cafile=args.ca_bundle)
    elif args.insecure:
        # Last resort for the self-signed renfield.local cert on a trusted LAN;
        # prefer --ca-bundle with the local CA (no MITM blind spot).
        ctx = ssl._create_unverified_context()
    else:
        ctx = None

    # Failed probes are counted as timeout-clamped samples, NOT dropped:
    # the exact failure mode this gate exists for (batch load pushing live
    # STT into timeouts) must WORSEN the p95, not clean it up.
    latencies: list[float] = []
    errors = 0
    deadline = time.monotonic() + args.duration_s
    while time.monotonic() < deadline:
        req = urllib.request.Request(
            args.url.rstrip("/") + "/api/voice/stt", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     **({"Authorization": f"Bearer {args.token}"} if args.token else {})})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=args.timeout_s, context=ctx):
                pass
            latencies.append(time.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001 — a probe records failures, never aborts
            errors += 1
            latencies.append(args.timeout_s)
            print(f"probe error (counted as {args.timeout_s}s): {exc}", file=sys.stderr)
        time.sleep(args.interval_s)

    if not latencies:
        print("no probes executed", file=sys.stderr)
        return 1
    ok = sorted(latencies)
    result = {
        "samples": len(latencies),
        "errors": errors,
        "error_rate": round(errors / len(latencies), 3),
        "p50_s": round(ok[len(ok) // 2], 3),
        "p95_s": round(_p95(latencies), 3),
        "mean_s": round(statistics.fmean(latencies), 3),
        "timeout_s": args.timeout_s,
    }
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


# --------------------------------------------------------------------------
# report — gates (fail-closed: a missing hard metric FAILS; verdict is per
# candidate, overall PASS = at least one candidate passes all hard gates AND
# the live probe pair passes)
# --------------------------------------------------------------------------

def cmd_report(args: argparse.Namespace) -> int:
    gates = _load_gates(Path(args.gates))
    print(f"{'gate':58} {'value':>12} {'threshold':>10}  verdict")
    print("-" * 96)

    any_candidate_passes = False
    for metrics_path in args.metrics:
        m = json.loads(Path(metrics_path).read_text(encoding="utf-8"))
        label = f"{m.get('whisper_model', '?')}|{m.get('audio_stem', Path(metrics_path).stem)}"
        scores = m.get("diarization_scores") or {}
        sep = m.get("embedding_separation") or {}

        rows = [
            _gate("attribution_error_rate", scores.get("attribution_error_rate"),
                  gates["attribution_error_rate_max"], "<="),
            _gate("der_like", scores.get("der_like"), gates["der_like_max"], "<="),
            _gate("hyp_coverage", scores.get("hyp_coverage"),
                  gates["min_hyp_coverage"], ">="),
            _gate("gpu_s_per_audio_min", m.get("gpu_seconds_per_audio_minute"),
                  gates["gpu_seconds_per_audio_minute_max"], "<="),
            _wer_gate(m.get("wer_sample"), gates.get("wer_max", 1.0)),
            _separation_gate(sep, gates),
        ]
        candidate_pass = all(ok for _, _, _, _, ok, advisory in rows if not advisory)
        any_candidate_passes = any_candidate_passes or candidate_pass
        for name, val, thr, note, ok, advisory in rows:
            verdict = ("PASS" if ok else "FAIL") if not advisory else \
                      ("pass" if ok else ("n/a" if ok is None else "no-build"))
            print(f"[{label}] {name:{max(1, 56 - len(label))}} {val:>12} {thr:>10}  "
                  f"{verdict}{'  # ' + note if note else ''}")
        print(f"[{label}] {'-> candidate verdict':{max(1, 56 - len(label))}} "
              f"{'':>12} {'':>10}  {'PASS' if candidate_pass else 'FAIL'}")

    # Live-latency gate is global (one probe pair per spike run). Missing
    # probe data is a missing HARD gate -> the overall verdict cannot pass.
    live_ok = False
    if args.live_baseline and args.live_during:
        base = json.loads(Path(args.live_baseline).read_text(encoding="utf-8"))
        during = json.loads(Path(args.live_during).read_text(encoding="utf-8"))
        factor = round(during["p95_s"] / base["p95_s"], 2) if base.get("p95_s") else None
        name, val, thr, note, ok, _ = _gate("live_stt_p95_factor", factor,
                                            gates["live_stt_p95_factor_max"], "<=")
        live_ok = bool(ok)
        extra = f"baseline_err={base.get('error_rate')} during_err={during.get('error_rate')}"
        print(f"{name:58} {val:>12} {thr:>10}  {'PASS' if ok else 'FAIL'}  # {extra}")
    else:
        print(f"{'live_stt_p95_factor':58} {'—':>12} "
              f"{'<=' + str(gates['live_stt_p95_factor_max']):>10}  FAIL  # not measured "
              "(pass --live-baseline/--live-during)")

    print("-" * 96)
    overall = any_candidate_passes and live_ok
    print("OVERALL:", "PASS — §2 build unblocked (auto-match only if its gate passed)"
          if overall else "FAIL — do not start the §2 build "
          "(no candidate passed all hard gates and/or live latency unmeasured/failed)")
    return 0 if overall else 1


def _gate(name, value, threshold, op, advisory=False):
    """Row tuple: (name, value, threshold, note, ok, advisory). A missing value
    on a HARD gate is ok=False (fail-closed); on an advisory gate it is None."""
    if value is None:
        ok = None if advisory else False
        return (name, "—", f"{op}{threshold}", "metric missing", ok, advisory)
    ok = value <= threshold if op == "<=" else value >= threshold
    return (name, str(value), f"{op}{threshold}", "", ok, advisory)


def _wer_gate(wer_sample, wer_max):
    """Transcription WER regression gate. HARD (blocks) when measured; advisory
    'n/a' when ``wer_sample`` is absent (jiwer not installed) so a run that didn't
    measure text quality never false-fails. Catches the gross-ASR-failure class
    (the German-on-English 0.95-WER bug) without punishing hard-but-fine audio."""
    if wer_sample is None:
        return ("wer", "—", f"<={wer_max}", "jiwer not installed — WER not gated",
                None, True)
    return ("wer", str(wer_sample), f"<={wer_max}", "", wer_sample <= wer_max, False)


def _separation_gate(sep: dict, gates: dict):
    """Advisory auto-match gate (never blocks §2). Below the minimum pair
    counts the metric is 'insufficient data', not a number vs threshold."""
    note = "gate for BUILDING auto-match, not for shipping §2"
    if not sep:
        return ("auto_match_separation", "—",
                f">={gates['auto_match_separation_min']}", note + " (not measured)",
                None, True)
    if (sep.get("same_pairs", 0) < gates["separation_min_same_pairs"]
            or sep.get("cross_pairs", 0) < gates["separation_min_cross_pairs"]):
        return ("auto_match_separation",
                f"{sep.get('separation')}",
                f">={gates['auto_match_separation_min']}",
                note + f" (insufficient data: {sep.get('same_pairs', 0)} same/"
                       f"{sep.get('cross_pairs', 0)} cross pairs)",
                None, True)
    value = sep.get("separation")
    if value is None:
        return ("auto_match_separation", "—",
                f">={gates['auto_match_separation_min']}", note + " (not measured)",
                None, True)
    ok = value >= gates["auto_match_separation_min"]
    return ("auto_match_separation", str(value),
            f">={gates['auto_match_separation_min']}", note, ok, True)


def _load_gates(path: Path) -> dict:
    """Load gates.yaml via PyYAML when present, else a minimal flat parser
    (the file is a single `gates:` mapping of numeric thresholds)."""
    try:
        import yaml  # noqa: PLC0415
        return yaml.safe_load(path.read_text(encoding="utf-8"))["gates"]
    except ImportError:
        gates: dict[str, float] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.endswith(":"):
                continue
            key, _, value = line.partition(":")
            gates[key.strip()] = float(value)
        return gates


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate-fixture", help="synthesize meeting wav + reference")
    g.add_argument("--script", required=True, help="text file: 'Speaker: sentence' lines")
    g.add_argument("--out", required=True, help="output wav path")
    g.add_argument("--voices", required=True,
                   help="'Speaker=Voice,...' (say voice names / piper model paths)")
    g.add_argument("--engine", choices=["say", "piper"], default="say")
    g.add_argument("--sample-rate", type=int, default=16000)
    g.add_argument("--gap-s", type=float, default=0.6)
    g.set_defaults(func=cmd_generate_fixture)

    r = sub.add_parser("run", help="run diarization+ASR+alignment on one fixture")
    r.add_argument("--audio", required=True)
    r.add_argument("--reference", help="reference.json (omit for exploratory runs; "
                                       "the report then FAILS that candidate)")
    r.add_argument("--out", required=True, help="metrics JSON output path")
    r.add_argument("--whisper-model", default="large-v3")
    r.add_argument("--language", default="de")
    r.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    r.add_argument("--diarization-model", default="pyannote/speaker-diarization-3.1")
    r.add_argument("--num-speakers", type=int, help="optional speaker-count hint")
    r.add_argument("--ecapa-onnx", help="voice-server ECAPA ONNX model path "
                                        "(enables the auto-match separation metric)")
    r.add_argument("--embed-chunk-s", type=int, default=20)
    r.add_argument("--embed-min-s", type=int, default=5)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("probe-live-stt", help="latency probe against voice-server")
    s.add_argument("--url", required=True, help="voice-server / backend base url")
    s.add_argument("--token", default=os.environ.get("VOICE_TOKEN", ""),
                   help="bearer token; PREFER the VOICE_TOKEN env var — argv is "
                        "visible in `ps` and shell history")
    s.add_argument("--sample", required=True, help="short wav to POST repeatedly")
    s.add_argument("--interval-s", type=float, default=2.0)
    s.add_argument("--duration-s", type=float, default=120.0)
    s.add_argument("--timeout-s", type=float, default=60.0,
                   help="per-request timeout; failures are clamped to this value")
    s.add_argument("--ca-bundle", help="PEM file with the local CA that signed the "
                                       "server cert (preferred over --insecure)")
    s.add_argument("--insecure", action="store_true",
                   help="skip TLS verification — last resort for self-signed certs "
                        "on a trusted LAN; prefer --ca-bundle")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_probe_live_stt)

    rep = sub.add_parser("report", help="evaluate metrics against the gates")
    rep.add_argument("metrics", nargs="+", help="one or more metrics JSONs from `run`")
    rep.add_argument("--gates", default="tests/eval/diarization/gates.yaml")
    rep.add_argument("--live-baseline", help="probe-live-stt JSON without batch load")
    rep.add_argument("--live-during", help="probe-live-stt JSON during a batch run")
    rep.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
