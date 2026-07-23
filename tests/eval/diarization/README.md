# Diarization eval — spike T1 & permanent regression harness

Harness: [`bin/run_diarization_eval.py`](../../../bin/run_diarization_eval.py) ·
Gates: [`gates.yaml`](gates.yaml) (fixed 2026-07-06 before first measurement; fail-closed
hardening amended same day by the PR #924 review) ·
Design: [`docs/design/meeting-transcription.md`](../../../docs/design/meeting-transcription.md)

## Two-tier fixture policy

| Tier | Where | Purpose |
|---|---|---|
| Synthetic (committed) | `fixtures/` | Privacy-clean regression anchor — placeholder names, TTS voices. Re-run after every whisper/pyannote/threshold change. |
| Real recordings (NEVER committed) | `local/` (gitignored) | Actual room/mic/German-household acoustics. The gates are judged on THESE; the synthetic fixture guards against regressions. |

Fixture provenance: `meeting_synthetic_de.wav` is generated from
`meeting_script_de.txt` (command below); `probe_short.wav` is a 4 s ffmpeg cut
of that same synthetic file (`ffmpeg -i meeting_synthetic_de.wav -ss 0.6 -t 4
probe_short.wav`) — no real voices anywhere in `fixtures/`.

Synthetic audio is sequential turns with gaps — it cannot exercise overlapping
speech or room reverb. Treat synthetic results as an upper bound; real
recordings decide the gates.

**Writing a `reference.json` by hand:** segments must be NON-OVERLAPPING (the
frame scorer is single-label; overlapping reference speech would be silently
mis-scored). Label a 2-3 minute excerpt; ±0.3 s boundary precision is fine —
speaker identity dominates the metric. For genuinely overlapping passages,
place the boundary where the dominant speaker changes.

## 1. Generate the synthetic fixture (macOS, one-time)

```bash
python3 bin/run_diarization_eval.py generate-fixture \
  --script tests/eval/diarization/fixtures/meeting_script_de.txt \
  --out tests/eval/diarization/fixtures/meeting_synthetic_de.wav \
  --voices "Anna=Anna,Ben=Rocko (Deutsch (Deutschland)),Clara=Sandy (Deutsch (Deutschland)),David=Eddy (Deutsch (Deutschland))"
```

## 2. Record real fixtures (drop into `local/`)

Per capture comparison (D15): record the SAME short meeting (~10 min, 3-4
speakers) twice — phone in the table center AND an XVF3800 satellite test
capture. Write a `*.reference.json` for a 2-3 minute excerpt (rules above).

## 3. Run on a GPU host

The run needs pyannote.audio + faster-whisper + speechbrain + onnxruntime —
exactly the voice-server stack plus pyannote. Easiest: a one-off container from
the voice-server image on a GPU box (e.g. cuda.local, which has docker + GPU;
avoid gpu-3 during the day — it serves live voice).

Export `HF_TOKEN` in your shell first (gated pyannote model — one-time license
accept on huggingface.co), then pass it through WITHOUT putting the value on
the command line (`-e HF_TOKEN` pass-through form, so it never lands in shell
history or `docker inspect` of the command):

```bash
export HF_TOKEN=...   # or use --env-file with a chmod-600 env file

docker run --rm --gpus all \
  -v /path/to/renfield:/eval -w /eval \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -e HF_TOKEN \
  your-registry.example/renfield/voice-server:<current-tag> \
  bash -lc 'pip install "pyannote.audio>=3.1" scipy pyyaml && \
    for M in base medium large-v3; do \
      python bin/run_diarization_eval.py run \
        --audio tests/eval/diarization/local/meeting_phone.wav \
        --reference tests/eval/diarization/local/meeting_phone.reference.json \
        --whisper-model $M --ecapa-onnx "$SPEAKER_MODEL_PATH" \
        --out /tmp/metrics-$M.json ; done'
```

Notes:
- The runtime `pip install` is for the SPIKE only — the §2 build bakes model +
  deps into the image (offline-first, no runtime HF access). Mounting the HF
  cache lets an offline GPU host reuse a model downloaded elsewhere.
- Model load/download time is excluded from `gpu_seconds_per_audio_minute`
  (reported separately as `t_model_load_s`), so cold- and warm-cache runs are
  comparable.
- **Embedding-space parity check (once per image tag):** the separation metric
  must live in the SAME ONNX space as production. Verify: extract the
  `speaker_embedding` for `fixtures/probe_short.wav` via the voice-server REST
  `/api/voice/stt` response, embed the same file with the harness
  (`--ecapa-onnx`), and confirm cosine ≥ 0.99. If not, the preprocessing
  drifted — fix before trusting any separation number.

## 4. Live-latency impact (gate)

```bash
# baseline (no batch running), then again DURING a `run`:
export VOICE_TOKEN=...   # bearer token — env var, NOT argv (visible in ps)
python3 bin/run_diarization_eval.py probe-live-stt \
  --url https://renfield.local --ca-bundle /path/to/local-ca.pem \
  --sample tests/eval/diarization/fixtures/probe_short.wav \
  --duration-s 120 --out /tmp/live-baseline.json
```

Probe caveats (all deliberate):
- `/api/voice/stt` requires auth → `VOICE_TOKEN`; renfield.local is
  self-signed → pass the local CA via `--ca-bundle` (or `--insecure` as a
  last resort on a trusted LAN).
- Failed/timed-out probes are counted as timeout-clamped samples — degradation
  cannot hide by dropping requests. `error_rate` lands in the JSON.
- The default 2 s interval ≈ 60 requests / 120 s — stay under the voice rate
  limit or raise `--interval-s`.
- **Side effects:** every probe is a REAL STT request and hits the
  speaker-recognition path (review-bucket entries under controlled
  enrollment). Run against a non-prod instance if possible, or expect ~60
  synthetic-voice candidates in the review bucket and clean them up after.

## 5. Verdict

```bash
python3 bin/run_diarization_eval.py report /tmp/metrics-*.json \
  --gates tests/eval/diarization/gates.yaml \
  --live-baseline /tmp/live-baseline.json --live-during /tmp/live-during.json
```

Semantics (fail-closed, per the PR #924 hardening):
- Verdict is **per candidate** (whisper model × recording): overall PASS needs
  **at least one** candidate passing every hard gate (AER, der_like,
  hyp_coverage, GPU cost) — `base` may fail while `large-v3` passes.
- A **missing** hard-gate metric (e.g. `run` without `--reference`, or no
  probe pair) is a FAIL, never an "n/a" — a run that measured nothing cannot
  green-light the build.
- `auto_match_separation` is advisory: it only decides whether the
  auto-matcher gets BUILT (D12); with fewer than the minimum pair counts it
  reports "insufficient data" instead of a number.
- Exit 0 = §2 build unblocked. The `run` also writes `der_pyannote_metrics`
  when pyannote.metrics is importable — if it disagrees materially with
  `der_like` on the synthetic fixture, the scorer itself is buggy;
  investigate before trusting any verdict.
