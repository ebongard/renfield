# Phase B.5 — XTTS-v2 Evaluation Spike — Plan

**Status:** REVISED post plan-eng-review (2026-05-07). Awaiting human review on the revised version.
**Author:** ebongard (with Claude Code).
**Date:** 2026-05-07.
**Branch:** `spike/b5-xtts-eval`.
**Supersedes:** the single bullet `B.5 — XTTS-v2 evaluation spike` in `docs/VOICE_PIPELINE_DESIGN.md:415`.

**Review history:**
- v1 (DRAFT) — initial 8-step plan.
- v2 — post plan-eng-review: Step 0 license gate, Step 5 output validation + warmup, Step 6 smoke-test + Piper regression check + sample-rate parity + satellite reconnect, Step 7 blind randomized scoring + pre-committed decision threshold.
- v3 (this revision) — post code-reviewer agent pass on PR #538: Step 2 explicit adapter contract (Piper service has `stream_sentences` not `synthesize`; XTTS-v2 returns float samples not WAV bytes), Step 6 unloaded-VRAM probe before benchmark, Step 5 + Step 7 long-prompt voice-drift detection (mechanical spectral-centroid + listener yes/no), Step 7 latency gate retargeted from "p95 total" to "p95 TTFB on first sentence" (streaming dispatch means TTFB is what users feel).

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

### Step 0 — License clearance (≈30 min)

The Coqui company shut down end-2023; the canonical CPML text at `coqui.ai/cpml` returns 404. The HuggingFace XTTS-v2 model card still references CPML but the license terms are functionally unrecoverable via the open web. Per general knowledge of CPML, non-commercial use is free, commercial use requires a paid license — but with Coqui defunct, no party can grant commercial licenses anymore.

**This is a go/no-go for the spike.** Renfield-as-household self-hosted use is plausibly fine on the non-commercial branch. But Renfield is also positioned as the framework underlying Reva (commercial Enterprise Teams-Bot per `memory/project_reva_compatibility.md`). If we adopt XTTS for production, Reva inherits the engine; that's commercial use of a CPML model with no licensor available to negotiate.

- [ ] Recover CPML text from a non-broken source: web.archive.org snapshot of `coqui.ai/cpml`, the `coqui-ai/TTS` GitHub repo's archived README, or a vendored copy in any active fork.
- [ ] Read the CPML's "Non-Commercial Purpose" definition and the "Commercial Use" clause. Document quotes in `docs/B5_LICENSE_NOTE.md` (new file, kept in tree).
- [ ] Resolve via one of three exits:
  - **(a) Cleared for evaluation only.** CPML allows the spike (research/eval is non-commercial). Continue to Step 1, but the swap-in PR is gated on a separate license review with the Reva owner.
  - **(b) Cleared for production.** Self-hosted household + Reva together fit under "Non-Commercial" per the CPML reading. Proceed without further gating.
  - **(c) Blocked.** CPML restricts our use case. Spike pivots: same harness, different candidate. F5-TTS (CC-BY-NC-4.0 — same problem), ChatterboxTTS (MIT — clear), GPT-SoVITS (MIT — clear), Sherpa-onnx VITS-de (Apache 2.0 — clear). Pick one open-licensed alternative for the spike instead of XTTS, redo the comparison.
- [ ] If exit (c): retitle this plan to `B.5 — open-licensed TTS alternative spike`, swap engine references throughout, restart Step 1. The harness, corpus, and listening methodology stay; only the engine changes.

**Output:** `docs/B5_LICENSE_NOTE.md` with the CPML quote and the chosen exit (a/b/c). Required as a deliverable regardless of decision.

### Step 1 — Spike image (≈2 h)

- [ ] Add `voice-server/Dockerfile.spike`, layered on top of the v0.1.5 base. Pulls `coqui-tts==0.27.0` from the active fork (`idiap/coqui-ai-TTS` — the original `coqui-ai/TTS` is archived).
- [ ] Bake XTTS-v2 model download into `voice-server/scripts/predownload_xtts.py`. Model lands at `/cache/huggingface/hub/models--coqui--XTTS-v2`. Reuses the existing `voice-server-hf-cache` Longhorn PVC; no extra storage allocation.
- [ ] Build + push as `registry.treehouse.x-idra.de/renfield/voice-server:b5-spike-rc1` from `.159` per the existing `deploy-production` skill flow.

**Risks:** Coqui TTS pip install on CUDA 12.6 + Python 3.12 is non-trivial. The Dockerfile already has the `--allow-unauthenticated` apt workaround for nvidia/cuda 12.6.3 on Ubuntu 24.04. If `coqui-tts` wheel breaks, fall back to a `pip install git+https://github.com/idiap/coqui-ai-TTS.git@v0.27.0` source install. Add ~30 min if hit.

### Step 2 — Dual-engine TTS code (≈2 h)

**Adapter contract (NEW, explicit — not a mirror).** v2's "mirrors `tts_service.py`" was incorrect: Piper service exposes `stream_sentences(text, request_id, language) -> AsyncIterator[bytes]` publicly with `_synth_one_sentence` privately. XTTS-v2's API is `tts.tts(text, speaker_wav, language) -> list[float]` (raw float samples, not WAV bytes). The "shapes" don't match in either direction.

The spike defines a new uniform engine adapter, used ONLY by the benchmark, NOT by the production streaming path:

```python
# voice-server/voice_server/services/_engine_adapter.py (new, spike-only)
class TTSEngine(Protocol):
    def synth_one(self, text: str, voice_ref: Path | None, language: str) -> bytes:
        """One-shot synthesis. Returns 22.05 kHz PCM-16 mono WAV bytes."""
```

- [ ] New `voice-server/voice_server/services/_engine_adapter.py` with the `TTSEngine` Protocol and a Piper implementation that wraps `_synth_one_sentence` (Piper already produces WAV bytes at the right sample rate).
- [ ] New `voice-server/voice_server/services/xtts_service.py` implementing the adapter:
  - Loads XTTS-v2 once; subsequent calls switch via `speaker_wav` only
  - Calls `tts.tts(text, speaker_wav=voice_ref, language=language)` to get `list[float]` samples at 24 kHz
  - Resamples to 22.05 kHz (per Step 5's sample-rate normalization)
  - Wraps as PCM-16 mono WAV using `wave` module
  - For long prompts (>250 chars): pre-splits on sentence boundaries (`re.split(r'(?<=[.!?])\s+')`), synths each, concatenates raw PCM. XTTS-v2 OOMs and drifts on >250-token inputs; manual chunking is required, not optional. Benchmark records one TTFB (first chunk) and total synth time across all chunks.
- [ ] Extend `/api/voice/tts` REST handler to accept `engine: "piper" | "xtts-default" | "xtts-clone"` and `voice_ref: str | None`. Default stays `piper` for safety.
- [ ] No streaming sentence-by-sentence path through XTTS for the spike. Benchmark hits the one-shot endpoint. Production streaming integration is a follow-up if the swap happens.

**Files modified:** `voice-server/voice_server/api/rest_voice.py` (add engine param), `voice-server/voice_server/services/_engine_adapter.py` (new), `voice-server/voice_server/services/xtts_service.py` (new), `voice-server/voice_server/services/__init__.py` (export). No frontend changes — benchmark is server-side only.

### Step 3 — Reference clip for cloning (≈15 min)

- [ ] Synthesize a 15-second canonical reference using the *current* production Piper-thorsten voice. Phonetically diverse German text — TBD: candidate text in plan-review.
- [ ] Store at `/mnt/llm/voice/xtts_refs/thorsten_ref.wav` (the existing NFS share, voice-server already mounts it read-only).
- [ ] **Why use Piper-synthesised thorsten as the reference?** It directly answers the brand-consistency question: *"can XTTS reproduce the exact voice the household has been hearing for months?"* Using a real Thorsten Müller dataset clip would test "can XTTS sound like the original speaker," which is a different (less-relevant) question for our decision.

### Step 4 — Corpus (≈45 min)

- [ ] Hand-written: 25 prompts in `voice-server/tests/b5/corpus_handwritten.txt`.
  - 5 short (≤5 words): typical confirmations, refusals, one-word answers
  - 10 medium (1-2 sentences): typical assistant replies
  - 5 long (paragraph): full RAG answer or summary
  - 5 special-content: `numbers` (dates, times, prices), `anglicisms` ("Container deployen", "Status checken"), `technical` (Hostnamen, Kommandos), `german names + addresses`, `mixed code-switching`
- [ ] Production sample: 10 prompts pulled from the last 7 days of `services/piper_service.py` log lines in the backend pod. Anonymise by replacing family names with `[NAME]`, addresses with `[ORT]`. Save as `voice-server/tests/b5/corpus_production.txt`.
- [ ] Privacy guarantee: the report references prod prompts as `prod-01..prod-10` only; the raw text is not in the report or the git repo. The corpus file itself stays untracked (`.gitignore` entry).

### Step 5 — Benchmark harness (≈2 h)

- [ ] `voice-server/scripts/b5_benchmark.py`. Inputs: corpus paths, output dir. For each `(prompt, engine)` pair:
  - HTTP `POST localhost:8080/api/voice/tts` with the engine param
  - Captures: time-to-first-byte (ms), total synthesis time (ms), WAV bytes, RMS audio level (sanity check that audio isn't silence), measured WAV duration (seconds)
  - VRAM: snapshot via `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits` before/during/after each engine's measured synths (post-warmup, steady-state)
- [ ] **Warmup loop:** before the measured trials, run N=3 throwaway synths per engine using prompts NOT in the corpus. XTTS first inference includes model compilation + kernel autotuning (typically 5-15 s); without warmup, p95 latency is dominated by cold-start and the comparison is misleading. Warmup results are written to `_warmup.csv` for record but excluded from aggregate metrics.
- [ ] **Sample-rate normalization:** Piper outputs 22.05 kHz, XTTS-v2 outputs 24 kHz. Resample both engines' output to a common 22.05 kHz before writing the WAV. Document the resample step in the report. Latency comparisons measured *before* resample (so the engine's native synth time is captured); listening pass uses the resampled outputs.
- [ ] **Output validation per trial:**
  - WAV bytes ≥ 1 KB (catches HTTP errors that wrote empty files)
  - RMS audio level > 0.001 (catches silent-audio bugs that would otherwise score as MOS=1)
  - Measured duration within 50 %-200 % of expected for the prompt length (catches truncation / runaway synthesis)
  - **Voice drift check (long prompts only, XTTS engines only).** Compute spectral-centroid delta between first 3 s and last 3 s of the WAV (`librosa.feature.spectral_centroid` on each window, take absolute mean delta in Hz). XTTS-v2 is autoregressive and is well-known to drift speaker identity on inputs >200 chars — its weakness is precisely the brand-consistency question D2 is designed to answer. The spectral-centroid delta is a cheap mechanical proxy: small delta = stable voice, large delta = drift. Threshold tuned in-flight from the medium-prompt distribution as a baseline; record raw delta per trial. Listener subjective drift judgement (Step 7) is the authoritative signal; this check exists so we know to look harder when it fires.
  - Trials that fail any check go to `_failures.csv` with reason; the listening pass and aggregation exclude them. If failure rate >10 % for any engine, abort the run and investigate.
- [ ] Output structure:
  - `b5_results.json` — full raw data, per-trial (includes pre-resample and post-resample numbers)
  - `b5_results.csv` — flat table, easy to paste into the report
  - `_warmup.csv` — N=3 throwaway results per engine
  - `_failures.csv` — any (prompt, engine) pair that failed validation
  - `wavs/{prompt_id}_{engine}.wav` — one file per (prompt, engine), normalized to 22.05 kHz, ~120 files total

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
  - License gate result from Step 0
  - Results tables (latency, VRAM, MOS per category, inter-pass agreement)
  - 3-gate threshold evaluation
  - Reference-WAV pairs for each category (3 picks, embedded as relative links to the wavs/ tarball)
  - **Decision:** swap / don't-swap, with the failing gate(s) explicitly named if don't-swap.
- [ ] Append a 1-paragraph summary + the decision to `docs/VOICE_PIPELINE_DESIGN.md` directly under the existing B.5 line.

### Step 8 — Decision branch

Two pre-planned outcomes:

- **XTTS wins** → spike branch becomes the base for `feat/b5-xtts-swap` PR. Promotes XTTS to the production engine, bumps voice-server to `v0.2.0` (matches the doc-defined milestone). Includes the benchmark harness as a regression suite. Also adds streaming sentence-by-sentence dispatch through XTTS — non-trivial follow-up scope, +1 day.
- **Piper stays** → spike branch closed, no production change. Keep `voice-server/scripts/b5_benchmark.py` and the corpus in tree (under `voice-server/tests/b5/`) for re-runs when the next TTS candidate emerges. Report stays as the historical record.

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
