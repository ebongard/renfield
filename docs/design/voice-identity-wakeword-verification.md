# Voice Identity — text-dependent wakeword verification + streaming sessions with diarization gating

**Status:** DESIGN, REVIEWED (2026-07-06, `/plan-eng-review` + outside voice — 13 decisions locked, see §5a/§7). **PR 1 built:** P0 fail-loud (`speaker_service.py` refuses in-process embeddings) + C1 binary Opus transport (`network/websocket_client.py`, `audio/opus_codec.py`), C1 active in the household (`SATELLITE_OPUS_ENABLED=true`). A0/A1/C2/C3/A2 not built. Successor/companion to `speaker-enrollment-redesign.md` (Phases 0–3b built, 3 dark) — this doc addresses what controlled enrollment structurally cannot fix.
**Trigger:** full pipeline review + devil's-advocate session 2026-07-06. Controlled enrollment fixes *profile pollution* (the disease found 2026-07-05), but the identity signal itself — one text-independent ECAPA embedding from 1–2 s of far-field command audio — remains near its information floor, and the multi-user room case (wakeword speaker ≠ command speaker) is unrepresentable in the current design.
**Delivery shape (locked):** **PR 1 = P0 + C1** (premise-independent). **A1 builds only after the A0 offline experiment confirms its premise** (PR 2). C2/C3/A2 are roadmap.

---

## 1. Problem — why controlled enrollment is not the endgame

The enrollment redesign attacks the *reference* side (clean, cohesion-gated, immutable profiles). Three residual problems live on the *query* and *authorization* side:

1. **Text-independent matching on short far-field audio is near its floor.** Even with clean references, the query is a single embedding from ~1–2 s of arbitrary-text, far-field, DSP-post-processed audio. Measured: polluted same-speaker cosine 0.275 vs different-speaker p95 0.224 (threshold 0.25 inside the overlap); a *clean continuous* capture reaches 0.70–0.74 cohesion — but a real command turn is neither clean nor continuous. Worse, a household is the adversarial case for ECAPA: genetically related voices (siblings, parent/child) cluster tightly — the same effect that forced the KG person-embedding-match exclusion. `bin/calibrate_speaker_threshold.py` honestly "emits no number when the profiles overlap"; for some households **no threshold exists**. **Caveat (outside voice, accepted):** the 0.275/0.224 numbers were measured on *polluted* profiles; clean Phase-1–3 references (deployed 2026-07-06) may already improve separation materially — which is exactly what the A0 experiment measures before A1 is built.
2. **Enrollment/inference domain mismatch.** Phase 2 enrolls via browser close-mic (`getUserMedia`, NS on / AGC off); inference arrives through a far-field XVF3800 beam with a different channel response and DSP chain. Reference and query live in shifted distributions. Phase 4 DSP softening bought +0.04 cohesion (one speaker, no CI) — it does not close a channel gap.
3. **The multi-user room breaks the identity↔command binding.** The dangerous case is not "two people present" — it is **person A speaks the wakeword, person B speaks the command** (or talks over it). Today one embedding is computed over the whole utterance buffer and whoever it (mis)matches gets that user's permissions. The current design cannot even *represent* "the command came from a different voice than the wakeword". This is also why identity fusion with BLE presence was **rejected as a primary mechanism** (Option B of the review): a presence prior only discriminates in the single-occupant room — precisely the case that needs identity least — and degrades in the family-evening case where confusable voices co-occur. (Presence may return later as a cheap tie-breaker *feature* inside the resolver, never as the deciding factor.)

Secondary, same session: the pipeline is batch push-to-talk (record → 1.5 s silence hangover → one-shot STT → agent → TTS; no follow-up turn without re-waking; barge-in passive), and the transport is base64 PCM inside JSON text frames (+33 % on Pi-Zero WiFi that is documented as flaky).

---

## 2. Design principles

1. **Identify at the highest-SNR, fixed-phrase moment.** The wakeword is the same phrase every time. Scoring a fixed phrase holds phonetic content constant, which should separate better than text-independent matching on short arbitrary audio. **Honesty note (outside voice, accepted):** the v1 scorer is *not* text-dependent modeling — it is the same text-independent ECAPA on a shorter, phrase-constant, channel-constant segment. The gain is constancy, not a new model class; whether it clears related-voice overlap is exactly the A0 question.
2. **Enroll through the same channel you infer through.** Wakeword-verification templates are captured **at the satellites** (the user says the wakeword at the device), killing the close-mic/far-field mismatch by construction. The existing close-mic ECAPA enrollment (Phases 1–2) stays for the text-independent confirmation path.
3. **Diarization provides continuity, not identity.** Online diarization answers "is this still the same voice?" within a session — a local-contrast question it is good at even for similar voices — while verification answers "who is it?" once. Never use a diarization cluster alone to *name* a speaker.
4. **Permissions bind to a verified speaker-turn, not to a session, a room, or a satellite.** A turn carries an identity only under **concurrence** (§3, decision D11): the wakeword verdict and the per-turn ECAPA match must agree positively. Anything less → `user_id=None` (fail-closed, same semantics as today: HA_CONTROL denied when auth is on).
5. **Layered evidence, fail-closed.** Wakeword verification (fixed-phrase) is the anchor; the existing per-turn ECAPA match (text-independent, Phase 3) is the required confirmation. Disagreement OR abstention of the per-turn match downgrades to unidentified — **abstention is not agreement** (this closes the A-wakes/B-commands actuation hole pre-C3).
6. **Measure before building, soak before enabling.** A1's premise is falsifiable offline in a day (A0); after build, a log-only shadow mode scores live detections before the flag ever flips (the enrollment-PSK PERMISSIVE→ENFORCING pattern applied to verification).
7. **Dark by default.** Every phase ships behind a flag; flag-off is byte-identical.

---

## 3. Option A — speaker verification on the wakeword segment

### A0 — offline phrase-separation experiment (GATES the A1 build; locked D12)

Before any A1 code: one household recording session (each member speaks each deployed wakeword ~10×, at 1–2 real satellites; the same session yields the P0 enroll-2nd-member data). `bin/calibrate_speaker_threshold.py --wakeword` (new mode, ~20 min of scripting) scores fixed-phrase same/different separation vs. command-audio separation on the *clean* Phase-3 profiles. Outcomes:
- **Fixed-phrase separation materially better** → build A1 (PR 2) with measured starting thresholds.
- **No improvement / no separation** → A1 is falsified for this household; the roadmap reroutes to C2/C3 (session evidence) and the doc gets a SUPERSEDED-partial banner. The build cost saved is the whole point.
- **Clean Phase-3 alone already separates** → A1 shrinks from rescue to enhancement; prioritize accordingly.

### A1 — backend-side verification (PR 2, built only if A0 passes)

No on-device ML change. Decisions D2–D8 locked the shape:

- **Capture (D2):** a **dedicated, always-on ring buffer** on the satellite consumer thread — `deque(maxlen≈25 chunks ≈ 2 s, ~32 KB)`, own knob `wakeword_capture_seconds`, independent of `vad_gate_preroll_chunks` (the existing `_ww_preroll` is ~320 ms, owned by VAD gating, and stays untouched). Snapshot taken at detection fire; generous margins, server-side energy trim (detection offset differs per wakeword framework).
- **Transport (D14/F7):** when C1 is active, the segment rides the **binary channel** (Opus) referenced from the `wakeword_detected` frame; base64-PCM-in-JSON (~85 KB) is only the non-C1 fallback. The "verdict ~200 ms after wakeword" claim holds only on the binary path.
- **Scoring:** backend forwards the segment to the voice-server, which scores it against the claimed household's `(user, keyword)` template centroids (L2-normalized cosine + margin) and returns `{candidate_user_id, score, margin}`. The contract is `(audio, keyword) → (user, score, margin)`; a true text-dependent verification head is a later upgrade behind the same seam.
- **Timing (D4):** the backend spawns the verify task at `wakeword_detected` and **joins it at `audio_end` with a bounded timeout** (`wakeword_verify_timeout_s` ≈ 1.0). Timeout → unidentified + WARNING + metric; late verdicts are discarded, never applied retroactively. Healthy case: verify completes while the user is still speaking — zero added latency.
- **Fusion (D3 + D11 — CONCURRENCE, single identity):** one `user_id` per turn, granted **only** when wakeword-verify = A **and** per-turn ECAPA = A (positive match, margin-gated). ECAPA abstention or disagreement → `user_id=None`. A wakeword-only verdict is **advisory**: logged, and surfaced as a **suggest-only** label on review-bucket candidates ("this unknown embedding followed A's verified wakeword") that a human confirms — **never auto-accepted** (D14/F4; auto-accept would rebuild the ambient-auto-enroll pollution loop). Turns with **no segment** (button press — `_on_wakeword_detected("button", 1.0)` — and browser turns) have verdict **ABSENT**: identity falls back to per-turn ECAPA alone, exactly today's Phase-3 behavior; absent is never treated as agreement (D14/F8b). Net: **actuation identity can never be weaker than today's bar** — A1 is strictly additive.
- **Enrollment (D5 + D8):** "Sag *Hey Renfield* fünfmal" — guided flow capturing **through the satellite mic** via a new `enroll_wakeword_capture` WS request/response (mirrors `capture_snapshot`/`request_irk_capture`). **Full IRK-pattern gating (D5):** admin-permission (SPEAKERS_ALL) on the API route; bounded capture window (~30 s); a **distinct LED state during capture**; raw audio **discarded after embedding** (never persisted — the snapshot precedent); under PSK enforcement, only enrollment-authenticated satellites accept the request. Implementation **extends `SpeakerEnrollmentService`** (D8) — one shared gate pipeline (duration/cohesion/L2/ONNX-parity) with **per-flow constants (D14/F6):** `wakeword_enroll_min_duration_s ≈ 0.8` (the phrase is ~1 s; the 2.0 s default would reject every take) and a **higher** cohesion bar (fixed-phrase takes cohere trivially more, so the 0.5 default would pass junk). UI reuses the `GuidedEnrollModal` shell with a satellite picker.
- **Storage (D9, amended by D13):** new table, **plaintext at rest** — consistent with `speaker_embeddings` (same DB, same threat model; per-column Fernet here would be decoration while the richer profiles sit plaintext, and would import the destructive `SECRET_KEY`-rotation caveat). Encryption applies at the **A2 push boundary** (template material leaving for satellites — the boundary the IRK pattern actually protects):

  ```sql
  -- migration pc2026xxxx_wakeword_templates
  CREATE TABLE speaker_wakeword_templates (
      id          SERIAL PRIMARY KEY,
      user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      keyword     VARCHAR NOT NULL,          -- model id, e.g. 'hey_renfield'
      embedding   JSONB   NOT NULL,          -- L2-normalized, voice-server ONNX space ONLY
      source_satellite_id VARCHAR NULL,      -- provenance; NULL = migrated/unknown
      created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE INDEX ix_swt_user_keyword ON speaker_wakeword_templates (user_id, keyword);
  ```
- **Keyword scoping + coverage honesty (D14/F5):** templates are keyed `(user, keyword)`; an unenrolled keyword → verification **skipped** (verdict ABSENT), never guessed. With the deployed multi-keyword fleet (`renfield_de`/`en`/`it` + `hey_renfield`), full coverage means enrolling each phrase per person — realistically the household enrolls the 1–2 primary phrases and **effective A1 coverage is a fraction of wakeword turns**, stated here so nobody mistakes A1 for "every turn verified". Also stated plainly: with **one** enrolled wakeword user there is no runner-up, so `wakeword_verify_margin` degenerates to a raw threshold — the regime §1 warns may have no valid value. A1's gates are calibrated for ≥2 enrolled members, same as Phase 3.
- **Shadow mode (D14/F3):** after build, `WAKEWORD_SPEAKER_VERIFY_ENABLED` first runs **log-only**: every live detection is scored and the verdict logged next to the Phase-3 outcome (and manual spot-labels), but no identity decision changes. Flag flips to enforcing only when the soak shows live-detection separation, closing the enrollment-conditions-vs-living-room gap the calibration corpus cannot. This is the PERMISSIVE→ENFORCING pattern from satellite enrollment.

**Why this attacks §1:** fixed phrase (problem 1), satellite-channel enrollment (problem 2), and it produces the *anchor* that C3 needs for problem 3.

### A2 — on-device verification (optional, later, measure first)

Move the scoring onto the satellite (int8 tiny verification model via the existing ONNX runtime): the `wakeword_detected` frame carries `candidate_user_id` directly, no audio round-trip. Per-user templates are pushed to satellites exactly like `ble_known_irks` — **encrypted for the push** (the D13 boundary), only to **enrollment-authenticated** satellites (same trust gate as IRKs, since voice templates are likewise identity material). **Only justified if A1's round-trip latency measurably matters**, and only for satellites with compute headroom (XVF3800-class; the AC108 arecord workaround and the Pi Zero 2 W onnxruntime kernel-panic history say: not the HAT fleet). Default answer: skip until A1 data exists.

---

## 4. Option C — streaming conversation session + online diarization gate

### C1 — transport: binary WS frames + Opus (PR 1, standalone win)

Replace base64-PCM-in-JSON with binary WebSocket frames carrying Opus (16 kHz mono, VoIP profile). ~50 % bandwidth cut was already tracked as P2; it is a *prerequisite* for streaming sessions (C2) and independent of everything else.

- **Decode seam (D6, AMENDED 2026-07-07 — decode on the voice-server, NOT the backend):** the satellite sends binary Opus frames (1-byte type prefix + session tag → demux). **Opus→PCM decode is media processing and belongs on the voice-server**, the component that already owns audio DSP (Phase B moved STT/TTS/embeddings off the backend precisely so the backend is an *orchestrator*, not a media processor — and the voice-server already ffmpeg-decodes webm/opus for the browser `/ws/voice` path, so decode already lives there). Putting satellite-opus decode in the backend would (a) re-introduce media work into the orchestration layer, (b) split "decode opus" across two components by client type, and (c) have to be *moved again* for C2 (which streams satellite audio to the voice-server anyway). Target: the backend forwards the Opus bytes to the voice-server (short-term: `/api/voice/stt` accepts opus, or a small new endpoint), and long-term the satellite audio path re-points at the voice-server's streaming endpoint — i.e. **C1 becomes the on-ramp to C2**, not a backend detour.
  - ✅ **DONE (C2 Phase 1, 2026-07-07):** decode moved to the voice-server. The backend `ha_glue/services/opus_transport.py` is now a pure wire-format module (parse/build binary frames only — no `opuslib`/libopus0 in the backend image); `satellite_manager` buffers the raw `[uint16 len][packet]` blob and `satellite_handler` forwards it to the new voice-server `POST /api/voice/stt-opus`, where `voice_server/services/opus_decode.py` owns the one-shot opuslib decode → float32 mono 16 kHz PCM. Downstream of decode (STT, speaker resolver, A1 segment) is PCM regardless of where decode happens. Still **dark** (`SATELLITE_OPUS_ENABLED`; no satellite negotiates opus yet), but the layering debt is cleared — decode now lives on the media layer alongside the browser `/ws/voice` ffmpeg path, so C1 is the on-ramp to C2 as intended.
- Control frames stay JSON. Capability negotiation via `register` (`audio_codec: opus|pcm`) so a mixed fleet keeps working indefinitely; satellite Opus-encode CPU on Pi Zero 2 W is measured before fleet rollout (§8).

### C2 — streaming session (roadmap; follow-ups + active barge-in)

After the wakeword, the satellite streams continuously for a **bounded session window** instead of record-until-silence-then-stop:

- **Session envelope:** `session_start` (on wakeword) → continuous audio → per-turn endpointing *server-side* (streaming STT emits turn boundaries) → `session_end` (hard cap `session_max_seconds`, or `followup_window_s` of post-TTS silence, or an explicit stop word). LED stays in a distinct session color the whole time — **the mic being live must be visible** (see §6 privacy).
- **Follow-up turns:** after TTS, the session stays open `followup_window_s` (~6–8 s); speech in that window is a new turn without re-waking. This removes the batch pipeline's biggest UX tax.
- **Active barge-in:** speech during TTS playback → backend sends an explicit `barge_in_ack`, satellite stops playback and flips to LISTENING (today's passive VAD-flag approach becomes an actual protocol event). Composes with `duck_on_listen`.
- **STT seam (D7, locked):** C2 **extends the voice-server's existing `/ws/voice` streaming endpoint** (`voice-server/voice_server/api/ws_voice.py` — the Phase-B browser voice path, with session handling, cancel-ack, and session-cap tests already in place). No parallel satellite streaming endpoint; satellite session semantics (follow-up window, barge-in ack) are reconciled into that one contract, and browser + satellite voice converge on it. The one-shot `/api/voice/stt` stays for non-upgraded paths.
- **Transport topology (D15, locked 2026-07-07 — backend-mediated):** the satellite keeps its **single** WS to the backend; the backend **proxies** the opaque opus frames on to the voice-server's `/ws/voice`, and the streaming transcript + turn boundaries + identity come back to the backend, which orchestrates the turn (agent, TTS, `barge_in_ack`). This extends exactly what C2 Phase 1 already does (backend forwards the opus blob) into a streaming session. **Not a D6 backslide:** the backend relays *compressed, opaque* frames (cheap I/O) — decode/STT/diarization stay on the voice-server; D6 was about *media processing* on the wrong layer, not byte transit. Rejected alternative — **direct** satellite→voice-server audio — is architecturally purer (audio never transits the orchestrator) but costs a 2nd satellite connection (Pi-Zero-unfriendly) and a new voice-server→backend transcript/identity push channel with its own auth + session correlation; and the transcript must NOT relay back through the satellite (explicitly rejected). Revisit only if a satellite ever needs voice without the backend reachable.

### C3 — online diarization + the cluster-binding gate (roadmap; closes §1 problem 3)

Run online diarization (pyannote streaming) over the whole session **including the wakeword segment**. The verified wakeword (A1) anchors one cluster to a user. Then, per turn:

- Turn's dominant cluster == the anchored cluster → the turn inherits the verified identity and its permissions.
- Different cluster → the turn is **unidentified** (fail-closed; the other person can say the wakeword themselves and anchor their own cluster — two people can hold interleaved, correctly-attributed conversations with one satellite).
- Overlapped/ambiguous attribution → unidentified. Never guess.

This is the structural fix for wakeword-speaker ≠ command-speaker, and it upgrades speaker detection from "one 2 s embedding" to "cluster evidence accumulated over 10–30 s of session audio". C3 is also where the **concurrence rule relaxes**: with cluster continuity binding the command to the verified wakeword speaker, wakeword-anchored identity can extend to turns where per-turn ECAPA abstains — the UX the pre-C3 rule deliberately forgoes. (The rejected dual actuation/personalization identity idea from the original draft lives here too, if ever.) **Shared investment:** the same pyannote-on-voice-server stack is the headline missing feature of the xidra business instance (meeting diarization, `docs/private/BUSINESS_INSTANCE_XIDRA_PLAN.md` §2) — co-schedule; offline/batch meeting endpoint first (de-risks model + GPU budget on gpu-3), online second.

---

## 5. Phased rollout (locked shape: D1 revised by D12)

| Phase | Contents | Flag | Depends on |
|---|---|---|---|
| **P0 (prereq, immediate)** | ✅ Fail-loud built: the backend SpeechBrain embedding fallback **refuses** cross-space embeddings when the voice-server is down (`speaker_service.py`, log + skip speaker resolution, never store). Open: A/B the XVF3800 ASR-tap vs far-field wakeword recall (A1 makes the wakeword segment load-bearing); enroll a 2nd household member. | — | — |
| **C1 (PR 1, with P0)** | ✅ Built: binary WS frames + Opus, capability-negotiated. Resolves the TODOS.md Opus P2. Decode: ✅ moved to the voice-server (`/api/voice/stt-opus`, C2 Phase 1, 2026-07-07) — D6 debt cleared. **Active in the household** (`k8s/configmap.yaml` `SATELLITE_OPUS_ENABLED=true`). | `SATELLITE_OPUS_ENABLED` | — |
| **A0 (experiment, ~1 day)** | Household wakeword-recording session + `calibrate_speaker_threshold.py --wakeword`; measures fixed-phrase vs command-audio separation on clean profiles. **Go/no-go for A1.** | — | P0 (same recording session) |
| **A1 (PR 2, only if A0 passes)** | Ring buffer + segment transport; voice-server scoring; satellite-mic enrollment (IRK-gated); concurrence fusion; suggest-only bucket labels; shadow mode → enforcing. | `WAKEWORD_SPEAKER_VERIFY_ENABLED` (log-only first) | P0, C1, A0 |
| **C2 (roadmap)** | Streaming session extending `/ws/voice`, server-side endpointing, follow-up window, active barge-in, session LED + hard cap. | `STREAMING_SESSION_ENABLED` | C1 |
| **C3 (roadmap)** | Online diarization + cluster-binding gate; concurrence-rule relaxation. Co-scheduled with xidra diarization (offline first). | `DIARIZATION_GATE_ENABLED` | A1 + C2 |
| **A2 (optional)** | On-device verification, IRK-pattern encrypted template push (enrolled satellites only). | `WAKEWORD_VERIFY_ON_DEVICE` | A1 latency data says it's needed |

New config (all defaults preserve today's behavior): `wakeword_capture_seconds` (~2.0), `wakeword_verify_timeout_s` (~1.0), `wakeword_verify_threshold`, `wakeword_verify_margin`, `wakeword_verify_min_templates` (~5), `wakeword_enroll_min_duration_s` (~0.8), `wakeword_enroll_min_cohesion` (higher than the 0.5 speaker default), `session_max_seconds` (~60), `session_followup_window_s` (~7), `diarization_min_turn_confidence`. Thresholds are **calibrated (A0) and soaked (shadow mode), never guessed**.

### 5a. Engineering-review decision log (2026-07-06)

| # | Decision |
|---|---|
| D1/D12 | PR 1 = P0 + C1; A1 = PR 2, gated on the A0 offline experiment |
| D2 | Dedicated always-on ~2 s ring buffer (`wakeword_capture_seconds`), VAD pre-roll untouched |
| D3+D11 | Single identity per turn; **concurrence required** (wakeword-verify AND positive per-turn ECAPA); abstention ≠ agreement; wakeword-only verdicts advisory/suggest-only |
| D4 | Verify future joined at `audio_end`, bounded `wakeword_verify_timeout_s`; timeout → unidentified; late verdicts discarded |
| D5 | Enrollment capture fully IRK-pattern gated (admin route, bounded window, LED, audio discarded, PSK-enrolled satellites only) |
| D6 | ~~Opus decodes at the backend edge~~ **AMENDED 2026-07-07:** decode belongs on the **voice-server** (media layer), not the backend (orchestration layer). **RESOLVED (C2 Phase 1, 2026-07-07):** decode moved to the voice-server (`/api/voice/stt-opus` + `voice_server/services/opus_decode.py`); backend `opus_transport.py` is now wire-format-only and the backend image dropped opuslib/libopus0. See §4 C1. |
| D7 | C2 extends the existing `/ws/voice` streaming endpoint |
| D15 | **C2 transport = backend-mediated** (2026-07-07): satellite → backend (single WS, proxies opaque opus frames) → voice-server `/ws/voice`; transcript/turns/identity return to the backend orchestrator. Backend relays compressed frames only (not a D6 backslide). Direct satellite→voice-server rejected (2nd connection + S2S push channel; no transcript relay through the satellite). See §4 C2. |
| D8 | Wakeword enrollment extends `SpeakerEnrollmentService` (shared gate code, per-flow constants) |
| D9/D13 | Schema specified (above); **plaintext at rest** like `speaker_embeddings`; encryption at the A2 push boundary |
| D14 | Shadow/soak mode before flip; suggest-only bucket labels; coverage/margin honesty documented; per-flow gate constants; segment rides the C1 binary channel; button/browser = verdict ABSENT |

---

## 6. Security & privacy model

- **Authorization semantics unchanged at the gate:** identified user → their RBAC permissions; unidentified → `user_id=None` (HA_CONTROL denied when auth is on). The concurrence rule (§3) guarantees **actuation identity is never weaker than today's Phase-3 bar** — a replayed wakeword plus an unknown command voice fails exactly as it does today. Voice remains a weak single factor against deliberate replay/cloning of *both* segments; actions above household-tier should continue to rely on RBAC, not voice alone.
- **Enrollment capture is a remote-mic primitive and is gated like one (D5):** admin-permission route, bounded window, distinct LED state during capture, raw audio discarded post-embedding, and under PSK enforcement only enrollment-authenticated satellites honor the request.
- **Template storage (D13):** plaintext at rest, deliberately consistent with `speaker_embeddings` (encrypting only the new table while richer profiles sit plaintext in the same DB under the same `SECRET_KEY` would be decoration, and imports the destructive key-rotation caveat). The boundary that gets encryption is the **A2 satellite push** (IRK pattern) — where template material actually leaves the backend's trust domain.
- **Streaming privacy (C2/C3):** the mic is live longer than one utterance. Mandatory mitigations: distinct session LED state (the LED-ring colour language extends, kiosk mirrors it), hard `session_max_seconds` cap enforced **satellite-side** (never backend-trusted), stop-word exit, per-satellite opt-out (`session_mode: off`), and **session audio is never persisted** (raw audio discarded after STT, as today).
- **No new actuation paths:** all commands still flow through the existing intent/agent pipeline and its permission gates.

---

## 7. Open decisions (operator)

1. **A1 template scope: per-satellite or pooled?** Pooled per-user templates, seeded at the 1–2 most-used satellites, is the working proposal; per-satellite refinement only if A0/shadow data shows channel variance matters. *(Still open — decide at A1 build.)*
2. ~~Disagreement policy strictness~~ — **RESOLVED (D3+D11):** concurrence for identity; no dual actuation/personalization identity pre-C3; wakeword-only verdicts advisory.
3. **C2 session scope:** all satellites or XVF3800-class first? Proposal: XVF3800-class first (hardware AEC; HAT satellites gain least). *(Open — C2 is roadmap.)*
4. **C3/xidra sequencing** — **RESOLVED direction:** offline/batch meeting diarization first (de-risks model + GPU budget), online gate second.

---

## 8. Risks / notes

- **A0 may falsify the premise** — that is its job. The fallback roadmap (C2/C3 session evidence) is stated in §3 A0. Related honesty: v1 scoring is constancy-boosted text-independent ECAPA, not true text-dependent modeling (§2 principle 1).
- **Wakeword segment quality varies with detection timing** — the detector fires at different phrase offsets per framework (openwakeword vs microWakeWord). Mitigate: generous ring-buffer margins (~2 s) + server-side energy trim.
- **Calibration-vs-live domain gap** — enrollment takes are deliberate and quiet-room; live detections are across-room with the TV on. The shadow/soak mode (D14/F3) exists precisely because the A0/enrollment corpus cannot represent live conditions; the flag flips on soak data, not calibration data.
- **Coverage is partial by design** (D14/F5): unenrolled keywords and single-enrolled-user margins are documented degradations, not surprises. Do not read A1 as "every wakeword turn verified".
- **Pi Zero 2 W compute** is a recurring trap (AC108 kernel panic, openwakeword inference budget). Every satellite-side addition (Opus encode, A2 inference) needs an on-device measurement gate before fleet rollout.
- **Streaming sessions change the failure surface:** a wedged session must never leave the mic streaming (hard cap is a *satellite-side* timer) or the DLNA volume ducked (existing duck safety-timeout covers this).
- **Fleet heterogeneity:** every protocol change (C1/C2) is capability-negotiated at `register`; an un-upgraded satellite keeps the batch JSON/PCM path indefinitely (regression-tested, see test plan).
- **C1 decode location (D6) — RESOLVED (C2 Phase 1, 2026-07-07):** decode moved off the backend WS handler onto the voice-server (`/api/voice/stt-opus`), so opus decode now lives on the media layer alongside the browser `/ws/voice` ffmpeg path. The backend is a pure packet forwarder; C1 is the on-ramp to C2. No longer a blocker on fleet rollout.
- Diarization on far-field single-array audio has its own error modes (overlapped speech, similar voices) — the gate's answer to ambiguity is always "unidentified", never a guess; UX cost is a re-wake, not a mis-attribution.

**Source:** pipeline map + speaker-recognition review + devil's-advocate session 2026-07-06; `/plan-eng-review` (13 decisions, §5a) + cold-context outside-voice challenge (8 findings, all arbitrated) same day. Related: `docs/design/speaker-enrollment-redesign.md`, `docs/VOICE_PIPELINE_DESIGN.md`, `docs/XVF3800_SATELLITE.md`, `docs/private/security/satellite-trust-design.md`, `docs/private/BUSINESS_INSTANCE_XIDRA_PLAN.md` §2, `TODOS.md` (Opus/bandwidth P2 — resolved by C1; browser TTS barge-in RFC).
