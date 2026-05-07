# B.5 — XTTS-v2 vs Piper Evaluation Report

**Date:** 2026-05-07.
**Author:** ebongard (with Claude Code).
**Branch:** `spike/b5-xtts-eval`. **PR:** #538.
**Decision:** **Stay on Piper.** Latency gate fails by 5×; VRAM is fine; XTTS is unreliable on short prompts. Subjective MOS pass not run — would not change the decision because the latency gate alone is disqualifying.

---

## TL;DR

| Gate (per `docs/B5_PLAN.md` §7) | Threshold | Measured | Status |
|---|---|---|---|
| **1. MOS (medium prompts)** | XTTS-clone − Piper ≥ 0.5 | not run | n/a |
| **2. Latency (p95 TTFB)** | XTTS-clone ≤ 2× Piper p95 | XTTS-clone **1367 ms** vs 2× Piper = **256 ms** | ❌ **FAILS by 5.3×** |
| **3. VRAM (post-cache steady)** | ≤ 8 GB | **4.25 GB** | ✅ passes (52 % of budget) |
| **4. Drift (long, listener)** | ≤ 1 of 5 yes-count | not run | n/a |

License gate (Step 0): exit (a) — XTTS-v2 cleared for eval only under CPML. Even on a hypothetical full-gate pass, the swap-in PR would have been gated on a separate Reva license discussion (per `docs/B5_LICENSE_NOTE.md`). Latency-fail makes that conversation moot.

---

## Methodology

Per `docs/B5_PLAN.md` v4 with the in-window discoveries documented in `docs/B5_RUNBOOK.md`. Summary:

- **Spike image** — `voice-server:b5-spike-rc1` layered FROM `v0.1.5`. Adds GPU torch (cu124), `coqui-tts==0.27.0`, `transformers>=4.40,<5.0` (pin discovered mid-window), latest `voice_server/` source (also discovered mid-window). Built on `.159`, pushed to Harbor. Push initially failed with `504 Gateway Timeout` — root-caused to a 3-layer proxy chain with default short timeouts; bumped to 30m at all three layers. See `public_k8s/docs/proxy-chain-timeouts.md`.
- **Spike pod** — single GPU on `k8s-gpu-3`. Production scaled to 0 for the duration. ConfigMap `xtts-thorsten-ref` mounted at `/etc/xtts-ref/` to deliver the cloning reference (NFS is read-only on the production mount). `TTS_HOME=/cache/coqui-tts` redirects Coqui's model cache to the Longhorn PVC (default `~/.local/share/tts/` triggers DiskPressure eviction).
- **Reference clip** — synthesised with the production Piper-thorsten voice for a 14-word German passage (~10.6 s, 22.05 kHz mono 16-bit). Tests "can XTTS reproduce the brand voice the household already hears?", not "can XTTS clone the original Thorsten Müller dataset speaker."
- **Corpus** — 25 hand-written prompts in 4 categories (5 short, 10 medium, 5 long, 5 special). Production-corpus sample (D3 second source) **deferred** — operator workflow blocked on manual anonymisation; would need a follow-up benchmark run if revisiting.
- **Benchmark** — `voice-server/scripts/b5_benchmark.py` inside the spike pod. 3 warmup prompts × 3 engines (excluded), then 25 corpus prompts × 3 engines = 75 measured trials. Per-trial: HTTP timing, server-side X-Synth-Chunk-Times-Ms, output WAV bytes, RMS, measured duration, librosa spectral-centroid delta on long+xtts. VRAM via `nvidia-smi --query-gpu=memory.used` at engine boundaries.
- **Sample-rate parity** — XTTS native 24 kHz → librosa-resampled to 22.05 kHz inside `XTTSService` to match Piper. Listening would compare like-to-like rate; latency timed pre-resample (the X-Synth-Chunk-Times header reflects native synth time, not the resample step).

---

## Results

### Latency (the gate-2 metric)

Per-engine, all categories combined:

| Engine | n | TTFB median | TTFB **p95** | Total median | Total p95 |
|---|---:|---:|---:|---:|---:|
| **Piper (de_DE-thorsten-medium)** | 25 | 98 ms | **128 ms** | 146 ms | 495 ms |
| XTTS-default (Damien Black) | 20 | 785 ms | 1573 ms | 2359 ms | 7074 ms |
| **XTTS-clone (thorsten ref)** | 18 | 844 ms | **1367 ms** | 2742 ms | 6601 ms |

**Piper p95 TTFB = 128 ms. The 2× gate cap is 256 ms. XTTS-clone is 1367 ms — 5.3× the cap.**

Per-category TTFB median, illustrating where the gap lives:

| Category | Piper | XTTS-default | XTTS-clone |
|---|---:|---:|---:|
| short (≤5 words) | 95 ms | 276 ms | 398 ms |
| med (1-2 sentences) | 100 ms | 721 ms | 714 ms |
| long (paragraph) | 98 ms | 1192 ms | 1199 ms |
| spec (numbers/anglicisms/etc.) | 103 ms | 785 ms | 844 ms |

Piper's TTFB is **flat at ~95-103 ms** across all categories — Piper streams sentence-by-sentence and the user hears the first sentence almost immediately, no matter how long the full reply is. XTTS-v2 in our integration synthesises the **full prompt** before returning (necessary for the autoregressive decode), so TTFB scales with length. The architectural gap is fundamental, not a tuning issue.

If we ever wanted to close the latency gap, we'd need XTTS streaming (sentence-by-sentence dispatch with overlap-add joining). That's `+1 day` of follow-up work per the plan's Step 8 if-XTTS-wins branch — but the failure-rate finding below complicates it further.

### VRAM (the gate-3 metric)

| Boundary | memory.used (MB) |
|---|---:|
| Before Piper measured trials | 3720 |
| After Piper | 3976 |
| Before XTTS-default | 3976 |
| After XTTS-default | 4250 |
| Before XTTS-clone | 4250 |
| After XTTS-clone | 4250 |

XTTS adds **~250 MB** to a hot Piper-only system. The plan's projection in `docs/VOICE_PIPELINE_DESIGN.md:172` was 4.0 GB **for XTTS alone**, totalling 7.7 GB system-wide. Actual measured: 4.25 GB total = ~3 GB under budget. The projection was conservative; XTTS-v2 weights are mostly on disk, with only KV cache + active decoder resident.

Verdict: **gate passes**. Even with overlapping STT inference (Whisper-medium ~2 GB) the budget would be ~6.25 GB, well under the 8 GB envelope.

### Failure-rate gate

Per the harness, trials that failed validation (duration outside [0.5×, 2.0×] of expected words/3-wps heuristic) are excluded from listening data:

| Engine | trials | failed | rate | gate |
|---|---:|---:|---:|---|
| Piper | 25 | 0 | 0 % | ✅ |
| XTTS-default | 25 | 5 | 20 % | ❌ (gate is ≤10 %) |
| XTTS-clone | 25 | 7 | 28 % | ❌ |

**All XTTS failures are over-long output.** Examples:
- `short-01` "Ja, gerne." (expected 0.82 s) → XTTS-clone produced **4.68 s** (5.7×), XTTS-default 1.76 s (2.1×).
- `short-02` "Das habe ich erledigt." (1.48 s) → XTTS-clone 3.76 s, XTTS-default 3.80 s.
- `med-04` (2.82 s) → XTTS-clone 6.77 s.

This is a known XTTS-v2 weakness on short inputs: the autoregressive decoder doesn't terminate cleanly when conditioning is sparse, so it pads with breath sounds, repeats fragments, or echoes the speaker_wav reference. **Even if the latency gate were passable, this is a brand-quality problem on its own** — household replies are short more often than long, and unreliable termination on "Ja, gerne." is unacceptable.

### Drift on long prompts (gate-4 mechanical proxy)

Spectral-centroid delta between first 3s and last 3s of each long-prompt synth, in Hz. Smaller = more stable voice through the paragraph.

| Engine | n | median | max | per-prompt |
|---|---:|---:|---:|---|
| XTTS-default | 5 | 729 Hz | 1003 Hz | long-01: 751, long-02: **1003**, long-03: 243, long-04: 729, long-05: 165 |
| XTTS-clone | 5 | **248 Hz** | 353 Hz | long-01: 99, long-02: 340, long-03: 353, long-04: 248, long-05: 58 |

**XTTS-clone has 3× lower drift than XTTS-default.** The cloning conditioning anchors the speaker more reliably, exactly as the spike's design hypothesis predicted.

Listener subjective verdict (gate-4 #4) was not run — the data above is the mechanical proxy only. If it had been run and confirmed the centroid delta correlates with audible drift, **XTTS-clone would still need to clear the latency gate**, which it doesn't.

### Audio artefacts (qualitative, from spot-checking the WAVs)

Spot-listening the failing XTTS-clone short-prompt WAVs (5 of 7 failures auditioned):
- `short-01` (4.68 s for "Ja, gerne."): audible breath sound + 2 s of silence-ish padding after "gerne".
- `short-02` (3.76 s for "Das habe ich erledigt."): the model says "Das habe ich erledigt. Das habe ich erledigt." — repeating the prompt.
- `short-04` (3.01 s for "Verstanden, einen Moment."): trailing breath + faint echo of "Moment".

This matches the documented XTTS-v2 short-input failure mode in upstream issues. Larger conditioning windows mitigate it, but our `voice_ref` is already 10.6 s — extending further into the 20-30 s range is unlikely to fix the autoregressive termination problem. It's a structural limitation of the model.

---

## 4-Gate evaluation summary

1. **MOS gate** — n/a (listening pass not run; would not change decision).
2. **Latency gate** — ❌ **FAILS by 5.3× on p95 TTFB** AND fundamentally limited by no-streaming architecture for XTTS in our integration.
3. **VRAM gate** — ✅ PASSES (4.25 GB total vs 8 GB envelope; ~3 GB under projection).
4. **Drift gate** — n/a (listener pass not run; mechanical proxy favours XTTS-clone over XTTS-default but doesn't determine pass/fail).

**ANY-fail rule (per plan §7):** ANY of the 4 gates failing → "stay on Piper." **Latency gate fails decisively.** Recommendation locked in regardless of unrun gates.

**Compounding factor:** even at hypothetical full-gate-pass, the CPML license gate (Step 0 exit `a`) requires a separate Reva-side license discussion before swap-in. With latency already failing, that conversation is moot.

**Compounding factor:** **failure-rate gate** triggered (XTTS-clone 28 % failure) on top of the threshold gates. Even ignoring all four pre-committed gates, the 28 % unreliability rate on short prompts alone is disqualifying for a household assistant where short replies dominate.

---

## Decision

> **Stay on Piper. Do not promote XTTS-v2 — neither as default nor as opt-in.**

Reasons in priority order:
1. Latency gate fails by 5.3× and is structural, not tunable.
2. 28 % failure rate on short prompts (XTTS-clone) is a brand-reliability cliff — household assistant replies are mostly short.
3. CPML license precludes commercial framework use anyway (Reva).

The benchmark harness, listening UI design, corpus, runbook, and proxy-chain fix REMAIN useful for the next TTS evaluation. Per the doc's note in B5_LICENSE_NOTE.md: ChatterboxTTS (MIT code AND MIT weights, multilingual incl. German, released 2025) is the primary post-XTTS candidate. A future spike re-uses 80 % of B.5's infrastructure.

---

## What stays in tree

| Artefact | Path | Why |
|---|---|---|
| Plan | `docs/B5_PLAN.md` | Decision audit trail |
| License note | `docs/B5_LICENSE_NOTE.md` | CPML analysis, alternative-candidate list |
| Runbook | `docs/B5_RUNBOOK.md` | Reusable for next TTS spike |
| Benchmark harness | `voice-server/scripts/b5_benchmark.py` | Reusable; engine-adapter-friendly |
| Corpus | `voice-server/tests/b5/corpus_handwritten.txt` | Reusable matrix |
| **This report** | `docs/B5_XTTS_EVAL.md` | Decision artefact |
| Reference clip generator | `voice-server/scripts/generate_thorsten_ref.py` | Reusable for cloning-engine evaluations |
| Proxy-chain doc | `public_k8s/docs/proxy-chain-timeouts.md` | Net-positive infra fix, applies broadly |

## What gets deleted (post-decision)

| Artefact | Why |
|---|---|
| `voice-server/voice_server/services/xtts_service.py` | XTTS not adopted; production has no use |
| `voice-server/voice_server/services/_engine_adapter.py` | Single-engine production doesn't need an adapter |
| `voice-server/Dockerfile.spike` | Spike-only |
| `k8s/voice-server-spike.yaml` | Spike-only |
| `voice-server/scripts/predownload_xtts.py` | Coqui TTS doesn't use HF cache anyway (discovered mid-window) |
| ConfigMap `xtts-thorsten-ref` in `renfield` ns | Spike-only |
| Image `:b5-spike-rc1` on Harbor | Spike-only; can stay until next Harbor cleanup |
| `xtts_*` settings in `voice_server/config.py` | Spike-only |
| `engine` + `voice_ref` fields in `TTSRequest` | Spike-only |

The dispatch in `rest_voice.py` reverts to the pre-Step-2 single-path Piper handler.

Cleanup PR is a follow-up — not part of this decision merge. The spike branch's commits stay as the audit trail.

## Discoveries worth surfacing for future TTS spikes

1. **Coqui TTS uses its own model manager, not HF Hub.** Setting `HF_HUB_CACHE` doesn't redirect Coqui. Use `TTS_HOME` instead. The `predownload_xtts.py` script wasted 1.8 GB of PVC; Coqui re-downloaded the same weights to its own cache layout. *Lesson:* future TTS-engine evals should test the model-manager wiring early.
2. **Coqui transformers pin gap.** `coqui-tts==0.27.0` requires `transformers` without a version pin, but breaks on `>=5.0` due to a removed symbol (`isin_mps_friendly`). *Lesson:* run `from TTS.api import TTS` as a build-time smoke check.
3. **Layered FROM previous-version image silently inherits old source.** The first 4 builds of `:b5-spike-rc1` lacked the new dispatch logic because the Dockerfile only `COPY scripts` and `COPY tests` — voice_server/ was inherited from v0.1.5. *Lesson:* always `COPY voice_server` explicitly in spike Dockerfiles even when layered on a recent base.
4. **NFS read-only mount on production by design.** Operator-prep tasks that need to write to `/mnt/llm` from a production pod fail with EROFS. *Lesson:* writes for spike-prep should target a writable scratch (`/tmp`), then propagate via ConfigMap/PVC/kubectl-cp.
5. **k8s-gpu-3 disk pressure threshold is tight.** The node has 48 GB ephemeral; the spike image is 7 GB pulled and Coqui's writes pushed it past the 7.5 GB-free threshold. *Lesson:* set explicit `ephemeral-storage` requests on heavy AI/ML pods.
6. **Three-layer proxy chain (HAProxy → Traefik → Harbor) had short read timeouts.** The 504 errors looked like the GPU-torch layer being too big; the real cause was 30s/60s timeouts. Fix is one-time, benefits every future big-image push. *Lesson:* ALWAYS check timeouts at every proxy hop before refactoring image layouts to dodge them.

---

*Plan-doc lifecycle (per `docs/B5_PLAN.md` footer): with this report committed, `docs/B5_PLAN.md` becomes historical. `docs/VOICE_PIPELINE_DESIGN.md` gets a 1-paragraph appendix referencing this report.*
