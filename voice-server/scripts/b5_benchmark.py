"""B.5 spike — benchmark harness driving Piper / XTTS-default / XTTS-clone.

Designed to run INSIDE the spike pod (has librosa via coqui-tts transitive,
has localhost access to the voice-server API). After the run, the operator
copies the results out:

    kubectl cp renfield/<spike-pod>:/tmp/b5_results /tmp/b5_results

Per-trial measurements:
- HTTP wall-clock time (request → full response)
- TTFB on first chunk (server-side, via X-Synth-Chunk-Times-Ms header)
- Total synth time across chunks (sum of chunk times)
- WAV bytes / RMS level / measured duration (for output validation)
- Voice-drift centroid delta (long prompts, XTTS engines only — gate-#4 metric)

VRAM is captured at engine boundaries (before first measured synth of an
engine, after last), not per-trial — `nvidia-smi` polling under load is
known to alias against the active synth and contaminate the measurement.

Failure-rate gate: if any engine's failure rate exceeds 10 %, the run
aborts with exit-code 2 — the comparison would be too noisy to feed
the listening pass.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("b5_benchmark")

ENGINES = ("piper", "xtts-default", "xtts-clone")
WARMUP_PROMPTS = (
    "Test eins zwei drei. Dies ist ein Aufwärmlauf des Synthesemoduls.",
    "Schöner Tag heute, das Wetter ist angenehm warm.",
    "Aufwärmphase, die nicht in den Messwerten erscheint.",
)

# Validation thresholds. WAV-bytes floor is generous to catch HTTP-error
# zero-byte payloads, not legit short clips. Duration window ratio is
# from the plan (50 %-200 % of expected for prompt length).
MIN_WAV_BYTES = 1024
MIN_RMS = 0.001
DURATION_LOWER_RATIO = 0.50
DURATION_UPPER_RATIO = 2.00

# Heuristic for expected duration: thorsten-medium speaks at ~3 wps for
# German. Sentence punctuation adds ~150 ms of pause. Accurate to ±25 %
# which is well inside the 50-200 % validation window.
EXPECTED_WPS = 3.0
SENTENCE_PAUSE_S = 0.15

# Drift detection (long prompts only).
DRIFT_WINDOW_S = 3.0


@dataclass
class Trial:
    prompt_id: str
    category: str
    engine: str
    text: str
    http_total_ms: float
    ttfb_first_chunk_ms: float | None
    total_synth_ms: float | None
    chunk_times_ms: list[float] = field(default_factory=list)
    wav_bytes_size: int = 0
    rms_level: float = 0.0
    measured_duration_s: float = 0.0
    expected_duration_s: float = 0.0
    centroid_delta_hz: float | None = None  # long+xtts only
    vram_mb_before: float | None = None  # only set on first trial of an engine
    vram_mb_after: float | None = None  # only set on last trial of an engine


@dataclass
class Failure:
    prompt_id: str
    engine: str
    reason: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, default=Path("/tmp/b5_results"))
    p.add_argument("--corpus-dir", type=Path, default=Path("/app/tests/b5"))
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument(
        "--voice-ref",
        type=Path,
        default=Path("/mnt/llm/voice/xtts_refs/thorsten_ref.wav"),
        help="speaker_wav reference for engine=xtts-clone",
    )
    p.add_argument("--language", default="de")
    p.add_argument("--skip-warmup", action="store_true")
    p.add_argument("--max-prompts", type=int, default=None,
                   help="limit corpus size for fast iteration during dev")
    p.add_argument("--engines", default=",".join(ENGINES))
    p.add_argument("--no-drift-check", action="store_true",
                   help="skip librosa-based drift detection (for dev when librosa is missing)")
    p.add_argument("--http-timeout", type=float, default=300.0)
    return p.parse_args()


def load_corpus(corpus_dir: Path, max_prompts: int | None) -> list[tuple[str, str, str]]:
    """Load both corpora. Returns list of (prompt_id, category, text)."""
    line_re = re.compile(r"^([a-z]+)-(\d+):\s*(.+)$")

    def _parse(path: Path, source: str) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        if not path.is_file():
            log.warning("corpus file missing: %s (%s)", path, source)
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = line_re.match(line)
            if not m:
                log.warning("unparseable corpus line in %s: %r", path, line[:80])
                continue
            category, num, text = m.groups()
            out.append((f"{category}-{num}", category, text.strip()))
        log.info("loaded %d prompts from %s", len(out), path.name)
        return out

    handwritten = _parse(corpus_dir / "corpus_handwritten.txt", "hand-written")
    production = _parse(corpus_dir / "corpus_production.txt", "production sample")
    if not production:
        log.warning(
            "production corpus missing — proceeding with hand-written only. "
            "Plan D3 specified BOTH; the report should note this."
        )

    combined = handwritten + production
    if max_prompts:
        combined = combined[:max_prompts]
    return combined


def expected_duration_s(text: str) -> float:
    words = len(text.split())
    sentences = max(1, len(re.findall(r"[.!?]", text)))
    return words / EXPECTED_WPS + sentences * SENTENCE_PAUSE_S


def nvidia_smi_used_mb() -> float | None:
    """Returns GPU memory.used in MB, or None if nvidia-smi unavailable."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        first_line = result.stdout.strip().splitlines()[0].strip()
        return float(first_line)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
        log.warning("nvidia-smi failed: %s", e)
        return None


def wav_metrics(wav_bytes: bytes) -> tuple[int, float, float, np.ndarray, int]:
    """Returns (size_bytes, rms_level, duration_s, samples_float32, sample_rate)."""
    size = len(wav_bytes)
    if size < 44:  # WAV header is at least 44 bytes
        return size, 0.0, 0.0, np.zeros(0, dtype=np.float32), 0

    with wave.open(io.BytesIO(wav_bytes), "rb") as r:
        nframes = r.getnframes()
        sample_rate = r.getframerate()
        sample_width = r.getsampwidth()
        nchannels = r.getnchannels()
        raw = r.readframes(nframes)

    if sample_width == 2:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0

    if nchannels > 1:
        samples = samples.reshape(-1, nchannels).mean(axis=1)

    duration_s = nframes / sample_rate if sample_rate else 0.0
    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    return size, rms, duration_s, samples, sample_rate


def centroid_delta_hz(samples: np.ndarray, sample_rate: int) -> float | None:
    """Plan-mandated drift proxy: |mean(centroid(first 3s)) - mean(centroid(last 3s))|.

    Returns None if librosa unavailable or audio is shorter than 2 × DRIFT_WINDOW_S
    (in which case the windows would overlap and the metric is meaningless).
    """
    try:
        import librosa  # type: ignore
    except ImportError:
        return None

    needed = int(DRIFT_WINDOW_S * sample_rate * 2)
    if samples.size < needed:
        return None

    win = int(DRIFT_WINDOW_S * sample_rate)
    head = samples[:win]
    tail = samples[-win:]
    head_centroid = librosa.feature.spectral_centroid(y=head, sr=sample_rate).mean()
    tail_centroid = librosa.feature.spectral_centroid(y=tail, sr=sample_rate).mean()
    return float(abs(head_centroid - tail_centroid))


def synth_one(
    client: httpx.Client,
    base_url: str,
    engine: str,
    text: str,
    voice_ref: Path | None,
    language: str,
) -> tuple[bytes, dict]:
    """Hit /api/voice/tts. Returns (wav_bytes, timing_dict).

    Raises httpx.HTTPError or RuntimeError on failure; caller turns it into
    a Failure record.
    """
    payload: dict = {"text": text, "engine": engine, "language": language}
    if engine == "xtts-clone" and voice_ref is not None:
        payload["voice_ref"] = str(voice_ref)

    t0 = time.monotonic()
    response = client.post(f"{base_url}/api/voice/tts", json=payload)
    http_total_ms = (time.monotonic() - t0) * 1000.0

    if response.status_code != 200:
        raise RuntimeError(
            f"HTTP {response.status_code}: {response.text[:300]}"
        )

    chunk_times_str = response.headers.get("X-Synth-Chunk-Times-Ms", "")
    chunk_times_ms = [float(x) for x in chunk_times_str.split(",") if x.strip()]
    timing = {
        "http_total_ms": http_total_ms,
        "ttfb_first_chunk_ms": chunk_times_ms[0] if chunk_times_ms else None,
        "total_synth_ms": sum(chunk_times_ms) if chunk_times_ms else None,
        "chunk_times_ms": chunk_times_ms,
    }
    return response.content, timing


def validate_trial(
    wav_size: int,
    rms: float,
    duration: float,
    expected: float,
) -> tuple[bool, str]:
    if wav_size < MIN_WAV_BYTES:
        return False, f"wav_size={wav_size} below floor {MIN_WAV_BYTES}"
    if rms < MIN_RMS:
        return False, f"rms={rms:.5f} below floor {MIN_RMS} (silent?)"
    lower = expected * DURATION_LOWER_RATIO
    upper = expected * DURATION_UPPER_RATIO
    if duration < lower or duration > upper:
        return False, f"duration={duration:.2f}s outside [{lower:.2f},{upper:.2f}] (expected {expected:.2f})"
    return True, "ok"


def run_warmup(
    client: httpx.Client,
    args: argparse.Namespace,
    engines: list[str],
    output_dir: Path,
) -> None:
    log.info("warmup: %d prompts × %d engines", len(WARMUP_PROMPTS), len(engines))
    rows: list[dict] = []
    for engine in engines:
        for i, text in enumerate(WARMUP_PROMPTS):
            try:
                _, timing = synth_one(client, args.base_url, engine, text, args.voice_ref, args.language)
                log.info("warmup %s/%d: total=%.0fms first_chunk=%.0fms",
                         engine, i, timing["http_total_ms"], (timing["ttfb_first_chunk_ms"] or 0))
                rows.append({
                    "engine": engine, "warmup_idx": i,
                    "http_total_ms": timing["http_total_ms"],
                    "ttfb_first_chunk_ms": timing["ttfb_first_chunk_ms"],
                    "total_synth_ms": timing["total_synth_ms"],
                })
            except Exception as e:
                log.warning("warmup %s/%d failed: %s", engine, i, e)
                rows.append({"engine": engine, "warmup_idx": i, "error": str(e)})

    out_path = output_dir / "_warmup.csv"
    if rows:
        keys = sorted({k for r in rows for k in r.keys()})
        with out_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        log.info("warmup results → %s", out_path)


def run_measured(
    client: httpx.Client,
    args: argparse.Namespace,
    engines: list[str],
    corpus: list[tuple[str, str, str]],
    output_dir: Path,
) -> tuple[list[Trial], list[Failure]]:
    wavs_dir = output_dir / "wavs"
    wavs_dir.mkdir(parents=True, exist_ok=True)

    trials: list[Trial] = []
    failures: list[Failure] = []

    for engine in engines:
        log.info("== engine: %s ==", engine)
        vram_before = nvidia_smi_used_mb()
        log.info("VRAM (memory.used) before %s: %s MB", engine, vram_before)

        engine_trial_count = 0
        for prompt_idx, (prompt_id, category, text) in enumerate(corpus):
            try:
                wav, timing = synth_one(client, args.base_url, engine, text, args.voice_ref, args.language)
            except Exception as e:
                log.warning("FAIL %s/%s: %s", engine, prompt_id, e)
                failures.append(Failure(prompt_id=prompt_id, engine=engine, reason=str(e)))
                continue

            size, rms, duration, samples, sample_rate = wav_metrics(wav)
            expected = expected_duration_s(text)
            ok, reason = validate_trial(size, rms, duration, expected)
            if not ok:
                log.warning("FAIL %s/%s: %s", engine, prompt_id, reason)
                failures.append(Failure(prompt_id=prompt_id, engine=engine, reason=reason))
                continue

            drift = None
            if (
                category == "long"
                and engine.startswith("xtts")
                and not args.no_drift_check
            ):
                drift = centroid_delta_hz(samples, sample_rate)

            wav_path = wavs_dir / f"{prompt_id}_{engine}.wav"
            wav_path.write_bytes(wav)

            trial = Trial(
                prompt_id=prompt_id,
                category=category,
                engine=engine,
                text=text,
                http_total_ms=timing["http_total_ms"],
                ttfb_first_chunk_ms=timing["ttfb_first_chunk_ms"],
                total_synth_ms=timing["total_synth_ms"],
                chunk_times_ms=timing["chunk_times_ms"],
                wav_bytes_size=size,
                rms_level=rms,
                measured_duration_s=duration,
                expected_duration_s=expected,
                centroid_delta_hz=drift,
                vram_mb_before=vram_before if engine_trial_count == 0 else None,
            )
            trials.append(trial)
            engine_trial_count += 1
            log.info(
                "%s/%s: ttfb=%sms total=%.0fms dur=%.1fs%s",
                engine, prompt_id,
                f"{timing['ttfb_first_chunk_ms']:.0f}" if timing["ttfb_first_chunk_ms"] is not None else "n/a",
                timing["http_total_ms"],
                duration,
                f" drift_dHz={drift:.0f}" if drift is not None else "",
            )

        # End-of-engine VRAM snapshot. Attach to last successful trial of this engine.
        vram_after = nvidia_smi_used_mb()
        log.info("VRAM after %s: %s MB", engine, vram_after)
        engine_trials = [t for t in trials if t.engine == engine]
        if engine_trials and vram_after is not None:
            engine_trials[-1].vram_mb_after = vram_after

    return trials, failures


def write_outputs(trials: list[Trial], failures: list[Failure], output_dir: Path) -> None:
    json_path = output_dir / "b5_results.json"
    with json_path.open("w") as f:
        json.dump([asdict(t) for t in trials], f, indent=2, ensure_ascii=False)

    csv_path = output_dir / "b5_results.csv"
    if trials:
        keys = list(asdict(trials[0]).keys())
        # Replace list-typed chunk_times_ms with a string repr for CSV safety.
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for t in trials:
                row = asdict(t)
                row["chunk_times_ms"] = ",".join(f"{x:.2f}" for x in row["chunk_times_ms"])
                w.writerow(row)

    failures_path = output_dir / "_failures.csv"
    if failures:
        with failures_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["prompt_id", "engine", "reason"])
            w.writeheader()
            for failure in failures:
                w.writerow(asdict(failure))

    log.info("outputs → %s/", output_dir)


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    unknown = [e for e in engines if e not in ENGINES]
    if unknown:
        log.error("unknown engines: %s (valid: %s)", unknown, ENGINES)
        return 2

    if args.voice_ref and not args.voice_ref.is_file():
        if "xtts-clone" in engines:
            log.warning(
                "voice-ref %s does not exist — engine=xtts-clone trials will fail; "
                "run scripts/generate_thorsten_ref.py first",
                args.voice_ref,
            )

    corpus = load_corpus(args.corpus_dir, args.max_prompts)
    if not corpus:
        log.error("empty corpus — nothing to benchmark")
        return 2

    log.info("corpus: %d prompts × %d engines = %d trials", len(corpus), len(engines), len(corpus) * len(engines))

    with httpx.Client(timeout=args.http_timeout) as client:
        if not args.skip_warmup:
            run_warmup(client, args, engines, args.output_dir)

        trials, failures = run_measured(client, args, engines, corpus, args.output_dir)

    write_outputs(trials, failures, args.output_dir)

    # Failure-rate gate. The plan calls 10% per engine an abort threshold —
    # exit-code 2 so CI / wrapper scripts can detect it.
    log.info("summary: %d trials succeeded, %d failed", len(trials), len(failures))
    abort = False
    for engine in engines:
        engine_total = len(corpus)
        engine_failed = sum(1 for f in failures if f.engine == engine)
        rate = engine_failed / engine_total if engine_total else 0.0
        log.info("  %s: %d/%d failed (%.1f%%)", engine, engine_failed, engine_total, rate * 100)
        if rate > 0.10:
            log.error("  → %s exceeds 10%% failure-rate gate", engine)
            abort = True

    if abort:
        log.error("ABORT: failure-rate gate triggered. Investigate before listening pass.")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
