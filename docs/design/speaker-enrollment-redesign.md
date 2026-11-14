# Speaker Enrollment Redesign — controlled enrollment over ambient auto-enroll

**Status:** Phases 0–3 BUILT (dark by default), 2026-07-06. Phase 4 (upstream capture / XVF3800) still research.
**Successor:** what controlled enrollment structurally cannot fix (short-turn text-independent matching, wakeword-speaker ≠ command-speaker) is designed in [`voice-identity-wakeword-verification.md`](voice-identity-wakeword-verification.md) (reviewed 2026-07-06).
**Trigger:** prod investigation found speaker recognition operating near its noise floor (38 fragmented "Unbekannter Sprecher", self-reinforcing profile pollution). See the measured evidence below.

**Build status (2026-07-06):**
- **Phase 0** (quality gating: L2-normalize before averaging, passive min-duration gate, confidence-gated continuous-learning) — SHIPPED dark (`speaker_quality_gating_enabled`).
- **Phase 1** (controlled multi-sample ONNX enrollment + cohesion gate + user-link + purge script) — SHIPPED (`services/speaker_enrollment_service.py`, `POST /api/speakers/enroll`, `bin/purge_unknown_speakers.py`).
- **Phase 2** (guided multi-take frontend flow) — SHIPPED (`components/speakers/GuidedEnrollModal.tsx`).
- **Phase 3** (controlled recognition + review bucket) — BUILT dark (`speaker_controlled_enrollment_enabled`): identify against ENROLLED profiles only, margin gate (`speaker_match_min_margin`), reference profiles immutable (no passive reinforcement), and a miss routes a quality-passing unknown to the `speaker_candidates` review bucket (capped `speaker_review_bucket_cap`) instead of auto-enrolling. Admin API `GET/POST /api/speakers/candidates{,/promote,/dismiss}` (SPEAKERS_ALL). Migration `pc20260707`.
- **Phase 3b** (review UI + calibration) — BUILT: `components/speakers/ReviewBucketSection.tsx` on `/speakers` (self-hiding until the bucket has candidates) lists unmatched voices and promotes selected ones to a named enrolled speaker or dismisses noise; `bin/calibrate_speaker_threshold.py` recommends `speaker_recognition_threshold` + `speaker_match_min_margin` from the household's OWN enrolled voices (measures same-vs-different separation the SAME way the resolver scores — cosine(sample, centroid); emits no number when the profiles overlap). **The flag is still NOT flipped on** — that is gated on an operational step no code can do: enroll >= 2 household members via `/speakers`, then run the calibration script and set the two values. Prod today has 1 enrolled speaker, so calibration reports same-speaker cohesion only.
- **Phase 4** (XVF3800 DSP softening) — PARTIAL, applied to Hobbyraum 2026-07-06. The chip booted to conference defaults tuned for ASR, not identity (AGC pumping + aggressive DNN-NS distort ECAPA embeddings) and **never persisted anything** (reverted to defaults every reboot). Now a softened profile — tamed AGC (`PP_AGCMAXGAIN` 120→60, longer `PP_AGCTIME`), higher NS floor (`PP_MIN_NS` 0.18→0.5), and the ASR beam tap (`AEC_ASROUTONOFF=1`) — is applied live + saved to the chip's OWN flash, and codified in provisioning (`provision.yml` "Apply XVF3800 DSP tuning" task + `xvf3800_tuning` var + committed `hardware/xvf3800/tune.sh`) so it survives reboot (chip flash) AND re-provision (when the per-satellite profile is present in `host_vars/` — those are gitignored, so the values live on the operator's provisioning machine, not in a fresh clone). Expected payoff is the measured **+0.04** cohesion (nice-to-have, not the lever). **Still needs a live recall/cohesion A/B** (someone speaking in the Hobbyraum) to confirm the ASR-tap + AGC changes don't hurt far-field wakeword recall; revert with `tune.sh reset`.
  - **Item 4 (playback echo-gating) BUILT as duck-on-listen** (`duck_on_listen_enabled`, dark): `ha_glue/services/duck_service.py` — when a room satellite enters LISTENING, lower the volume of media playing in that room over DLNA (via `mcp.dlna.get_volume`/`set_volume`), restore on IDLE (+ a safety timeout). Hooked in `satellite_manager._set_satellite_state`. This is the industry-standard "duck the source" answer to AEC-with-an-external-loudspeaker (research below), not a naive mute. Items 5-6 (DOA beam-pin, 24-bit) remain.

---

## 1. Problem — measured, not assumed

> **UPDATE (2026-07-05, live capture experiment — reframes the diagnosis):** an A/B capture on sat-hobbyraum of continuous clean speech through the *same* XVF3800 gave **same-speaker cohesion 0.70** (baseline) / **0.74** (AGC off + NS 0.5). So a **clean capture is HEALTHY** — the embeddings and the mic are fine. The 0.275 below was measured *within* auto-enrolled "Unbekannter" profiles that actually **mix multiple people** (so it was never truly same-speaker) plus short/noisy far-field turns. **Corrected conclusion:** the disease is **profile pollution** (fixed by controlled enrollment + the cohesion gate, Phase 1) and real-world capture conditions — NOT a broken model or the wrong DSP tap. XVF3800 softening is a real but modest **+0.04** bonus (Phase 4, nice-to-have), not the lever. This *validates* Phases 0–2 as the right fix.

Prod snapshot (2026-07-05, 41 speakers / 140 embeddings, 192-dim ECAPA) — the stored (largely polluted) profiles:

| Metric | Measured | Healthy far-field ECAPA |
|---|---|---|
| **Same-speaker cosine (median)** | **0.275** (within-polluted-profile) | 0.6 – 0.85 |
| **Different-speaker cosine (median / p95)** | **0.071 / 0.224** | 0.0 – 0.2 |
| Within-profile spread (n=10 profiles) | min ~0.03 → max ~0.77 | tight, high |
| Match threshold | **0.25** | — |

The same-speaker and different-speaker distributions **overlap almost completely** (0.275 vs 0.224 p95). The match threshold **0.25 sits inside the overlap**, so it fails both ways simultaneously:

- **>5% of *different*-speaker pairs exceed 0.25** → different people merge into one profile (the wide within-profile spread = pollution).
- **~half of *same*-speaker pairs fall below 0.25** → a person fails to match their own profile → a new "Unbekannter #N" is minted.

`speaker_continuous_learning` then appends every turn's embedding to whatever matched (including wrong matches), so profiles degrade over time — a self-reinforcing slide toward garbage. Result: 38 unnamed fragments for a 3-person household, 0 usable reference profiles.

### Root causes (ranked)
1. **Ambient auto-enroll is the wrong data source.** Every spoken turn that clears a 0.5 s / cosine-0.25 bar silently mutates the speaker DB — no consent, no quality gate, no distinction between "deliberate enrollment" and "passive recognition". Far-field, short, noisy utterances produce embeddings that barely encode identity.
2. **No quality gating.** Only guard anywhere is a 0.5 s min-duration (in-process path only) + an empty-PCM check. No SNR, no min-speech-duration, no min-sample-count, no per-enrollment consistency check.
3. **Threshold 0.25 is in the ambiguous band** — and no *fixed* threshold works until embedding quality improves; not the real lever.
4. **Averaging un-normalized embeddings** (`np.mean(raw)` über Vektoren mit Normen 246–412, dann normalisieren) — Aufnahmen mit größerer Norm dominieren den Schwerpunkt. Sekundär, aber real.
   🛑 **Nachtrag 2026-09-27:** `GET /api/speakers/identify` rechnete diesen Schwerpunkt bis dahin SELBST und wich vom lebenden Pfad in drei Punkten ab — normalisierte nur unter `speaker_quality_gating_enabled` (im Haushalt aus, während `controlled` an ist), nahm alle statt nur der eingeschriebenen Sprecher, und mittelte alle statt der `MAX_EMBEDDINGS_PER_SPEAKER` jüngsten Einbettungen. Das Diagnose-Endpunkt antwortete damit aus einem anderen Modell als die Erkennung. Die Rechnung steht jetzt genau einmal, als `speaker_resolver.build_known_speaker_centroids` + `known_speaker_flags`; Phase 0 und Phase 3 gelten dort damit gleichermaßen.
5. **Continuous-learning pollution loop** — compounds (1)+(3).

### Hard constraint discovered
Two *different* ECAPA implementations coexist:
- **Passive turns** → voice-server ONNX (`voice-server/…/speaker_service.py`, `VOICE_SERVER_URL` active in prod). **All 140 stored embeddings came from this path** (they carry NULL `sample_duration`; the manual path sets it).
- **Manual `/enroll` + SpeakersPage** → backend SpeechBrain (`speaker_service.extract_embedding`).

These almost certainly do **not** share a representation space. **Enrollment must use the SAME model as inference** (the voice-server ONNX), or enrolled profiles will never match live turns. This is load-bearing for the whole redesign.

---

## 2. Design principles

1. **Separate deliberate ENROLLMENT from passive RECOGNITION.** They are one code path today; split them.
2. **Enrolled reference profiles are trusted and immutable-by-default.** Passive turns identify against them but do NOT mutate them (no silent continuous-learning into a reference).
3. **One embedding model everywhere** — enrollment and inference both go through the voice-server ONNX ECAPA.
4. **Quality in, or nothing.** Reject short/noisy/incoherent audio at capture; never store a low-quality embedding into a reference profile.
5. **Unknown = unidentified, not auto-enrolled.** A turn that doesn't match an enrolled profile is "unknown speaker", full stop — it does not mint a polluting profile.

---

## 3. The redesign

### 3a. Enrollment mode (new, deliberate)
A guided, admin/user-initiated flow that builds ONE trusted reference profile per household member and links it to a `users` row.

- **Multi-sample capture:** N clean samples (default 5), each ≥ `speaker_enroll_min_duration_s` (default ~2.0 s of *voiced* audio).
- **Per-sample quality gate:** minimum voiced duration + loudness/energy floor (reject silence/clipping). Reuse `VOICE_MIC_CONSTRAINTS` (noise-suppression on, AGC off) — already used by the SpeakersPage recorder.
- **Cohesion gate (the anti-pollution key):** the N sample embeddings must be mutually consistent — mean pairwise cosine ≥ `speaker_enroll_min_cohesion` (default ~0.5). If the takes don't cohere, the capture is noisy or mixes speakers → **reject the enrollment**, prompt to redo. This prevents a polluted profile *at the source*.
- **Storage:** L2-normalize each accepted embedding, store as the speaker's reference set; mark the speaker **enrolled** + **named** + **linked to a user**. (Enrolled speakers are excluded from the "Unbekannter" auto-numbering.)
- **Model:** compute via the **voice-server** (same ONNX as inference), NOT backend SpeechBrain. New voice-server enroll endpoint or reuse its embed API.

### 3b. Passive recognition mode (changed)
- Identify each turn against **enrolled reference profiles only**.
- **L2-normalize before averaging** the reference set (fix `speaker_resolver`).
- **Margin requirement:** accept a match only if the best score ≥ threshold AND beats the second-best by a margin (`speaker_match_min_margin`) — reduces false matches in the residual overlap.
- **No match → "unknown speaker"** (unidentified). Do **not** auto-enroll.
- **Continuous learning OFF by default** for reference profiles. (Optional, later: a *separate*, quality-gated "adaptation" that only appends a very-high-confidence, quality-passing embedding — never below the enroll bar.)
- **Threshold recalibrated** from real enrolled-profile separation once clean profiles exist (measure same/diff distributions post-enrollment; set the threshold in the new gap). Expected to rise well above 0.25 with clean references.

### 3c. Optional: review-bucket for unknowns (deferred)
Instead of silently minting profiles, a passive turn that fails to match MAY (quality-gated) drop an embedding into a **candidate/review** store the admin can inspect and *promote* to a named enrollment — but it is **never auto-merged** into an enrolled profile. Deferred; only if passive labelling of unknowns proves useful.

### 3d. Clean slate (ops)
The existing 41 profiles are noise-polluted and unrecoverable. **Purge all `Unbekannter Sprecher` speakers** (now possible — delete/merge fixed in `pc20260705`) and re-enroll the 3 members via the new flow. Provide a one-shot admin action / `bin/` script to wipe unknown, unenrolled speakers.

### 3e. Frontend
Extend the existing SpeakersPage record modal into a **guided multi-take enroll flow**: prompt → capture sample → show per-sample quality (duration, level, and running cohesion) → accept/redo → enroll only when N good, coherent samples exist → link to a user. Keep the existing `getUserMedia` + RMS meter.

---

## 4. Config + flags

New settings (`utils/config.py`), all with safe defaults:
- `speaker_controlled_enrollment_enabled` (master flag; when on: auto-enroll OFF, continuous-learning OFF, identify-against-enrolled-only).
- `speaker_enroll_min_duration_s` (~2.0), `speaker_enroll_min_samples` (5), `speaker_enroll_min_cohesion` (~0.5).
- `speaker_recognition_min_duration_s` (~1.0) — passive quality gate.
- `speaker_match_min_margin` (~0.05) — best-vs-second margin.
- Keep `speaker_recognition_threshold` but **recalibrate** post-enrollment.

Flag-off = today's behavior byte-identical (dark rollout).

---

## 5. Phased rollout

- **Phase 0 (safe, no behavior change to matching):** L2-normalize before averaging; add the passive min-duration gate (use the voice-server's `audio_duration_s`, already returned); make continuous-learning require a high-confidence + quality-passing match. Stops the *worst* of the pollution loop with near-zero risk. Ship dark.
- **Phase 1 (enrollment):** voice-server enroll endpoint (ONNX) + backend enroll service (multi-sample + cohesion gate) + `users`-link. Purge script.
- **Phase 2 (frontend):** guided multi-take enroll flow on SpeakersPage; then purge + re-enroll the 3 members.
- **Phase 3 (recalibrate):** measure enrolled same/diff separation; set threshold + margin; flip `speaker_controlled_enrollment_enabled` on (auto-enroll OFF, identify-vs-enrolled-only).
- **Phase 4 (upstream capture — the biggest quality lever; research 2026-07-05):** adaptation / review-bucket; AS-norm if still marginal; and **XVF3800 DSP tuning** (below).

### Phase 4 detail — XVF3800 capture is mis-tuned for biometrics (research 2026-07-05)
We drive the XVF3800 for **LEDs only** and capture **channel 0** (the *conference*-tuned tap: AGC-pumped + DNN-noise-suppressed + dereverbed — optimized for human intelligibility) at **16-bit / 16 kHz mono**, and set **none** of the ~40 DSP params (defaults, never persisted). ECAPA embeddings are biometric and far more sensitive than ASR to time-varying gain (AGC pumping), timbre distortion (aggressive NS), and residual echo/reverb — so we're feeding it the single worst-for-identity tap. This is a prime suspect for the 0.28 same-speaker cosine. All controllable via the existing `xvf_host` + `libcommand_map.so` already in `src/satellite/hardware/xvf3800/` — no reflash needed. Ranked (impact/effort):
1. **Capture the ASR beam (ch 1), not conference ch 0** — or a less-post-processed `MUX_*` tap (`MUX_PROCESSED_MICS`/`MUX_DELAYED_MICS`); the chip has a dedicated ASR-optimized beam (`AEC_ASROUTONOFF`/`ASROUTGAIN`, `AUDIO_MGR_SELECTED_CHANNELS`). High impact / low effort.
2. **Tame AGC on the embedding path** — `PP_AGCONOFF`/`PP_AGCMAXGAIN` (our memory says we RAISED it — wrong for a stable embedding), fix `AUDIO_MGR_MIC_GAIN`, lengthen `PP_AGCTIME`/`AGCFASTTIME`. High / low.
3. **Soften DNN NS** — `PP_MIN_NS`/`PP_MIN_NN` gain floors, `PP_NLATTENONOFF`; preserve voice timbre. High / low.
4. **AEC-with-an-external-loudspeaker → duck the source (BUILT).** Datasheet: the far-end reference must be on ch 0. On-board TTS (`plughw:XVF3800,0`) gives a reference; **routing TTS/media to a DLNA/room speaker leaves the board with NO reference → capture during playback is echo-polluted.** This is a named, well-studied problem, compounded by **sample-rate offset** (the mic ADC and the networked speaker DAC run on independent clocks → any software reference drifts ~a full cycle per ~1000s and the AEC misaligns). The industry answers, ranked by fit for a heterogeneous self-hosted DLNA setup (no synchronized Sonos/AirPlay2 clock):
   1. **Duck / erase the external device's audio while listening** — the dominant *shipping* approach (Amazon/Google patents: "Ducking and erasing audio from nearby devices", "Audio playback settings for voice interaction"). We own the DLNA output layer, so this is orchestration, not DSP. **IMPLEMENTED** as `duck_on_listen` (`ha_glue/services/duck_service.py` + `satellite_manager` hook): duck on LISTENING, restore on IDLE, safety-timeout, dark by default. Since we can't cancel residual (no reference), the duck target is low (`duck_on_listen_volume`, default 20) to actually remove the echo, not just soften it.
   2. **Software reference-AEC with delay + SRO compensation** (Amazon "hybrid reference signal"; arXiv 2507.05399 multi-device AEC) — the "truly cancel it / barge-in over networked speakers" path. Heavy + fragile over DLNA jitter. Deferred.
   3. **Train the models to tolerate playback** (implicit-AEC / playback-augmented data — arXiv 2111.10639, 1808.00563). **We already do this for the wakeword** (renfield_de v4 audiobook hard-negatives); the same augmentation extends to the enroll/embedding path. Cheap follow-up.
   4. **Target-speaker extraction** using the enrolled voiceprint (VoiceFilter, arXiv 2103.08781) — isolate the known speaker from the music before ECAPA. Most powerful, backend-GPU-only, research-grade. Deferred.
5. **DOA-driven beam steering** — read talker azimuth (`AEC_AZIMUTH_VALUES`) and pin a fixed beam (`AEC_FIXEDBEAMS*`), not just LED color. Medium.
6. **24-bit capture** (`USB_BIT_DEPTH`) — more SNR headroom once AGC is softened. Low–med.
7. **`SAVE_CONFIGURATION`** — else every reboot reverts to conference defaults. Trivial enabler.
Sources: XMOS VocalFusion audio-pipeline datasheet; Seeed wiki HA/ESPHome reference (`noise_suppression_level: 0` — trusts the chip, refuses to double-process); the board's own `libcommand_map.so`. Highest-leverage experiment: **capture the ASR/less-processed tap, AGC+NS softened, 24-bit, persisted, playback-gated.**

---

## 6. Decisions (operator, 2026-07-05)

1. **Post-go-live unknowns → quality-gated review bucket** (3c is IN scope, not deferred). A non-matching turn that passes the quality gate drops a candidate embedding into a review store the admin promotes to a named enrollment; **never auto-merged** into a reference.
2. **Enroll embedding source → voice-server ONNX** (required for match compatibility). The backend SpeechBrain enroll path is retired for enrollment (or must be proven identical). Load-bearing.
3. **Enrollment mic → HYBRID.** Close-mic (phone/browser, guided multi-take) seeds a CLEAN reference; optional later adaptation from high-confidence far-field samples (Phase 4). Inference stays far-field.
4. **Household scale = 3** → prefer strict gates (high quality, low recall) over permissiveness.

**Build order confirmed:** Phase 0 first (safe dark fixes), now.

---

## 7. Risks / notes
- Even perfect enrollment can't fully fix *inference* on very noisy far-field audio — but a clean reference profile materially raises same-speaker separation vs today's noisy-reference-vs-noisy-query. If Phase 3 measurement still shows poor separation, the lever moves upstream (satellite noise-suppression / XVF3800 AEC+beamforming before ECAPA, or a better/enhanced embedding pipeline) — tracked separately.
- The two-ECAPA-model split must be resolved (decision 2) or profiles won't match turns.
- All embedding capture must L2-normalize consistently (store normalized, or always normalize on read).

**Source:** prod investigation 2026-07-05 (analysis scripts, read-only); pipeline map in the same session. Related: `docs/SPEAKER_RECOGNITION.md`, `docs/XVF3800_SATELLITE.md`, `TODOS.md` (Self-Learning follow-ups — the identity gap this unblocks).
