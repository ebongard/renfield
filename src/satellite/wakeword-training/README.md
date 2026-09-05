# Per-language "Renfield" wake-word training

Reproducible recipe for training a **single-word, per-language** "Renfield"
openWakeWord model on a k8s GPU pod. The shipped German model `renfield_de.onnx`
(in `data/wakeword-models/`, served to satellites + browser) was produced this way.

## Why per-language single-word models

- The stock `hey_renfield` model is **English-pronunciation only** (offline
  scoring: EN clips 0.74–0.89, German clips ≤0.28). A German speaker saying
  "Renfield" barely registers — the root cause of the "premium far-field array
  but devastating 1–3 m recall" report.
- openWakeWord is a tiny classifier on a frozen melspectrogram + speech-embedding
  pipeline. **One short word per model** lets the small net spend all its capacity
  on one pronunciation, which both raises recall and tightens the false-positive
  boundary. Trying to cover DE+EN+IT in one model diluted recall ~5× and pushed
  false-positives up.
- The satellite loads **multiple** models at once (the detector takes a keyword
  list; the backend pushes the list and serves each `.onnx`). So the end state is
  `renfield_de` + `renfield_en` + `renfield_it` loaded together — one per language.

## Result (renfield_de)

- Arch: `layer_size=48`, `max_negative_weight=700`, concentrated multi-voice
  positives, German adversarial near-miss negatives.
- Offline: ~76 % recall / ~16.6 false-positives-per-hour @ threshold 0.9 on
  continuous synthetic speech; adversarial near-miss accept ~2.5 %.
- **Real voice beat the synthetic metric**: 9/9 detections of a spoken German
  "Renfield" at 0.50–0.99. The synthetic TTS-on-TTS recall understates real-world
  recall — trust an on-device test over the offline number.
- v1 `md5 = 6bdd7c61f31d2089220c8404716977cc` — **DO NOT SHIP.** It false-fired
  ~500×/hr fleet-wide in real rooms (see FP hardening below).
- **Deployed = v3** (`md5 = 5cef9bd7991fa48780272488e2869886`), hardened with
  real-ambient hard-negatives: **0 false wakes** fleet-wide, recall intact.

## Also shipped: renfield_en (US+UK) + renfield_it

Same recipe, per language — `gen_en.py` (9 US+UK voices), `gen_it.py` (2 IT
voices; piper ships only 2 Italian), `renfield_{en,it}.yaml` (= the DE v3 config
with `model_name` swapped; the room-ambient hard-negatives are **language-
independent**, so they're reused verbatim). Held-out real-ambient FP for both:
**0 false wakes** (peak 0.002 / 0.016), recall EN ~77 % / IT ~85 % synthetic.

**Loading all three at once:** the satellite detector already accepts a keyword
**list**, and the backend wake-word config now pushes a comma-separated set
(`renfield_de,renfield_en,renfield_it`) — `WakeWordConfig.keyword_list` splits it,
`update_config` validates each element. So one satellite wakes to "Renfield" in
any of the three pronunciations. Marginal CPU per extra model ≈ 0 (melspectrogram
+ embedding features are computed once and shared across all loaded classifiers).

## The GPU pod

The blocker was Blackwell (sm_120) + CUDA versions. What works:

- torch **2.7.0+cu128** (2.2 → "no kernel image for sm_120").
- onnxruntime-gpu **1.20.1** (CUDA-12). 1.27 needs libcudart.so.13 (CUDA-13) which
  isn't present; 1.20.1 runs against torch's bundled CUDA-12 libs.
- `LD_LIBRARY_PATH` from torch's bundled cuDNN/CUDA (`/work/ld.env`).
- **NFS RWX PVC** (`oww-nfs`) for `/work`, NOT a Longhorn RWO volume (RWO went
  "not ready for workloads" on pod recreate) and NOT node disk (image churn
  disk-pressure-tainted the node). Keeping the 17 GB negative features + clips on
  NFS keeps them off the node.
- torchcodec is CUDA-13-only → **bypassed**: decode audio with `soundfile`
  directly; download RIRs as already-16 kHz WAVs via `huggingface_hub`.
- Apply the 5 `train.py` patches — see `scripts/train.py.patches.md`.

## Pipeline (scripts/)

| Script | Role |
|---|---|
| `oww_setup.sh` | apt + pip env inside the pod (openwakeword + training stack; clones openWakeWord + piper-sample-generator) |
| `dl2.py` / `dl3.py` | fetch ACAV100M negative features (~17 GB) + MIT RIRs + AudioSet noise (already-16 kHz, no torchcodec) |
| `gen_de.py` | **German** positive + adversarial-negative generation (the shipped one) |
| `gen_samples.py` | multilingual generator (DE+EN-US+EN-UK+IT) — template for the other languages |
| `make_config.py` | write the openWakeWord training YAML |
| `renfield_de.yaml` | the German training config (layer 48, weight 700, fp-target 0.5/hr) |
| `run_train_de.sh` | run `openwakeword.train` end-to-end + export ONNX |
| `validate_de.py` / `diag_de.py` | overall + per-voice recall / false-accept validation |
| `score_wav.py` | score any wav the way the satellite does (80 ms streaming chunks) — peak score + detection events with timestamps. Use it to prove a room capture is provocative BEFORE training on it (see the commissioning doc) and to A/B two models on identical material. |
| `derive_detector_mono.py` | collapse a raw multi-channel room capture to the mono the detector actually scores, replaying the satellite's own beamform/select/downmix |

### Lessons baked into the config / generator
- **Concentrate the voices.** A 236-speaker `de_DE-mls` model sat at ~13 % recall
  and *diluted* the whole set — the single biggest fix was regenerating with a
  small set of distinct, high-quality voices (cap multi-speaker models to ~15
  speakers; drop the worst performers). `diag_de.py` (per-voice recall) is how you
  find the diluters.
- **Adversarial near-misses matter.** German fillers + near-miss words (Rennfeld,
  Feld, Held, rennen, Manfred, Reinfeld, Enfield, …) as negatives tighten the
  boundary around the one short word. See `ADVERSARIAL` in the generator.
- **Negative weight is the recall↔FP dial.** Lower weight → higher recall, more
  FPs. Sweep: weight 200 ≈ 79 %/~100 fp-hr; 500 ≈ 73 %/65; **700 ≈ 76 %/16.6**
  (the chosen point, concentrated voices). Tune per language.
- **Validate on a real voice in the room**, not just the offline number.

## Real-ambient false-positive hardening (v2/v3 — REQUIRED before shipping)

> **Per-room capture is a commissioning gate, not a training option.** Every
> satellite must have its own room in this negative set before it counts as
> live. The procedure, the acceptance thresholds, and the per-room event
> checklist are in [`docs/SATELLITE_ACOUSTIC_COMMISSIONING.md`](../../../docs/SATELLITE_ACOUSTIC_COMMISSIONING.md).
> Capture with `bin/capture-room-ambient.sh` (records raw multi-channel with a
> sanity gate) and collapse it with `scripts/derive_detector_mono.py` (replays
> the satellite's own beamform/select/downmix, so the negatives match what the
> detector actually scores).

**The synthetic FP metric lied by ~30×.** v1 measured ~16 fp/hr @0.9 on synthetic
speech, but in the real house it false-fired **~500×/hr fleet-wide** — a constant
wake→empty-transcription storm. Synthetic negatives do not represent your rooms.

The fix (scripts: `gen_hard_negs.py`, `validate_ambient.py`, `measure_wav.py`,
`renfield_de_v2.yaml`, `renfield_de_v3.yaml`):

1. **Record real room ambient** on each satellite:
   `bin/capture-room-ambient.sh satellite-<room> --minutes 45`. XVF3800/USB mics
   are exclusive → stop the service to record; HAT mics allow concurrent capture
   via the shared `dsnoop` PCM. Capture while the room is **in use** — a quiet
   noise floor is not what false-fires the model — and at the **deployment mic
   gain**. The script records RAW multi-channel (never a pre-downmixed mono: on a
   beamforming satellite that is a different signal than the detector scores) and
   rejects captures with a DC offset, clipping, or a dead channel.
   `scripts/derive_detector_mono.py` then produces the detector-side mono for
   `/work/ambient/`.
2. **`gen_hard_negs.py`** embeds each wav (`AudioFeatures._get_embeddings` →
   `(frames,96)`) and splits each room **75/25 by time**: first 75% → windowed
   `(N,16,96)` training **hard-negatives**; last 25% → concatenated **held-out
   FP-validation** (`real_ambient_features.npy`). The split avoids train/val leakage.
3. **Retrain** with the ambient as a heavily-sampled `hard_negative` feature class
   AND as `false_positive_validation_data_path` (so the FP target optimizes against
   REAL noise). Denser windowing (`step=1`) + `max_negative_weight` 1000→1500 helped.
4. **`validate_ambient.py`** scores the held-out ambient (model ONNX is fixed
   batch=1 → score one 16-frame window at a time) + recall on positive clips.

Results (held-out real ambient): v1 **336/h** → v2 **36/h** → v3 **18/h** @0.9,
recall steady ~70-75%. Real-voice live test after deploy: **0 false wakes**, the
storm gone, "Renfield" still detected.

### v4 — audiobook / media-speech hard-negatives (2026-07-06)

v3 was hardened against room *ambient* (idle noise) but never against **continuous
media speech**. An audiobook playing in the Arbeitszimmer (on the room's HiFiBerry,
which the satellite's AEC has no reference for) false-fired renfield_de **~24×/day**:
the model scored the narration up to **95-97%**, woke, and recorded the next words
as a "command". Audiobooks are the worst case — hours of varied human speech, so a
phonetic near-miss eventually crosses threshold.

Fix = the same recipe with the audiobook added as a new hard-negative class:
capture the live audiobook off the HAT (`arecord`, concurrent — no service stop),
drop the wav(s) into `/work/ambient/` **alongside** the v3 room-ambient (preserve
it), re-run `gen_hard_negs.py`, retrain (`renfield_de_v4.yaml` = v3 config; only
the ambient data changed). Capture at the **deployment mic gain** — negatives must
match what the model actually hears. A/B on the held-out set (now including the
audiobook): v3 **22.7 FP/h** @0.8 → v4 **0 FP/h**, German recall **69%** @0.8
(intact). Live after deploy: same audiobook scored **~23%** (was 95-97%), 0 wakes.

**Mic-gain lever, HAT edition:** on the WM8960 HAT the analogue of the XVF3800
`PP_AGCDESIREDLEVEL` is **`ALC Max Gain`**. At the max (7) the ALC amplified the
audiobook to peak **0.98** at the mic; cutting it to **4** dropped that to
**0.09-0.71** and roughly halved the residual activations — applied first, then v4
finished the job. Persist with `amixer -c <card> sset "ALC Max Gain" 4 && alsactl
store`, and in `host_vars/satellite-<room>.yml` (`wm8960_alc_max_gain`).

**Deploy gotcha — satellites cache the model by filename.** `ensure_models_available`
skips the download when `<model_id>.onnx` already exists locally, so a backend roll
alone does NOT push a new model of the same name. To refresh a bare-metal satellite
WITHOUT a service restart (SD-card brick risk): move the cached file aside
(`mv renfield_de.onnx renfield_de.onnx.v3bak`), then `PUT /api/settings/wakeword`
(broadcasts unconditionally) → the satellite re-runs its config-apply, finds the
file missing, downloads v4, and `update_config` hot-reloads the running detector.

**Per-satellite mic gain is a first-class FP lever — check it before over-training.**
The per-room breakdown (`validate_ambient` split by room) was decisive: v3 fired on
**zero** HAT-mic ambient (peak <0.005) — **100% of residual FP was the one XVF3800
satellite**, whose AGC we'd cranked (`PP_AGCDESIREDLEVEL=0.03`) for far-field reach.
That gain amplified the room's noise floor to speech amplitude. Halving it to
**0.015** dropped that satellite's peak 0.96→0.29 (0 false wakes) while still
detecting a normal "Renfield" across the room. Lesson: don't fight an over-gained
mic with more training data — fix the gain (it's the dominant FP knob on XVF3800
sats), then let the model handle the rest. The gain lives in the gitignored
`host_vars/satellite-<room>.yml` (`xvf3800_tuning.PP_AGCDESIREDLEVEL`), persisted
on-device with `xvf_host SAVE_CONFIGURATION 1`.

### v5 — Kinderbad in-use ambient (2026-09-05)

`md5 = e11f769cd141c303b87d602acc39910a`. Config identical to v4
(`renfield_de_v5.yaml` = `renfield_de_v4.yaml`); the only change is the ambient
corpus, which gained 45 min of **in-use** Kinderbad audio (`ambient_kinderbad_inuse_{a,b}.wav`,
split so part of it stays held out). Augmentation was skipped — the positive/negative
clips from June are unchanged, only `--train_model` was re-run (~9 min).

**The finding that motivated it:** Kinderbad was *already* in the v3 corpus, with a
quiet 10-minute capture. Scored against v3 that capture peaks at **0.127 with zero
detections** — the model never reacted to it, so it taught the model nothing, and
the room kept false-firing (25 genuine wakes in 9 h). The new in-use capture peaks
**0.969 with 8 detections**. Material the model ignores is not a hard negative.

Per-room A/B via `score_wav.py` (streaming, 80 ms chunks — the faithful path):

| Ambient | v3 | v4 | v5 |
|---|---|---|---|
| kinderbad in-use A (23 min) | peak 0.969, 6 ev | peak 0.964, 4 ev | **peak 0.609, 1 ev** |
| kinderbad in-use B (22 min) | peak 0.909, 2 ev | peak 0.925, 1 ev | peak 0.970, 1 ev |
| kinderbad (old, quiet) | peak 0.127, 0 | 0.003, 0 | 0.010, 0 |
| arbeitszimmer | — | 0.049, 0 | 0.028, 0 |
| fitnessraum | — | 0.150, 0 | 0.161, 0 |
| wohnzimmer | — | 0.001, 0 | 0.001, 0 |
| audiobook g4 / g7 | — | 0.191 / 0.002, 0 | 0.010 / 0.002, 0 |

Recall (synthetic positives): **75 % @0.5, 71 % @0.9** — in line with v3/v4.

Two honest caveats:

- **`validate_ambient.py` disagrees** (v5 2.5 FP/h @0.9 vs v4 0.0). It scores the
  *concatenated* held-out features of all 8 rooms, and each file boundary is a
  discontinuity that spikes the score. v5 has a stronger cold-start transient than
  v4 — visible as a lone 0.815 hit at **t=1 s** of the Arbeitszimmer file that
  vanishes (peak 0.028) once the first 5 s are skipped. In production a satellite
  warms up once per restart, so prefer the per-file streaming numbers above.
- **Kinderbad B still peaks 0.970 at t=243 s**, and that segment is in the
  *training* portion — v5 failed to suppress it. Possibly genuinely speech-like
  audio rather than a defect. Not investigated.

Recall is verified only synthetically. Per the commissioning gate, a human must
still speak "Renfield" in the room before this counts as passed.

## Train a new language (e.g. EN-US, EN-UK, IT)

1. Copy `gen_de.py` → `gen_<lang>.py`; swap `VOICE_DEFS` to that language's piper
   voices (concentrated, distinct) and `ADVERSARIAL` to that language's near-misses.
2. Copy `renfield_de.yaml` → `renfield_<lang>.yaml`; set `model_name=renfield_<lang>`.
3. Reuse the same negative features + RIRs (language-independent).
4. `run_train_<lang>.sh` → `renfield_<lang>.onnx`; validate with a per-voice diag.
5. Drop the `.onnx` in `data/wakeword-models/` + `src/frontend/public/wakeword-models/`,
   register the id in `AVAILABLE_KEYWORDS` (`services/wakeword_config_manager.py`),
   redeploy the backend, and add the id to the global wake-word list.

## Deploying a model (what makes it "live")

1. `cp <model>.onnx data/wakeword-models/` and `src/frontend/public/wakeword-models/`
   (deploy rsyncs `data/wakeword-models/` into the backend build context; the
   Dockerfile `COPY wakeword-models /app/wakeword-models` bakes it; the backend
   serves it at `/api/settings/wakeword/models/{id}` and satellites auto-download).
2. Register the id in `AVAILABLE_KEYWORDS` / `VALID_KEYWORDS`
   (`src/backend/services/wakeword_config_manager.py`) so validation accepts it.
3. Build + roll out the backend (see `.claude/skills/deploy-production`).
4. Set the global wake word (admin Settings → Wake Word, or the wakeword config
   API). The backend pushes the keyword list to every satellite, which downloads
   the model(s) and loads them. **The global config overrides any local satellite
   wake-word setting** — there is no per-satellite override today.
