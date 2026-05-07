# Phase B.5 — XTTS-v2 Evaluation Spike — Plan

**Status:** v4 — Step 0 executed (license clearance complete, exit (a) chosen 2026-05-07). Steps 1-5 code-complete. Steps 6-7 are operator-driven; see [`docs/B5_RUNBOOK.md`](B5_RUNBOOK.md) for the copy-paste sequence from "build on .159" through "report written."
**Author:** ebongard (with Claude Code).
**Date:** 2026-05-07.
**Branch:** `spike/b5-xtts-eval`.
**Supersedes:** the single bullet `B.5 — XTTS-v2 evaluation spike` in `docs/VOICE_PIPELINE_DESIGN.md:415`.

**Review history:**
- v1 (DRAFT) — initial 8-step plan.
- v2 — post plan-eng-review: Step 0 license gate, Step 5 output validation + warmup, Step 6 smoke-test + Piper regression check + sample-rate parity + satellite reconnect, Step 7 blind randomized scoring + pre-committed decision threshold.
- v3 — post code-reviewer agent pass on PR #538: Step 2 explicit adapter contract (Piper service has `stream_sentences` not `synthesize`; XTTS-v2 returns float samples not WAV bytes), Step 6 unloaded-VRAM probe before benchmark, Step 5 + Step 7 long-prompt voice-drift detection (mechanical spectral-centroid + listener yes/no), Step 7 latency gate retargeted from "p95 total" to "p95 TTFB on first sentence" (streaming dispatch means TTFB is what users feel).
- v4 (this revision) — Step 0 executed: CPML text recovered from `huggingface.co/coqui/XTTS-v2/raw/main/LICENSE.txt`, full analysis in `docs/B5_LICENSE_NOTE.md`, **exit (a) chosen** (run the spike under CPML's testing/evaluation clause; do not auto-promote XTTS to default on win because Reva framework consumer is commercial). Verified open-licensed alternatives — ChatterboxTTS (MIT/MIT) is the primary post-XTTS candidate, F5-TTS eliminated (CC-BY-NC weights mirror the CPML problem), Sherpa-onnx VITS-de identified as effectively redundant with current Piper. Step 7 decision rule amended to "swap-or-document-as-future-target on win." Step 8 decision branch expanded from 2 to 3 outcomes.

---

## 1. Decision being answered

> **Should we swap the production TTS engine from `piper-tts` (de_DE-thorsten-medium) to Coqui XTTS-v2?**

The output is a decision artefact, not a swap. Implementation of the swap (or non-swap) is a separate follow-up PR keyed off the report's recommendation.

The criteria are the three D2-locked metrics: German MOS (subjective listen), latency (TTFB + total synth time), VRAM headroom on the RTX 4060 Ti (16 GB).

XTTS-v2 has to win on MOS *and* stay within the 8 GB peak-while-overlapping-STT envelope to be worth the swap. Latency is informational — Piper is sub-200 ms per sentence on GPU, XTTS will be slower, the question is "slow enough that streaming feels worse?"

## 2. Locked-in decisions

| ID | Question | Decision | Rationale |
|---|---|---|---|
| **D1** | How to deploy the spike image without breaking household voice? | **Maintenance window swap.** Scale `voice-server` to 0, deploy `b5-spike-rc1` parallel image, run benchmark, scale back to `v0.1.5`. ~30 min downtime. | Production image stays lean (Coqui pulls torch+xformers, ~2 GB). Spike is self-contained: either gets promoted in a follow-up PR or thrown away cleanly. |
| **D2** | Voice reference for XTTS-v2 cloning? | **Both.** Test (a) XTTS default German speaker AND (b) XTTS cloning a thorsten reference clip. Two engine variants per prompt. | (b) answers the brand-consistency question ("can we keep our voice?"), (a) measures the model's native German quality without the cloning artefact. Two data points cost the same listening pass; one extra benchmark engine. |
| **D3** | Test corpus source? | **Both.** 25 hand-written prompts covering the matrix + 10 anonymised real prompts pulled from production logs. | Hand-written gives matrix coverage (short / medium / long × numbers / anglicisms / technical / names). Production sample validates against actual usage. Privacy: real prompts referenced as `prod-01..prod-10` in tables; raw text not in the report. |
| **D4** | Quality scoring method? | **Subjective A/B listening only.** No automated proxy. | UTMOS / MOSNet are trained on English; their German numbers are not trustworthy. A misleading number in the report is worse than no number. Scope is one listening pass (~1.5 h). |

## 3. Workplan

9 steps (Step 0 is a license gate that must close before any code lands). Each is checkable for review-time progress tracking. Step ordering is a real dependency chain — Step 6 (the maintenance window) cannot start until Steps 1-5 are all done, and Step 0 must close before Step 1.

### Step 0 — License clearance — COMPLETE (2026-05-07, exit (a))

**Resolved.** Full analysis in `docs/B5_LICENSE_NOTE.md`. Headline:

- CPML text recovered from `huggingface.co/coqui/XTTS-v2/raw/main/LICENSE.txt` (LICENSE.txt is shipped alongside the model weights — the canonical `coqui.ai/cpml` URL is dead, but the model repo carries an authoritative copy).
- Renfield-household: clearly permitted (personal-use clause, no payment).
- Reva-commercial: NOT permitted for production deployment; CPML's commercial-entity branch caps at testing/evaluation. Compounded by Coqui being defunct (no commercial license available even for purchase).
- **Exit (b) closed** — XTTS cannot be Renfield's default engine while the framework is shared with a commercial consumer.
- **Exit (a) chosen** — run the spike under CPML's evaluation clause. On win, the swap is gated on a Reva-side license discussion and likely ships as opt-in only (default stays Piper). The harness + corpus + listening UI become reusable for any future TTS evaluation.
- **Verified post-XTTS candidates** (for any future re-evaluation): ChatterboxTTS (MIT code AND MIT weights, multilingual incl. German) is the primary alternative. F5-TTS eliminated (CC-BY-NC weights replicate the CPML problem). Sherpa-onnx VITS-de is effectively redundant with current Piper. GPT-SoVITS is fallback if Chatterbox underperforms on German.

The check items below are kept as a forensic record of how the gate was structured; all are completed.

- [x] Recover CPML text from a non-broken source — done via HuggingFace `LICENSE.txt`, with web.archive.org snapshots as cross-check.
- [x] Read CPML's "Non-Commercial Purpose" definition and "Commercial Use" clause — quoted verbatim in `docs/B5_LICENSE_NOTE.md`.
- [x] Resolve via one of three exits — exit (a) chosen.
- [n/a] If exit (c): retitle plan and swap engine references — not triggered.

### Step 1 — Spike image (≈2 h, +30 min for torch GPU swap)

- [x] Add `voice-server/Dockerfile.spike`, layered on top of the v0.1.5 base. Pulls `coqui-tts==0.27.0` from the active fork (the package is published as `coqui-tts` on PyPI by the `idiap/coqui-ai-TTS` maintainers; the original `coqui-ai/TTS` repo is archived). 2026-05-07.
- [x] **Discovery during Step 1: torch CPU→GPU swap required.** v0.1.5 ships CPU torch (`~150 MB`) because torch is used only for ECAPA's CPU-side feature preproc. XTTS-v2 needs GPU torch (`~1.5-2 GB compressed layer`) for CUDA inference. The spike Dockerfile uninstalls CPU torch and installs `torch>=2.4` from the cu124 wheel index. GPU torch is a superset of CPU torch — ECAPA's CPU ops continue working unchanged. This wasn't in the v3 plan; surfacing here so future readers know the v0.1.5 → spike delta is "more than adding a pip package." 2026-05-07.
- [x] Bake XTTS-v2 model download into `voice-server/scripts/predownload_xtts.py`. Uses `huggingface_hub.snapshot_download` with `allow_patterns` for the four required files (model.pth, config.json, vocab.json, speakers_xtts.pth — skips ~50 MB of training-state and samples). Model lands at `$HF_HUB_CACHE/hub/models--coqui--XTTS-v2`. Reuses the existing `voice-server-hf-cache` Longhorn PVC; no extra storage allocation. 2026-05-07.
- [x] Pre-accept CPML in non-interactive contexts via `ENV COQUI_TOS_AGREED=1` in the Dockerfile. The actual license text + rationale for accepting under CPML's testing/evaluation clause is documented in `docs/B5_LICENSE_NOTE.md`. 2026-05-07.
- [ ] Build + push as `registry.treehouse.x-idra.de/renfield/voice-server:b5-spike-rc1` from `.159` per the existing `deploy-production` skill flow. (Pending — requires `.159` build box access.)

**Risks:** Coqui TTS pip install on CUDA 12.6 + Python 3.12 is non-trivial. PyPI confirms `coqui-tts==0.27.0` exists with `requires_python: <3.15,>=3.10` and a `py3-none-any` wheel (pure-Python wrapper). If install breaks at build time, fall back to `pip install git+https://github.com/idiap/coqui-ai-TTS.git@v0.27.0` source install. Add ~30 min if hit.

**Risk (TRIGGERED, ROOT-CAUSED, FIXED 2026-05-07).** First build pushed the 5.2 GB single-RUN GPU torch layer to Harbor and got `Client Closed Request` followed by `504 Gateway Timeout` on retry. Initial mitigation tried in Dockerfile.spike was a 3-RUN split, but the split made the image **larger** (~7.4 GB instead of 5.2 GB) because pip downgraded the pinned nvidia-* deps in the torch layer, duplicating their binaries across layers. The actual root cause was the proxy chain in front of Harbor — HAProxy on the firewall (`tuning_timeoutHttpReq` defaulting to ~10s, `tuning_timeoutServer` to ~30s) AND Traefik in the cluster (entrypoint `http` `respondingTimeouts.readTimeout` defaulting to 60s). All three timeouts now bumped to 30m, documented in `public_k8s/docs/proxy-chain-timeouts.md`. Dockerfile reverted to single-RUN; image pushes through cleanly at ~83 MB/s = ~63s for the 5.2 GB layer.

### Step 2 — Dual-engine TTS code — COMPLETE (2026-05-07)

**Adapter contract (NEW, explicit — not a mirror).** v2's "mirrors `tts_service.py`" was incorrect: Piper service exposes `stream_sentences(text, request_id, language) -> AsyncIterator[bytes]` publicly with `_synth_one_sentence` privately. XTTS-v2's API is `tts.tts(text, speaker_wav, language) -> list[float]` (raw float samples, not WAV bytes). The "shapes" don't match in either direction.

```python
# voice-server/voice_server/services/_engine_adapter.py
class TTSEngine(Protocol):
    async def synth_one(self, text: str, voice_ref: Path | None, language: str) -> bytes:
        """One-shot synthesis. Returns 22.05 kHz PCM-16 mono WAV bytes."""
```

- [x] New `voice-server/voice_server/services/_engine_adapter.py` (113 lines). `TTSEngine` Protocol + `PiperEngine` adapter wrapping `TTSService._synth_one_sentence`. The Piper adapter is documentation of the contract; the REST handler still routes `engine=piper` through `TTSService.stream_sentences()` directly so production code paths are not perturbed by the spike. 2026-05-07.
- [x] New `voice-server/voice_server/services/xtts_service.py` (140 lines). Lazy-loads coqui-tts on first synth (so production v0.1.5 image, which doesn't ship coqui-tts, never triggers the import). Long-prompt manual chunking via `_split_sentences(max_chars=settings.xtts_max_chunk_chars)`. 24 kHz → 22.05 kHz resample via librosa (a coqui-tts transitive). Per-chunk timings exposed to the REST handler so the benchmark reads first-chunk TTFB from `X-Synth-Chunk-Times-Ms` response header. 2026-05-07.
- [x] Extended `voice-server/voice_server/api/rest_voice.py` (+90 lines). `TTSRequest` adds `engine: str | None` and `voice_ref: str | None` (both default-None to keep production callers unchanged). Handler dispatches: `piper` (default, existing path) / `xtts-default` / `xtts-clone`. Both engine paths emit `X-Synth-Chunk-Times-Ms` header for uniform benchmark timing. `engine=xtts-*` returns 503 when `xtts_enabled=False`. 2026-05-07.
- [x] Wired XTTSService construction in `voice_server/main.py` behind `settings.xtts_enabled` flag (default `False`). The spike Dockerfile sets `ENV XTTS_ENABLED=true`. Production v0.1.5 image: flag stays False, XTTS service not constructed, coqui-tts never imported. 2026-05-07.
- [x] Added xtts_* settings to `voice_server/config.py` (+23 lines): `xtts_enabled`, `xtts_repo_id`, `xtts_clone_voice_ref` (path), `xtts_use_cuda`, `xtts_target_sample_rate=22050`, `xtts_max_chunk_chars=240`. 2026-05-07.
- [x] No streaming sentence-by-sentence path through XTTS for the spike. Benchmark hits the one-shot endpoint. Production streaming integration is a Step 8 follow-up if the swap happens.

**Files touched:** `voice-server/voice_server/api/rest_voice.py` (+90 / -2), `voice-server/voice_server/services/_engine_adapter.py` (new, 113), `voice-server/voice_server/services/xtts_service.py` (new, 140), `voice-server/voice_server/main.py` (+11), `voice-server/voice_server/config.py` (+23), `voice-server/Dockerfile.spike` (+6 — XTTS_ENABLED=true env). `voice_server/services/__init__.py` left empty (the v2/v3 plan listed it as touched, but the package uses explicit submodule imports — no exports needed). No frontend changes.

### Step 3 — Reference clip for cloning — code COMPLETE (2026-05-07)

- [x] **Canonical text picked** (~40 words, ~13-14 s when spoken at thorsten-medium's natural pace, inside the XTTS-v2 5-30 s reference window): a natural German passage covering varied sentence lengths and the umlaut/sch-dominant phoneme set typical of household German. Text constant lives in `voice-server/scripts/generate_thorsten_ref.py:CANONICAL_TEXT`. 2026-05-07.
- [x] Wrote `voice-server/scripts/generate_thorsten_ref.py` (115 lines). Loads Piper-thorsten via the same `PiperVoice.load` API the production service uses, synthesises the canonical text, validates the WAV (channels, sample width, sample rate, duration in 5-30 s window), and writes to `/mnt/llm/voice/xtts_refs/thorsten_ref.wav`. Honours env overrides (`PIPER_VOICES_DIR`, `PIPER_DEFAULT_VOICE_DE`, `XTTS_REF_OUTPUT`). Idempotent overwrite — running twice produces the same reference. 2026-05-07.
- [ ] **Pending operator action** — run the script inside the production voice-server pod **before** scaling down for the maintenance window, so the reference exists on the NFS share before the spike pod boots:
  ```
  kubectl exec -n renfield deploy/voice-server -- \
      python /app/scripts/generate_thorsten_ref.py
  ```
  (Production v0.1.5 doesn't currently have this script in its image; the script needs to land in the production image first via a routine rebuild, OR be copied into the running pod at runtime via `kubectl cp`. Latter is the cheap path: `kubectl cp voice-server/scripts/generate_thorsten_ref.py renfield/voice-server-pod:/tmp/ && kubectl exec ... -- python /tmp/generate_thorsten_ref.py`.)

- [x] **Why use Piper-synthesised thorsten as the reference?** It directly answers the brand-consistency question: *"can XTTS reproduce the exact voice the household has been hearing for months?"* Using a real Thorsten Müller dataset clip would test "can XTTS sound like the original speaker," which is a different (less-relevant) question for our decision.

### Step 4 — Corpus — code COMPLETE (2026-05-07)

- [x] Hand-written: 25 prompts in `voice-server/tests/b5/corpus_handwritten.txt`. Format `prompt_id: text` per line, category derived from prefix (`short-` / `med-` / `long-` / `spec-`). 2026-05-07.
  - 5 short — confirmations, refusals, one-word answers
  - 10 medium — typical assistant replies (lamp control, calendar, weather, family chat, etc.)
  - 5 long — paragraph; covers the autoregressive-drift gate (Step 7 #4 metric): weather summary, news headlines, recipe instructions, day overview, troubleshooting how-to
  - 5 special — one prompt per sub-category: numbers/dates/prices, anglicisms (deployt, checken, dashboard), technical (hostnames, ports, commands), German names+addresses+phone, code-switching (push-notification, standup-meeting)
- [x] Production-sample procedure documented in `voice-server/tests/b5/README.md`. **Discovery during Step 4:** the backend's `piper_service.py` does NOT log synth text (status-only logging) — the v3 plan assumption "pull from log lines" doesn't work. The README pivots to a Postgres query against the backend's `messages` table (`role='assistant'`, last 7 days, length-filtered, `ORDER BY random() LIMIT 30`) and an anonymisation table (NAME / ORT / TEL / EMAIL / DATUM / PII). 2026-05-07.
- [x] `voice-server/tests/b5/corpus_production.txt` added to `.gitignore`. Privacy guarantee holds — the file never ships in the repo. 2026-05-07.
- [ ] **Pending operator action** — run the SQL extraction inside the maintenance-window prep, anonymise + pick 10 representative prompts, save to the (gitignored) corpus_production.txt.

Privacy guarantee: the report (`docs/B5_XTTS_EVAL.md`) references prod prompts as `prod-01..prod-10` only; the raw text is not in the report or the git repo.

### Step 5 — Benchmark harness — code COMPLETE (2026-05-07)

- [x] `voice-server/scripts/b5_benchmark.py` (483 lines). Designed to run INSIDE the spike pod (has librosa via coqui-tts transitive, has localhost API access). Operator copies results out via `kubectl cp` post-run. 2026-05-07.
- [x] **HTTP request flow:** for each `(prompt, engine)` pair, `POST /api/voice/tts` with `{text, engine, voice_ref?, language}`. Captures HTTP wall-clock + the per-chunk timing in the `X-Synth-Chunk-Times-Ms` response header (added in Step 2). Per-chunk timing is the gate-metric source for TTFB-on-first-sentence and total synth.
- [x] **Warmup loop:** N=3 throwaway prompts per engine before measured trials. Written to `_warmup.csv` for record but excluded from aggregate metrics. Toggleable via `--skip-warmup` for dev iteration.
- [x] **Sample-rate normalization** is handled inside `XTTSService.synth_one` (Step 2 deliverable) — XTTS native 24 kHz → 22.05 kHz via librosa. Per-chunk timings recorded BEFORE resample in `xtts_service.py`, so the X-Synth-Chunk-Times-Ms header reflects native synth time. The benchmark reads the WAV (already 22.05 kHz) for listening-pass uniformity.
- [x] **Output validation per trial** (`validate_trial`):
  - WAV bytes ≥ 1 KB (catches HTTP-error empty files)
  - RMS audio level > 0.001 (catches silent-audio bugs that would otherwise score as MOS=1)
  - Measured duration within 50 %-200 % of expected (heuristic: words ÷ 3 wps + sentences × 0.15 s pause; tested against the corpus and lands inside the validation window for typical prompts)
  - **Voice drift check** (long+xtts only): `centroid_delta_hz()` computes `|mean(spectral_centroid(first 3 s)) - mean(spectral_centroid(last 3 s))|` via librosa. Returned as `centroid_delta_hz` field on the trial record, raw — listener subjective judgement (Step 7) is the authoritative signal.
  - Failed trials → `_failures.csv` with reason. **Failure-rate gate:** if any engine exceeds 10 %, the harness exits with code 2 — the comparison is too noisy to feed the listening pass.
- [x] **VRAM measurement.** Captured at engine boundaries (before first measured synth of an engine, after last) via `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits`. Per-trial polling under load aliases against the active synth and contaminates the measurement; engine-boundary snapshots are clean.
- [x] **Output structure**:
  - `b5_results.json` — full raw data, per-trial, JSON-pretty
  - `b5_results.csv` — flat table for paste-into-report (chunk_times_ms serialised as a comma-separated string)
  - `_warmup.csv` — warmup-trial results
  - `_failures.csv` — any (prompt, engine) pair that failed validation
  - `wavs/{prompt_id}_{engine}.wav` — one file per (prompt, engine), already at 22.05 kHz from the engine layer, ~75 files total (25 prompts × 3 engines; +30 if production corpus also loads)
- [x] Updated `voice-server/Dockerfile.spike` to `COPY tests /app/tests` so the corpus is in the image; benchmark resolves `--corpus-dir` to `/app/tests/b5/` by default.

**Files touched:** `voice-server/scripts/b5_benchmark.py` (new, 483 lines), `voice-server/Dockerfile.spike` (+5 lines for `COPY tests`).

**CLI surface** (sanity-tested via manual regex parse against the hand-written corpus, all 25 prompts parsed, category counts match):
```
python /app/scripts/b5_benchmark.py \
    --output-dir /tmp/b5_results \
    --voice-ref /mnt/llm/voice/xtts_refs/thorsten_ref.wav
```
Useful flags: `--skip-warmup` (dev iteration), `--max-prompts N` (smoke a small subset), `--engines piper,xtts-clone` (skip xtts-default), `--no-drift-check` (if librosa missing).

### Step 6 — Maintenance window swap (≈50 min)

The window is announced separately by the user.

**Pre-window — Piper baseline capture (run BEFORE the window, against production):**
- [ ] Run a 5-prompt smoke against `engine=piper` on the live production v0.1.5 pod. Record TTFB and total synth time per prompt. This is the parity baseline for the in-window regression check (T1 from review). If spike-Piper later runs >10 % slower than this baseline, the comparison is invalidated and we either rebuild the spike image or run XTTS-only and re-use these numbers for Piper.

**Pre-flight (in window):**
- [ ] Confirm no active voice sessions: `kubectl --context renfield-private logs -n renfield deploy/voice-server --tail=20`. Visually verify no `session_start` events in the last 60 s.
- [ ] **Satellite reconnect plan.** Pi Zero satellites hold persistent WS connections and will auto-reconnect when voice-server returns. Per `memory/CRITICAL: Satellite Deployment Safety`, satellite restarts carry brick risk so we do NOT stop satellite services. Instead: (a) accept the reconnect storm, (b) verify each satellite returns to healthy state in the post-window check, and (c) if a satellite fails to reconnect, it stays down until the next scheduled satellite-deploy window — do not remote-restart on the spot.

**Window:**
- [ ] `kubectl scale -n renfield deploy/voice-server --replicas=0` — frees the GPU.
- [ ] Wait for `kubectl get pods -l app.kubernetes.io/name=voice-server` to show 0 pods, sleep 10 s, snapshot baseline VRAM. If `nvidia-smi memory.used` does not return to ~node-idle (<500 MB for CUDA runtime), investigate before deploying the spike pod — there's a leftover allocation that will contaminate measurements.
- [ ] `kubectl apply -f k8s/voice-server-spike.yaml` — separate manifest, `image=b5-spike-rc1`, otherwise byte-identical to `voice-server.yaml` (same node selector, same PVC, same NFS mount).
- [ ] Wait for pod ready (`kubectl wait`). Port-forward 8080.
- [ ] **Unloaded-VRAM probe (gate-pre-check, ~5 min).** Before the benchmark, measure XTTS-v2's standalone VRAM ceiling on the longest corpus prompt. Run `nvidia-smi --query-gpu=memory.used --loop=1 --format=csv,noheader,nounits` in one shell while synthesising the longest prompt 3× through `engine=xtts-clone` in another. Capture peak memory.used. If standalone XTTS peak alone is >7 GB, the 8 GB-with-Whisper-overlap gate is effectively pre-failed (Whisper-medium adds 2 GB on top during overlap; we'd be over-budget before the listening pass starts). Document the peak in the report; if pre-failed, the spike still runs (we want the MOS data) but the swap recommendation is automatically "stay on Piper" regardless of MOS outcome. Reasoning: the doc's 4 GB XTTS-v2 projection at `VOICE_PIPELINE_DESIGN.md:172` is unsourced; published XTTS-v2 figures show 3 GB weights + 1-2 GB autoregressive KV growth on long inputs, and we haven't measured this on a 4060 Ti.
- [ ] **Smoke test (abort-or-proceed gate).** Synthesize one canonical prompt ("Heute scheint die Sonne über dem Garten.") through each of the three engines. For each: validate WAV bytes >1 KB, RMS >0.001, duration 1.0-3.0 s. If any engine fails, abort the full run, capture pod logs to `/tmp/b5/spike_smoke_failure.log`, roll back to v0.1.5, debug offline. Only proceed to the full benchmark if all three smoke checks pass.
- [ ] **Piper-regression check.** Run the 5-prompt pre-window baseline corpus with `engine=piper` against the spike pod. Compare TTFB to the pre-window baseline. If spike-Piper is >10 % slower, the spike image has bumped CUDA libs in a way that hurts Piper, and the spike-vs-spike comparison is biased in XTTS's favor. Decision: (a) rebuild image without the offending dep, or (b) proceed with XTTS-only measurements and use pre-window Piper numbers for the comparison. Document choice in the report.
- [ ] Run full benchmark from local machine: `python voice-server/scripts/b5_benchmark.py …`.
- [ ] **VRAM measurement protocol.** Between each engine switch, call torch.cuda.empty_cache() in the spike pod (via a debug endpoint or pod exec), then snapshot VRAM. Report both pre-cache and post-cache numbers. Post-cache is the meaningful number for the swap decision; pre-cache is informational on fragmentation.
- [ ] `kubectl cp` results + WAVs to local `/tmp/b5/`.
- [ ] Tear down: `kubectl delete -f k8s/voice-server-spike.yaml`, `kubectl scale -n renfield deploy/voice-server --replicas=1`.

**Post-window:**
- [ ] **Mandatory post-deploy E2E** per project rule (`memory/feedback_post_deploy_browser_e2e.md`): browser test against `https://renfield.local`, ask a single voice question, verify TTS playback. curl smoketest is **not sufficient** — build-time env vars, cookies, mixed-content can only be observed in the browser.
- [ ] **Satellite reconnect verification.** Check each registered satellite has re-established its WS connection (`kubectl logs deploy/voice-server | grep session_start | grep <satellite-id>`). If any satellite is missing, escalate per the satellite-safety memory.

**Recovery posture:** if `voice-server-spike` pod fails to start (image broken, dependency conflict at runtime, OOM), the rollback is `kubectl delete + scale --replicas=1` — under 60 s. The production manifest (`k8s/voice-server.yaml`) is untouched throughout. No risk to production state.

### Step 7 — A/B listening + report (≈2.5 h, of which 1.5-2 h is the listen pass)

**Pre-committed decision threshold (set BEFORE the listen pass starts, to avoid post-data threshold-bending):**

XTTS-clone wins (recommend swap) only if ALL FOUR conditions hold:

1. **MOS:** XTTS-clone beats Piper by **≥0.5 MOS points on the medium-prompt category**. Medium prompts are the production-typical case; weight them strongest.
2. **Latency:** XTTS-clone p95 **TTFB on first sentence** stays within **2× Piper p95 TTFB**. The metric is TTFB-on-first-sentence, NOT total synth time, because production already streams sentence-by-sentence — what users feel is the wait until the first word is audible. (Piper TTFB ~150-200 ms on GPU; XTTS budget ~400 ms.) Total synth time is recorded for context but does not feed the gate.
3. **VRAM:** XTTS-clone peak VRAM (post-cache, steady-state) stays within the **8 GB envelope** projected in `VOICE_PIPELINE_DESIGN.md:172`. Anything higher invalidates the single-GPU concurrency assumption and changes the deployment story. The unloaded-VRAM probe in Step 6 is also a hard pre-gate: if standalone XTTS-v2 alone exceeds 7 GB, this gate fails before the benchmark runs.
4. **Voice drift (long prompts):** Listener subjective drift count for XTTS-clone on long prompts is **≤1 of 5** ("did the voice change identity within this clip?" → "yes" responses). XTTS-v2's autoregressive nature makes drift its known weakness on >200-char inputs. If 2 or more of the 5 long prompts show audible drift to the listener, the swap is rejected on brand-consistency grounds regardless of MOS.

If any of the four fails: recommendation is "stay on Piper." XTTS-default is informational only (since cloning is what we'd actually ship); its numbers do NOT feed the threshold gate.

**Listening pass (blind + randomized):**
- [ ] Generate `b5_listen.html` — static page that:
  - For each prompt, randomly permutes the three engine outputs and labels them A/B/C (the actual engine→label mapping is stored in a separate JSON not visible during scoring)
  - Presents one prompt at a time. The listener scores A, B, C on a 1-5 scale, with no engine identity visible.
  - **Drift question on long prompts only:** for each of the 5 long-category prompts, an additional yes/no question per engine: "Did the voice identity change between the start and end of this clip?" Aggregated separately from MOS — drift is a brand-consistency gate (Step 7 threshold #4), not a quality score.
  - Two-pass: after scoring all 35 prompts, take a 5-min break. Re-shuffle the A/B/C mapping per prompt and score again. Inter-pass agreement is a sanity check on score noise — if a listener gives the same engine wildly different scores across passes, fatigue is contaminating the data.
  - Saves to `localStorage` between sessions; explicit "reveal mapping and aggregate" button at the end.
- [ ] User scores 35 prompts × 3 engines × 2 passes = 210 ratings. Estimate ~1.5-2 h with 2 short breaks (after prompts 12 and 24 in each pass).
- [ ] Aggregate:
  - Mean MOS per engine, with 95 % CI (computed from both passes combined)
  - Inter-pass agreement per engine (Pearson r between pass-1 and pass-2 scores per engine; <0.7 = high noise, flag as low-confidence)
  - Mean MOS broken down by prompt category (so "XTTS wins on long but loses on numbers" is visible)
  - **Drift count per engine on long prompts** (out of 5; drift is the gate-#4 metric)
  - Mean spectral-centroid delta per engine on long prompts (mechanical proxy from Step 5; cross-check with subjective drift count)
  - Latency mean / median / p95 per engine — separately for **TTFB on first sentence** (gate metric) and **total synth time** (informational)
  - Peak VRAM per engine (post-cache, steady-state); also unloaded-VRAM probe result from Step 6
- [ ] **Threshold evaluation:** apply the 3-gate decision rule above. Document each gate's pass/fail. Record the recommendation.
- [ ] Write `docs/B5_XTTS_EVAL.md`:
  - Methodology (this plan, condensed)
  - License gate result from Step 0 (per `docs/B5_LICENSE_NOTE.md`: exit (a) selected; XTTS may not auto-promote even on win because of CPML / Reva framework constraint)
  - Results tables (latency, VRAM, MOS per category, drift count, inter-pass agreement)
  - 4-gate threshold evaluation
  - Reference-WAV pairs for each category (3 picks, embedded as relative links to the wavs/ tarball)
  - **Decision:** **`swap-or-document-as-future-target` / stay-on-Piper** (amended from v2's "swap / don't-swap" per Step 0 exit (a)). On win, the report explicitly does NOT recommend an automatic production swap — it documents the XTTS numbers as the calibration bar, and the swap itself is gated on a separate Reva-side license discussion. With the failing gate(s) explicitly named if stay-on-Piper.
- [ ] Append a 1-paragraph summary + the decision to `docs/VOICE_PIPELINE_DESIGN.md` directly under the existing B.5 line.

### Step 8 — Decision branch

Three pre-planned outcomes (amended from v2's two, per Step 0 exit (a)):

- **XTTS wins ON ALL 4 GATES → swap-or-document-as-future-target.** No automatic swap-in PR. Instead, the result feeds two follow-ups: (1) a Reva-side license discussion to determine whether Reva can opt out of XTTS at the framework boundary (resolution #1 or #2 from `docs/B5_LICENSE_NOTE.md`), and (2) a separate evaluation spike against ChatterboxTTS (verified MIT-MIT) to test whether an open-licensed engine matches the bar XTTS just set. The XTTS numbers become the calibration target for that next spike, not an immediate production swap. If after both follow-ups XTTS is still the right choice for non-Reva-affected operators, a `feat/b5-xtts-optin` PR ships XTTS as opt-in (default stays Piper), bumps voice-server to `v0.2.0`. Production swap to XTTS-as-default remains blocked by CPML.
- **XTTS wins on MOS but fails latency/VRAM/drift gate → stay on Piper, ChatterboxTTS spike planned.** XTTS is "good enough quality but wrong fit." The numbers still serve as the calibration bar for the ChatterboxTTS evaluation.
- **XTTS doesn't beat Piper → stay on Piper, no follow-up.** Spike branch closed, no production change. Keep `voice-server/scripts/b5_benchmark.py` and the corpus in tree for re-runs when the next TTS candidate emerges. Report stays as the historical record.

In all three branches, `voice-server/scripts/b5_benchmark.py`, `voice-server/tests/b5/corpus_handwritten.txt`, and the listening UI become permanent reusable infrastructure for the next TTS evaluation.

## 4. Deliverables

At the end of B.5, regardless of decision outcome:

1. `docs/B5_XTTS_EVAL.md` — methodology, tables, decision.
2. 1-paragraph summary appended to `docs/VOICE_PIPELINE_DESIGN.md`.
3. `voice-server/scripts/b5_benchmark.py` — reusable harness.
4. `voice-server/tests/b5/corpus_handwritten.txt` — committed.
5. WAVs as a tarball stashed somewhere (NFS, not git) — referenced from the report. Too large for git, not worth Git LFS for a one-off.

## 5. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Coqui TTS install fails on CUDA 12.6 / Python 3.12 | Medium | Source install fallback (Step 1); ~30 min. If both fail: pivot to Coqui's official Docker image as the spike base instead of layering on voice-server. ~2 h pivot. |
| XTTS-v2 cold-load VRAM spike pushes past 8 GB during overlap with Whisper-medium | Low | Doc projects 7.7 GB peak. If exceeded, single-session-only is enforced anyway by the existing pod (no concurrent voice sessions). Report still produced; the swap recommendation just gets a "needs second GPU" caveat. |
| Listening pass takes longer than 2 h, fatigue contaminates scores | Medium | Hard-stop at 35 prompts, 2 mandatory breaks (after prompts 12 and 24). If fatigue is visible in score variance, add a second short pass on a fresh day for the long-prompt category. |
| Production logs don't have 10 distinct TTS prompts in the last 7 days | Low | If under 10, top up from the assistant's recent chat history (same anonymisation rule). Keep the matrix size at 35. |
| Maintenance window collides with household active hours | Owner-decided | User picks the window. ~30 min of voice unavailable. |

## 6. Out of scope

- **Voice cloning of household members.** Not testing whether XTTS can clone a real person — only whether it can reproduce the existing Piper-thorsten brand voice.
- **Multi-language eval beyond German.** English XTTS quality is well-known; the swap decision is German-driven.
- **Streaming sentence dispatch through XTTS.** Belongs to the swap PR (Step 8 follow-up), not the spike.
- **Finetune XTTS-v2 on Thorsten Müller dataset.** Voice cloning from a reference is the standard use case; finetune is +0.5–1 day for marginal expected gain. If the cloning result is *almost* good enough, finetune becomes a third spike.
- **Comparing XTTS-v2 against other open-licensed alternatives** (F5-TTS, ChatterboxTTS, GPT-SoVITS, Sherpa-onnx VITS-de). Each is a separate spike. Step 0 exit (c) pivots THIS spike to one of those candidates if CPML blocks our use case; otherwise they remain future work.
- **Cache-pattern refactor.** `xtts_service.py` mirrors `tts_service.py`'s lock-and-check cache. If XTTS wins, the swap-in PR refactors both into a shared `services/_voice_cache.py` helper before promoting; for the spike, the duplication stays.

## 7. Total estimate

~10.5 h calendar time (v3; v1 was 8 h, v2 was 10 h):
- ~0.5 h Step 0 (license clearance)
- ~6.5 h build / benchmark / report (Steps 1-5, 7-8) — Step 2 budget bumped to 2 h after v3 found the adapter contract is non-trivial (Piper has `stream_sentences` not `synthesize`; XTTS returns float samples not WAV bytes; long-prompt manual chunking required)
- ~1.5-2 h listening pass (two-pass blind scoring + drift yes/no on long prompts)
- ~0.85 h maintenance window (Step 6, expanded in v3 for the unloaded-VRAM probe before the benchmark)

Each revision added cost to protect against a class of silent-failure modes:
- v2 +2 h: smoke gate, Piper regression check, blind randomized scoring → without these the decision could be corrupted
- v3 +0.5 h: explicit adapter contract, VRAM probe, drift detection → without these the spike would be measuring a fictional API or missing XTTS's known long-prompt weakness

Maintenance window: ~50 min, schedule TBD.

## 8. Pre-execution checklist for reviewer

- [ ] D1-D4 still right? Any locked-in decision I should reconsider?
- [ ] Step ordering correct? Any missing dependency?
- [ ] Risk #1 (Coqui install) mitigation strong enough, or should we proof-of-concept the install on .159 *before* committing to the spike branch?
- [ ] Step 6 maintenance window: announce-and-execute, or schedule via a dry-run on a non-prod cluster first? (We don't have a non-prod cluster — the alternative would be running benchmark against the spike pod *without* scaling production down, which violates D1.)
- [ ] Anything missing from the deliverables list?

---

*Plan-doc lifecycle: this file gets superseded by `docs/B5_XTTS_EVAL.md` once Step 7 is complete. Either delete the plan doc at that point (history in git) or keep it as a "how we did it" companion. Reviewer's call.*
