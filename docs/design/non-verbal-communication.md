# Non-Verbal Understanding (gesture language + body-language reading)

Status: **PROPOSED / DESIGN** — not implemented, not scheduled. Scoped 2026-06-27.
No code exists for any part of this; the repo has **zero** prior mention of
gesture / pose / affect / non-verbal detection (verified by sweep). This is a
**re-architecture** toward maximal capability, not an extension of the existing
single-frame occupancy-counting vision.

**Goal (user directive):** Renfield should understand **gesture language to the
largest feasible extent** on self-hosted hardware — specifically a **rich,
expandable vocabulary of command gestures** *and* **continuous body-language
reading** (posture, engagement, attention, affect) fed to the agent as context.
**Sign language (DGS/ASL) is explicitly out of scope** — see "The honest ceiling."

## What "largest feasible extent" actually requires

The original minimal scope (single `rpicam-still` JPEG → VLM) **cannot do gesture
language at all** — a language is *temporal* (movement, sequence) and single static
frames give you only "thumbs-up held still." Maximal capability needs four things,
each a real change:

1. **Continuous video, not snapshots.** A libcamera **video pipeline** on the
   satellite, replacing the one-shot `rpicam-still` path.
2. **A landmark + temporal-model pipeline.** The standard, well-trodden stack:
   **MediaPipe Holistic** (21×2 hand + 33 pose + face-mesh landmarks per frame) →
   a **temporal model** (Transformer/LSTM over landmark *sequences*) → gesture
   classification + body-language encoding.
3. **Inference on capable hardware.** A Pi Zero 2 W (quad A53, 512 MB) cannot run
   this in real time. Two tiers (below).
4. **House-wide camera coverage.** Half the satellites have no camera today.

## Hardware capability finding (researched 2026-06-27, not assumed)

**Allwinner A733** (Orange Pi Zero 3W — the Esszimmer board, `docs/design/ble-presence-improvement.md:4`):

| Spec | Value | Implication |
|---|---|---|
| CPU | 2× Cortex-A76 + 6× Cortex-A55 @ 2.0 GHz | A76 cores ≈ Raspberry Pi 5 class for single-thread |
| NPU | **3 TOPS INT8** (INT16/FP16/BF16), Imagination BXM GPU | The real unlock for accelerated on-device landmark inference |
| RAM | up to 16 GB LPDDR5 | Comfortable headroom for model + buffers |
| Radios | WiFi 6, BT 5.4 LE | — |

**MediaPipe performance reference points (researched):**
- MediaPipe Hands is designed for **30 Hz CPU-only, no GPU** on mobile-class SoCs.
- Full 33-landmark *pose* on a Raspberry Pi 5 (4× A76) ≈ **6 FPS CPU-only**.

**Verdict on the user's question — "is the Orange Pi good enough to run it
on-device?":**
- **Yes, meaningfully** — the A733 (A76 cores **+ 3 TOPS NPU**) can do on-device
  landmark extraction. CPU-only MediaPipe Hands runs at a usable rate today; the
  NPU is the performance ceiling **but requires model conversion** to the
  Allwinner/Verisilicon NPU toolchain (stock MediaPipe does not target it — real
  eng work, not plug-and-play).
- **The 3 current camera satellites cannot** — `arbeitszimmer` / `benszimmer` /
  `kinderbad` are Pi Zero 2 W-class (quad A53), too weak for real-time MediaPipe.
- **The Esszimmer A733 has no camera yet** (`k8s/satellite-esszimmer.yaml` mounts
  no `/dev/video*`; the Pi CSI module is unsupported on that board) → needs a **USB
  camera**.

**Conclusion:** gesture-capable satellites should **standardize on Orange Pi Zero
3W + USB camera**. This is what makes "on-device" real.

## Architecture: two-tier hybrid (satisfies on-device + GPU-backend + coverage)

```
 SATELLITE (Orange Pi A733 + USB cam)        BACKEND (GPU node — where voice-server/CUDA already runs)
 ┌─────────────────────────────────┐         ┌──────────────────────────────────────────────┐
 │ libcamera continuous video      │         │                                              │
 │   → MediaPipe Holistic          │ landmark│  Temporal model over landmark sequences      │
 │     (on-device, NPU/CPU)        │ stream  │   ├─► Command-gesture classifier  ──► intent │
 │   → 21×2 hand + 33 pose + face  │ ───────►│   │     (rich, expandable vocab)       (gated)│
 │     landmark coordinates ONLY   │ (tiny,  │   └─► Body-language encoder       ──► {nonverbal_context}
 │   (RAW VIDEO NEVER LEAVES ROOM) │  private)│         (posture/engagement/affect)   (advisory)│
 └─────────────────────────────────┘         └──────────────────────────────────────────────┘

 FALLBACK tier — weak (Pi Zero 2 W) or where on-device infeasible:
   satellite streams compressed video → backend GPU runs MediaPipe + temporal model.
   (Higher bandwidth + raw frames leave the room → less private; used only where needed.)
```

- **Tier 1 (preferred): on-device landmarks.** The A733 runs MediaPipe Holistic
  locally and streams **only landmark coordinates** — tiny bandwidth, and **raw
  video never leaves the room** (a privacy *upgrade* over today's snapshot-to-backend
  model). The GPU backend runs only the lightweight temporal classifier on coords.
- **Tier 2 (fallback): GPU-backend video.** Weak or transitional satellites stream
  compressed video to the GPU node, which runs the full pipeline. Used only where
  Tier 1 isn't available.
- **Two heads, one pipeline.** The same landmark sequence feeds (a) a **discrete
  command-gesture classifier** and (b) a **continuous body-language encoder**.

## Two outputs

### A. Rich command gestures (discrete → action)
- An **expandable vocabulary** (point, swipe L/R/up/down, finger-count, wave,
  beckon, palm-stop, etc.), classified from landmark sequences by the temporal model.
- **Actuation is fail-closed, reusing the device-widget gate verbatim:** a
  recognized command routes through the **same** path as an interactive widget click
  — `HA_CONTROL`-gated in `chat_handler`, **server-side re-validated**, executed via
  an `_HANDLERS`-only internal tool. A gesture **grants nothing the user lacks**
  through the agent; an unidentified-voice / `user_id=None` turn is denied actuation
  when auth is on. No open-ended "gesture → arbitrary service."
- Vocabulary is **config-driven and growable** (gesture → intent mapping in YAML),
  so adding gestures is data + a trained class, not new wiring.

### B. Body-language reading (continuous → context)
- The encoder summarizes posture / engagement / attention / affect into a compact
  `{nonverbal_context}` line injected into the agent prompt alongside `{time_context}`
  (the established per-turn context pattern, `services/daypart_service.py`).
- **Advisory only** — it shapes tone, verbosity, and proactivity (e.g. user
  disengaged → don't interrupt; confused → simplify). It **cannot call a tool or
  change a decision**. Fail-open: no signal → behave exactly as without it.

## The honest ceiling (where "largest extent" stops)

**Sign language (DGS/ASL) is out of scope and will not be promised.** Continuous,
grammatical, two-handed signing with facial grammar is an **open research problem**
even for large labs. *Isolated*-sign recognition over a small fixed vocabulary would
be technically reachable on this stack, but conversational sign-language translation
is not realistically buildable here. The architecture above is deliberately a
**command-gesture + body-language** system, which is the maximal *reliable*
capability. If isolated-sign ever becomes a goal, it slots in as additional classes
in the same temporal classifier — but it is not part of this design.

## Phasing (delivery order — capability is the goal, but ship it in provable slices)

1. **Phase 1 — Pipeline + body-language context (Tier 2 first).** Continuous video
   from one Orange Pi satellite → backend MediaPipe + body-language encoder →
   advisory `{nonverbal_context}`. No actuation. Proves capture, streaming, latency,
   and the agent-context loop end-to-end with zero risk.
2. **Phase 2 — On-device landmark extraction (Tier 1).** Move MediaPipe onto the
   A733 (CPU first, then NPU conversion), stream landmarks only. The privacy +
   bandwidth win; validates the on-device target the user asked about.
3. **Phase 3 — Command-gesture vocabulary + actuation.** Train the discrete
   classifier on a starter vocabulary, wire it through the `device_action`
   fail-closed gate. Grow the vocabulary as config + classes.
4. **Phase 4 — House-wide rollout.** Standardize/upgrade gesture satellites to
   Orange Pi + USB cam; add cameras to `wohnzimmer` / `fitnessraum` / `esszimmer`.

## Decisions

1. **Continuous video + landmark pipeline, not single-frame VLM.** Gesture
   *language* is temporal; the VLM-on-one-JPEG path is abandoned for this feature.
2. **Two-tier hybrid: on-device landmarks (Tier 1) preferred, GPU-backend video
   (Tier 2) fallback.** Satisfies on-device + GPU-backend + coverage simultaneously.
3. **Standardize gesture satellites on Orange Pi Zero 3W + USB camera.** The A733
   (A76 + 3 TOPS NPU) is the minimum board that makes on-device real; Pi Zero 2 W
   cams are Tier-2-only or upgraded.
4. **Landmark-only streaming is the privacy story (Tier 1).** Raw video never leaves
   the room; only coordinates do. Strictly more private than today's snapshot path.
5. **Body-language shapes words, never actions.** Advisory prompt context only;
   fail-open.
6. **Command gestures reuse the `device_action` fail-closed gate.** `HA_CONTROL` +
   server re-validation + `_HANDLERS`-only. A gesture is never a privilege-escalation
   channel. Vocabulary is config-driven/expandable.
7. **Nothing is persisted — hard non-goal.** No raw video, no landmark history, no
   affect/engagement timeline, no mood log. Per-turn / per-window, ephemeral,
   discarded. (Contrast: presence history *is* persisted — non-verbal reads
   deliberately are not. Storing them is surveillance creep.)
8. **`kinderbad` (bathroom) requires a dedicated opt-in.** It does **not** inherit
   the camera/feature flag. Non-verbal inference runs there only behind a separate
   explicit per-satellite flag, off by default, so it can never be enabled by
   accident — regardless of its camera being on for occupancy. (User decision.)
9. **Dark by default, independent flags.** Body-language context and command-gesture
   actuation gate independently; the ambient read can run without ever enabling the
   actuation channel.
10. **Sign language is out of scope** (see "The honest ceiling").

## Starter command-gesture vocabulary (Phase 3)

Concrete, household-relevant, and each maps to a tool that **already exists** — the
gesture classifier emits an intent, the existing internal tool actuates it through
the existing fail-closed gate. Vocabulary lives in config (gesture → intent), so
growing it is data + a trained class, not new wiring.

| Gesture (static or short motion) | Intent | Existing tool / path |
|---|---|---|
| Open **palm-stop** (push toward camera) | Stop current action / cancel pending | media stop + clear pending confirm |
| **Thumbs-up** | Confirm pending action | confirm the staged `device_action` / Paperless card |
| **Thumbs-down** | Reject pending action | cancel the staged confirm |
| **Wave** | Wake / "I'm talking to you" (touchless attention) | Phase-4 candidate; see touchless-wake note |
| **Swipe left / right** | Previous / next track | `internal.media_control` (skip) |
| **Palm up–down** (raise / lower flat hand) | Volume up / down in this room | `internal.media_control` (volume) |
| **Point at a lamp + thumbs-up/-down** | That light on / off | `internal.device_action` (light, on/off) |
| **Finger-count 1–5** | Pick option N from a spoken/visible list | confirm-by-index on a pending choice |

Notes:
- **Point-at-device** needs the pose vector resolved to an entity — Phase-3-late /
  Phase-4; the simpler gestures (stop, confirm, skip, volume) ship first.
- Actuation is **identical** to a widget click: `HA_CONTROL`-gated →
  `internal.device_action` / `internal.media_control` re-validate → execute. The
  gesture vocabulary grants **nothing** the user can't already do by voice/tap.

## Body-language taxonomy → agent behavior (Phase 1)

The body-language encoder emits a compact, bounded read; the agent prompt maps each
dimension to a behavior. **Advisory only — shapes wording/timing, never decisions.**

| Dimension | Values | Agent behavior |
|---|---|---|
| **Engagement** | engaged / neutral / disengaged / absent | disengaged → be brief, don't elaborate; absent → defer proactive prompts |
| **Attention (gaze)** | at-device / away | away → it's likely not a turn for *me*; suppress false barge-in |
| **Affect** | neutral / confused / frustrated / pleased | confused → rephrase simpler; frustrated → concise + concrete next step |
| **Proximity / posture** | approaching / seated / leaving | leaving → wrap up; approaching → ready to listen |

Bounded enums (no free-form), discarded each window, fail-open on low confidence.

## Streaming protocol (WS) — mirrors the existing satellite patterns

New message types, shaped exactly like the existing `capture_snapshot` /
`bt_scan_request` request-response and `ble_known_irks` push patterns
(`satellite_handler.py`):

- **Capability advertisement** — extend the `register` / `register_ack` handshake
  with `gesture_capable: bool` + `gesture_tier: "ondevice" | "video"` so the backend
  knows per-satellite which tier to drive (mirrors how camera/BLE capability is
  already surfaced).
- **Control (backend → satellite):** `gesture_stream_start` / `gesture_stream_stop`
  — gate the pipeline on/off (e.g. only while engaged, or paused by consent control).
- **Tier 1 (satellite → backend):** `landmark_frame` — `{ts, hands[], pose[], face?}`
  coordinate arrays only. Tiny; **raw video never sent.** Batched at the model's
  window rate, not per-frame.
- **Tier 2 (satellite → backend):** `video_chunk` — compressed frames for satellites
  that can't run MediaPipe locally. Higher bandwidth; used only where Tier 1 isn't.
- **Result (backend → agent loop, internal):** recognized gesture → a
  `device_action`-style frame through the **existing** fail-closed gate; body-language
  read → `{nonverbal_context}` injected into the prompt builder.

## Privacy & consent control surface

Continuous in-room vision is a real surveillance surface; the controls are part of
the scope, not an afterthought:

- **Hardware "vision active" tell.** When the gesture pipeline is running, the
  satellite drives a **distinct LED state** (`LEDController.set_pattern` /
  `set_all`, `src/satellite/renfield_satellite/hardware/led.py`) — a non-defeatable,
  always-visible indicator that the camera is processing. Off pipeline → normal LED.
- **Per-room enable** + the **`kinderbad` dedicated opt-in** (Decision 8).
- **"Pause vision"** — a voice command *and* the palm-stop gesture both send
  `gesture_stream_stop` for a cooldown; resumes on explicit re-enable.
- **Landmark-only by default (Tier 1).** Raw video leaving the room (Tier 2) is the
  exception, flagged per-satellite, never the default.
- **Nothing persisted** (Decision 7) — no recording, no review buffer, no history.
- **Household consent** documented as a rollout gate (DE context: in-home camera
  understanding warrants explicit per-person sign-off before Phase 4).

## Hardware & migration plan (Phase 4)

- **Reference gesture satellite:** Orange Pi Zero 3W (A733) + **USB UVC camera**.
  Reuses the proven k8s-pod pattern (`k8s/satellite-esszimmer.yaml`) for arm64, or
  bare-metal Ansible like the Pi sats.
- **Esszimmer:** add a USB camera to the existing A733 pod (mount `/dev/video*`); it
  becomes the first gesture node with no new board.
- **Migrate the 3 Pi Zero 2 W cameras** (`arbeitszimmer` / `benszimmer` /
  `kinderbad`): either upgrade the board to A733-class (Tier 1) or keep them on
  Tier 2 (video → GPU) until upgraded.
- **Add cameras** to `wohnzimmer` / `fitnessraum` for house-wide coverage.
- **GPU node:** the temporal classifier shares the existing CUDA node with
  voice-server — confirm headroom (see Risks).

## Agent integration & routing

- **Command gestures bypass the agent** — like `internal.device_action`, they are
  **frame-dispatched** through the fail-closed gate, *not* agent-advertised tools.
  No new `agent_roles.yaml` entry is needed for actuation; only the gesture→intent
  config map.
- **Body-language is pure prompt context** — injected like `{time_context}`; no tool,
  no role.
- If any *new* agent-callable tool is later added (e.g. "what gestures can I use?"),
  remember the **two-step** registration: `InternalToolService.TOOLS` + `_HANDLERS`
  **and** the role's `internal_tools` list in the **ConfigMap-served**
  `config/agent_roles.yaml` (skipping step 2 = "no tool available").

## Risks / open questions (resolve during build, not now)

- **NPU model conversion.** Stock MediaPipe doesn't target the Allwinner/Verisilicon
  NPU. CPU-only is the proven baseline; NPU acceleration needs a conversion +
  validation spike. Measure CPU FPS before committing to NPU work.
- **Latency budget.** A command gesture must actuate fast enough to feel responsive;
  body-language context must land before generation (or apply to the next turn).
  Measure end-to-end (capture → landmark → classify → act) on real hardware.
- **Training data.** A rich command-gesture classifier needs a labeled
  landmark-sequence dataset (public sets + household-specific capture). Build an eval
  harness (mirror `bin/run_kg_extraction_eval.py` style) before trusting either head.
- **Multi-person frames.** Whose gesture / body language? Likely the dominant /
  attentive person; tie to gaze/engagement to ignore bystanders.
- **Bandwidth (Tier 2).** Continuous video from multiple rooms to the GPU node —
  measure load; Tier 1 (landmarks-only) is the mitigation.
- **GPU-node capacity.** The temporal classifier shares the node with voice-server
  (Whisper/TTS, CUDA). Confirm headroom under concurrent voice + gesture load.
- **Whole-house privacy posture.** Continuous in-room video understanding (even
  landmark-only) is a far larger surveillance surface than on-demand snapshots —
  warrants an explicit household-consent + per-room control story before rollout.
- **i18n** of the injected `{nonverbal_context}` line (DE/EN).

---

## Eng Review Outcomes (2026-06-27, /plan-eng-review)

**Scope:** build the full architecture (MVP-first VLM-burst probe considered and
**rejected**, D1).

**Resolved decisions** (these supersede/annotate the design above):

| # | Topic | Outcome |
|---|---|---|
| D2 | Gesture model | **Spike both** — measure pretrained (MediaPipe Gesture Recognizer + Model Maker) vs. a custom-trained model on real household gestures BEFORE committing to a training pipeline. |
| D3 | GPU node | **Share** the CUDA node with voice-server for now (no cap), **logged as tech debt** (`T-GPU-CAP`). |
| D4 | Actuation attribution | **Identified-user-only**; unidentified frames read-only. *How* a silent gesturer is identified is unresolved → spike (T1). |
| D5 | Tier-2 raw video | **Kept** as fallback; MUST be flagged in the consent UI as "raw video leaves the room" (distinct from Tier-1 landmark-only). |
| D6 | Stream channel | **Separate** gesture WS connection — and it **MUST inherit the satellite enrollment-PSK + fleet-state machine** (security H1) from day one, or it reopens the "LAN device claims a satellite_id" hole, now streaming video. |
| D7 | Misfire safety | Confidence floor + N-frame debounce + per-gesture cooldown + **safe-action allowlist** (no irreversible action via gesture without voice/tap confirm). |
| D8 | Evals | Labeled eval + **accuracy gate for BOTH heads** before actuation is trusted (reuse the `kg_extraction_eval` harness pattern). |
| D9 | Body-language latency | **Background read, apply latest** to the current turn — never block the `done` frame (the follow-up-chips lesson). |

**Outside-voice tension outcomes:**

- **T1 — Attribution: SPIKE before building Head A.** Measure BLE-**single-occupant**
  attribution coverage in the real household vs. the effort of a **face-ID
  subsystem** (enrollment UI, encrypted embeddings, GPU recognition model,
  consent/legal surface). **Head A is blocked until this resolves** — it is the
  gating dependency. Until resolved, Head A must fail-closed (read-only), never guess.
- **T2 — Capture model CHANGED to gesture-GATED.** **Supersedes the "continuous
  video" framing throughout this doc:** CV spins up on a trigger (wakeword /
  raise-hand pre-filter / presence+intent), runs a **bounded window**, then sleeps.
  Kills the 24/7 power/heat/thermal-throttle load on passively-cooled SBCs (the
  Esszimmer node already went NotReady twice under lighter load) and most of the
  always-on-camera consent problem. Accept ~1s spin-up latency (fine for gesture,
  unlike barge-in audio). Read every "continuous" above as "gated, bounded-window."
- **T3 — Body-language (Head B): build Head A first, gate Head B on a real
  read-quality bar before shipping.** Do not ship Head B until its eval clears a
  meaningful threshold. Its "advisory tone-only" steering is still a behavioral
  input (terser → skips a confirmation) that needs an audit trail + consent surface
  if it ships at all.

**Additive findings folded in:**

- **NPU/silicon runtime probe gates hardware standardization** (`T-SILICON-PROBE`).
  MediaPipe does **not** target the Allwinner NPU; it likely runs on the A76 **CPU**,
  worsening the thermal/throughput math. A ~1-week probe ("MediaPipe fps on one A733
  + sustained core temp") must clear BEFORE standardizing the fleet on A733.
- **Hardware migration sequenced AFTER a one-room proof** clears the D8 accuracy
  gate — not in parallel. Avoids re-imaging the fleet for a feature that might not
  clear the bar.
- **Consent UI is on the critical path**, not trailing (DE legal reality for cameras
  in shared space + guests).
- **Custom-model training data reintroduces raw-video-at-rest** — the privacy
  problem Tier-1 was built to avoid. If the custom path is chosen (D2 spike), the
  training-data source / labeling / storage / retention must be designed explicitly.

## NOT in scope

- **Sign language (DGS/ASL)** — open research problem; isolated-sign could slot in
  later as classes in the same classifier.
- **Face-ID subsystem** — built only if the T1 spike shows BLE-single-occupant
  attribution is insufficient; not a v1 commitment.
- **Body-language head (Head B) shipping** — built behind Head A, gated on a
  read-quality eval; may not ship in v1 (T3).
- **Continuous always-on capture** — explicitly replaced by gesture-gated capture (T2).
- **MVP-first VLM-burst probe** — considered and rejected (D1).
- **Multi-person gesture attribution** — out for v1; multi-person rooms are
  read-only until T1 resolves.
- **Pi-Zero-2W on-device landmarks** — those boards are Tier-2 (raw video → backend)
  until upgraded; no on-device CV on A53-class hardware.

## What already exists (reuse, not rebuild)

Reused: the snapshot WS request/response pattern, the VLM, the `LEDController`,
`internal.device_action`/`media_control` + the `HA_CONTROL` gate, the per-turn
`{context}` injection, the **satellite enrollment-PSK / fleet-state machine** (D6),
and the **`kg_extraction_eval` harness pattern** (D8). New build: gated video
pipeline, MediaPipe integration, temporal classifier, the two heads, the separate
gesture WS, consent UI, and (pending spikes) attribution + training-data.

## Failure modes (per new codepath)

| Codepath | Realistic failure | Test | Error handling | User sees |
|---|---|---|---|---|
| Gesture classifier | False positive actuates wrong device | D7 guards + D8 eval | confidence/debounce/cooldown + safe-action allowlist | Reversible action only; no silent irreversible |
| Attribution gate | Multi-person room → can't attribute | T1 spike + security test | **fail-closed: read-only** when not single-identified | Gesture ignored (documented) |
| On-device MediaPipe | A733 can't hit real-time fps / thermal throttle | `T-SILICON-PROBE` | degrade to Tier-2 or disable gesture in that room | Feature unavailable, not wrong |
| Gesture WS | LAN device claims `satellite_id`, streams video | reuse H1 enrollment tests | enrollment-PSK + fleet-state (D6) | Connection rejected |
| Body-language read | Stale/low-confidence read steers tone wrongly | D8 eval + fail-open | omit context on low confidence | No effect (fail-open) |
| Nothing-persisted | A frame/landmark accidentally written | ★★★ invariant test | assert no write path | n/a (invariant) |

**Critical gap:** multi-person-room attribution has no resolved mechanism (T1 spike
pending). Until resolved, Head A MUST fail-closed (read-only) rather than guess —
otherwise it's a silent safety hole.

## Implementation Tasks
Synthesized from this review. Spikes are sequential-blocking gates; build lanes follow.

- [ ] **T-SILICON-PROBE (P1, human: ~1wk)** — satellite — MediaPipe fps + sustained core-temp probe on one A733; **gates fleet standardization**.
- [ ] **T-ATTRIB-SPIKE (P1, human: ~3d)** — backend — BLE-single-occupant coverage vs face-ID effort; **blocks Head A**.
- [ ] **T-MODEL-SPIKE (P1, human: ~1wk)** — backend — pretrained vs custom gesture accuracy on real gestures; resolves D2 + training-data story.
- [ ] **T-GATED-CAPTURE (P1)** — satellite — gesture-gated CV (trigger → bounded window → sleep); replaces continuous.
- [ ] **T-GESTURE-WS (P1)** — satellite+backend — separate gesture WS inheriting enrollment-PSK + fleet-state (H1).
- [ ] **T-MISFIRE (P1)** — backend — confidence floor + debounce + cooldown + safe-action allowlist.
- [ ] **T-CONSENT-UI (P1)** — frontend — per-room consent + Tier-2 "raw video leaves room" flag + pause-vision; critical path.
- [ ] **T-EVAL-HARNESS (P1)** — backend — labeled eval + accuracy gate for both heads.
- [ ] **T-GPU-CAP (P2, tech debt)** — backend — voice-priority cap / preemption on the shared CUDA node.
- [ ] **T-HEADB (P2)** — backend — body-language head behind Head A, gated on a read-quality eval.

**Parallelization:** spikes block everything. After they clear — Lane A (satellite:
gated capture + gesture WS), Lane B (backend: classifier + misfire + eval), Lane C
(frontend: consent UI) are largely independent. Hardware migration is a final lane,
after the one-room proof clears D8.

## Spike Outcomes (2026-06-27)

Three P1 spikes ran (full reports in `docs/design/spikes/`). All three resolve
favorably; the feature is more shippable than the review feared, and Phase 3
re-shapes into a cheap near-term slice + a later motion phase.

### T-SILICON-PROBE → CPU, ~3-5 fps, viable for command gestures
- MediaPipe runs on the **A76 CPU**, **not the NPU** (the Tasks Python `Delegate`
  enum is CPU/GPU only; NPU use = a separate Verisilicon TFLite→NBG conversion port,
  not a v1 flag). Confirms the review's suspicion.
- Estimated **~3-5 fps sustained** (≈half a Pi-5's A76 count) → **plausibly clears
  real-time for command gestures** (static poses + bounded-window motion + debounce),
  marginal for fluid body-language.
- arm64 install feasible via community wheels (PINTO0309/mediapipe-bin); the
  Esszimmer pod `cpu: 2` cap must be raised; node was NotReady so live thermal
  numbers couldn't be taken.
- **Benchmark script written** (`bin/silicon_probe_mediapipe.py`, inline in the
  report). **REMAINING PHYSICAL STEP:** USB camera on the board + run for
  sustained-fps/thermal confirmation **before fleet standardization**.
- `docs/design/spikes/nonverbal-silicon-probe.md`.

### T-ATTRIB-SPIKE → ship on BLE-single-occupant, defer face-ID
- Real data: only **1 of 3 residents is BLE-tracked**, and **zero multi-occupant
  room overlaps in 17 days**. The single-occupant predicate **already exists**
  (`is_user_alone_in_room()`).
- Face-ID ≈ 2-3 weeks + a biometric-consent track + it contradicts the
  nothing-persisted spine — **not justified by a zero-occurrence risk**.
- **Head A is UNBLOCKED** on BLE-single-occupant (multi-person/unidentified =
  read-only), with two **hard conditions**: (1) **per-room full-household BLE
  enrollment is a rollout gate** (an untracked 2nd person can mask attribution — the
  IrkPairing flow already exists, so onboarding not engineering); (2) **confine the
  safe-action allowlist to reversible actions** so wrong-attribution is harmless even
  for an un-enrollable guest. Face-ID stays a scoped, evidence-gated fallback.
- `docs/design/spikes/nonverbal-attribution-spike.md`.

### T-MODEL-SPIKE → STATIC ships now (stock, zero training); MOTION is a later phase
- Reframes D2: the real axis is **STATIC vs MOTION**, not pretrained-vs-custom. The
  stock MediaPipe Gesture Recognizer is **single-frame** (8 labels).
- **STATIC subset ships today, zero training:** palm-stop (`Open_Palm`),
  thumbs-up/down, finger-count 1-2.
- **MOTION (wave, swipe, volume) needs a custom temporal model** (LSTM/Transformer
  over landmark sequences) — trainable on the public **Jester** dataset,
  **landmark-only** so raw video never persists (extract-and-discard at the edge;
  transient frames TTL'd, never in the DB/git). This **neutralizes the
  training-data raw-video-at-rest concern**.
- **Eval harness designed** (`bin/run_gesture_eval.py` + `tests/eval/gesture_eval.yaml`,
  mirroring `kg_extraction_eval`: per-class recall ≥0.90, **false-actuation ≤1% on
  the `none` class**, confusable-pair precision ≥0.95; heads gated independently
  before actuation is wired).
- `docs/design/spikes/nonverbal-model-spike.md`.

### Re-shaped phasing (supersedes the original Phase 3)
- **Phase 3a (near-term, cheap):** STATIC gestures via the stock recognizer +
  BLE-single-occupant attribution + reversible-only safe-action allowlist + the eval
  gate. No training, no raw-video, runs on the A76 CPU. **The genuine first shippable
  slice.**
- **Phase 3b (later):** MOTION gestures via a custom temporal model trained on Jester
  (landmark-only).
- **Hardware standardization** stays gated on the physical sustained-fps/thermal
  confirmation (`T-SILICON-PROBE` remaining step).
- **Body-language (Head B)** remains deferred (T3) behind a read-quality eval.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | issues_open | 8 decisions + 3 tensions resolved, 1 critical gap (T1 attribution) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE VOICE:** Claude subagent (Codex not installed). Surfaced 3 tensions (attribution=face-ID dependency, always-on-camera posture, cut Head B) + additive findings (NPU runtime unconfirmed, gesture-WS PSK, consent on critical path, training-data raw-video). All presented; user resolved each.
- **CROSS-MODEL:** review + outside voice agreed the actuation surface and GPU contention are the high-risk areas; outside voice additionally caught that attribution is an unbuilt subsystem and that capture should be event-gated.
- **UNRESOLVED:** T1 (attribution mechanism) and Head B shipping are deferred to spikes by explicit decision, not silently — both are blocking gates in Implementation Tasks.
- **VERDICT:** ENG reviewed + **all 3 P1 spikes ran and cleared (analysis)**.
  **Phase 3a is the de-risked first slice and is buildable now:** STATIC gestures via
  the stock MediaPipe recognizer (zero training, runs on A76 CPU) + BLE-single-occupant
  attribution (predicate already exists) + reversible-only safe-action allowlist + the
  designed eval gate. Remaining gates before *that* slice ships: nothing blocking the
  build; before **hardware fleet standardization** — the physical A733
  sustained-fps/thermal probe (needs a USB camera); before **Head A actuation in
  production** — per-room full-household BLE enrollment. Phase 3b (motion gestures,
  Jester-trained temporal model) and Head B (body-language) remain deferred behind
  their own gates.

---

## Addendum (2026-08-09) — camera + NPU-rail reconciliation

Two findings from the NPU-vision-offload work update assumptions in this doc. They do
**not** change any decision; they de-risk the hardware + acceleration story.

1. **CSI camera is viable on the A733 — supersedes "needs a USB camera."** This doc
   (2026-06-27) assumed "the Pi CSI module is unsupported on that board → needs a USB
   camera" (§Hardware finding, Decision 3, Phase-4 plan). A camera survey against the
   **actual A733 vendor kernel config** (`linux-sun60iw2-current-a733.config`, 6.6.98)
   found the board **does** expose MIPI CSI (the A733 Orange Pi Zero 3W has 2× 4-lane
   connectors) and that **`CONFIG_SENSOR_IMX219=m` is compiled in** — so an IMX219 CSI
   module works with only a device-tree overlay (host driver in-tree; the pod mounts
   `/dev/video*`+`/dev/media*`, like `/dev/snd` today). USB UVC still works and stays a
   valid Tier-2/transitional option, but **CSI-IMX219 is now the reference** for a
   gesture/vision satellite. Only four sensors have drivers (IMX219, OV13850, GC05A2,
   GC030A) — OV5647 is unsupported. Note the optics tension: occupancy wants wide/fisheye
   + low-light (IMX219-160IR), gesture/expression wants a less-distorted, closer view —
   the board's **two** CSI ports allow one of each, or pick per priority.

2. **The MediaPipe→NPU conversion rail now exists (demonstrated).** §Risks and
   T-SILICON-PROBE note NPU acceleration needs "a separate Verisilicon TFLite→NBG
   conversion port" and is deferred (Phase 3a runs MediaPipe on the A76 CPU). That exact
   rail — ACUITY toolkit (ONNX/TFLite → INT8 NBG) → VIPLite runtime — is now built and
   documented in `prototypes/npu-occupancy/` (for occupancy + object/document
   recognition). When the deferred MediaPipe-landmark NPU port is picked up, reuse that
   rail; it does **not** change the Phase-3a-on-CPU decision.

**Prototype scaffold** for the buildable slices lives in `prototypes/npu-occupancy/`:
`nonverbal_starter.py` implements Phase-3a static gestures (stock MediaPipe, CPU) + the
deferred Head-B facial-expression affect read (FaceLandmarker blendshapes, advisory),
faithful to the decisions above (fail-closed actuation, advisory affect, nothing
persisted, separate PSK-bound gesture WS). It is prototype scaffolding, not wired into
prod — the productization PR is the Implementation Tasks above.
