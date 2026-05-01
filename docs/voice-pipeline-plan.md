# Voice Pipeline Plan — Renfield

> **Status:** Living plan. Phase A in flight as PR #509. Phase B/C signal-gated, no firing trigger yet.
> **Audience:** Future maintainers reading the codebase cold; the founder coming back after a Reva-focused stretch; a Reva-side developer figuring out which voice work belongs upstream in Renfield vs. local in Reva.
> **Companion doc:** [`../../reva/docs/architecture/voice-pipeline-enhancements.md`](../../reva/docs/architecture/voice-pipeline-enhancements.md) — same engineering scope from Reva's angle.

---

## 1. Why this doc exists

The Reva team drafted a phased plan to upgrade the Whisper + Piper pipeline. Their doc is excellent and most of the engineering it proposes lives in Renfield by policy ("STT/TTS code lives in Renfield, fixes go upstream, not worked around in Reva"). But the Reva doc is written for Reva's use case (Teams + web chat, technical-vocabulary prospects, push-to-talk minimum). Renfield's use case is different enough that just executing the Reva plan verbatim would over-build for some axes and under-build for others.

This doc captures Renfield's adapted version: which Reva phases translate as-is, which translate with a reframing, which to skip, and what's missing from the Reva plan that Renfield needs.

It is **not** a re-derivation of the Reva analysis. Read the Reva doc for the "what's in the pipeline today" + "what would the engineering look like" sections. This doc is the **selection layer** on top.

---

## 2. Renfield's voice users (one paragraph each)

### Voice satellites — Pi Zero 2 W + ReSpeaker
Wake-word listening on-device (OpenWakeWord), VAD on-device (RMS or Silero), beamforming on-device (Delay-and-Sum for 2-mic, planned 4-mic). Audio uploads to the backend over WebSocket once VAD says the user is done. Backend does STT → agent → TTS → audio comes back. The satellite path's bottleneck is **agent latency + audio transit + STT**, not Piper subprocess cold-start (which is dwarfed by the 4-second budget overall).

### Web chat — kitchen tablet, browser
The family talks to the assistant via the chat UI on a tablet stuck to the fridge. Browser-side audio capture (WebRTC), POST `/voice-chat`, blocking serial round-trip back. **This** path is where the subprocess cold-start hurts proportionally — the 150-300 ms Piper tax is a noticeable percentage of the perceived response latency in conversational interaction.

### Multi-user concurrency
A four-person household with three satellites + a kitchen tablet can produce 5+ concurrent voice requests during dinner prep. Today's non-thread-safe Whisper singleton serializes them.

### Personalization signals available
- `users` table: family member names, per-user preferences (`src/backend/models/database.py`)
- `speakers` table: voice fingerprints, auto-enrolled embeddings (`src/backend/models/database.py`)
- `rooms` table: room names + aliases (Wohnzimmer, Küche, Schlafzimmer) — lives in `ha_glue` (`src/backend/ha_glue/models/database.py`); referenced from the main DB via a loose ID, not a hard FK
- KB top documents per user: needs a per-household ranking service that doesn't exist yet — Phase B would have to introduce one (or v1 can ground the bias on `users.name` + `rooms` only and defer KB-bias to a follow-up)
- Per-user TTS voice choice: NOT yet exposed in UI but the data shape is there (`piper_voice_map` in `utils/config.py`)

---

## 3. Phase mapping (Reva plan → Renfield plan)

### Direct wins — translate as-is

These are pure Renfield-side engineering work that the Reva doc proposes putting in `renfield/`. Renfield benefits identically; Reva gets them via submodule bump.

| Reva # | Change | Why for Renfield |
|---|---|---|
| **P0-1** | `openai-whisper` → `faster-whisper` | Same CPU-bound openai-whisper in `whisper_service.py` today. CTranslate2 backend gets PRD's GPU node without API-contract change. |
| **P0-2** | model size bump | Renfield default is `whisper_model="base"` (`config.py`). Bump to `medium` (CPU fallback) / `large-v3` (PRD with cuda) — same logic. |
| **P0-4** | singleton dedup | Confirmed: `voice.py` instantiates `WhisperService()` directly while `api/websocket/shared.py:get_whisper_service` lazy-creates a second one. Two model loads under load. (Phase A also fixes a third instantiation in `ha_glue/api/websocket/device_handler.py` flagged during code review.) |
| **P1-1** | in-process Piper | `piper_service.py` shells out via subprocess per request. ~150-300 ms cold-start is felt most in the kitchen-tablet web-chat path. |
| **P3-1** | worker pool around WhisperService | More important for Renfield than Reva: a household with 4 satellites genuinely produces concurrent voice requests. |
| **P3-2** | TTS LRU cache | **Bigger win for Renfield than Reva.** Household interactions repeat heavily — "OK", "Erledigt", "Erinnerung gesetzt", "Licht ist an", confirmations on actions. |
| **P3-4** | Kokoro-82M swap | Better German prosody is a daily-driver win for the household. Apache 2.0, ~80 ms latency, multilingual, runs on CPU. Drop-in at the synth layer. |

### Translate with Renfield framing

These map but the *contents* differ from Reva.

| Reva # | Renfield adaptation |
|---|---|
| **P0-3** initial_prompt bias | Reva biases on release/ticket vocabulary. Renfield should bias on **per-household vocabulary**: room names from `rooms`, family member names from `users`, top-N KB document titles, speaker aliases. The plugin hook architecture is identical; the contents come from a different source. |
| **P1-2** streaming TTS | Win for Renfield web chat (kitchen tablet). Less critical for the satellite path where audio is already buffered. Ship for the web chat first. |
| **P1-3** server-side Silero VAD | Useful for the **web-chat path** (browser doesn't VAD reliably). Redundant for the satellite path — Renfield satellite already does VAD on-device. Make it conditional. |
| **P2-3** per-role prompts in `agent_roles.yaml` | Reva uses this for release/Jira/Confluence roles. Renfield can repurpose for **per-user** or **per-room** prompt bias (e.g. kitchen-room context biases toward recipe vocabulary). |
| **P3-3** Prometheus metrics | Yes, but rename `reva_voice_*` → `renfield_voice_*`. Add to `/api/metrics`. |

### Skip for Renfield

| Reva # | Why |
|---|---|
| **P2-1** mic button on `chat.reva.aktivities.ai` | Reva's web UI, not Renfield's. Renfield's chat already has voice. |
| **P2-2** Teams voice messages | Teams transport doesn't exist in Renfield (Reva-only). |
| **P4** end-to-end voice models | Explicitly future-future for both projects. |

### Renfield-specific — missing from the Reva doc

These matter for Renfield but are out-of-scope for Reva by design:

1. **Per-user TTS voice.** Each household member could pick their preferred voice. Reva doesn't have multi-user voice. Builds naturally on the `voice_map` in `piper_service.py`.
2. **Speaker recognition × faster-whisper interaction.** Reva says "speaker-recognition path unaffected." For Renfield (auto-enrollment + continuous learning, max 10 embeddings/speaker) the swap needs verification: confirm `faster-whisper`'s output format still feeds the SpeechBrain ECAPA-TDNN embeddings cleanly.
3. **Backend-side preprocessing for resource-constrained satellites.** Already on `src/satellite/TECHNICAL_DEBT.md` ("Medium priority: Audio Preprocessing auf Backend verschieben"). Pi Zero 2 W can offload noise reduction. Scoped for Phase B (Section 4) — touches the satellite firmware contract, so it shouldn't be smuggled into Phase A's backend-only blast radius.
4. **Wake-word path.** Reva explicitly excludes this; Renfield's satellite already runs OpenWakeWord. Reason the Reva framing of "push-to-talk minimum" doesn't fit Renfield.
5. **Household privacy framing.** Reva's GDPR section talks about individual-performance attribution. Renfield's equivalent: voice metadata of one family member not bleeding into another's `Speaker` profile during auto-enrollment. Already handled at the speaker-service level but worth re-verifying after the swap.
6. **Opus compression for satellite uplink.** Already on the satellite tech-debt list ("~50% bandwidth"), separate from STT/TTS upgrades but in the same neighborhood.

---

## 4. Phased rollout

### Phase A — Backend swap (in flight as PR #509)

Same blast radius as Reva's recommended first slice, plus speaker-recognition verification:

- P0-1 (`faster-whisper`)
- P0-2 (`medium` CPU / `large-v3` GPU)
- P0-4 (singleton dedup)
- P1-1 (in-process Piper)
- + verify speaker-recognition embeddings still align (Renfield-specific risk)

Latency drops from ~4 s to ~1.5 s on PRD; German technical-term WER drops materially. Both projects benefit; Reva picks it up via submodule bump.

### Phase B — Renfield-specific value (1-week PR after Phase A soak)

The household differentiation that Reva doesn't ask for:

- **TTS LRU cache** (P3-2 from Reva) — biggest practical UX win for repeated confirmations. LRU keyed on `hash(text|lang|voice)`; size bound to keep memory predictable.
- **Worker pool** (P3-1 from Reva) — necessary for multi-satellite households. Either an `asyncio.Queue` worker or N model copies sized to GPU headroom.
- **Per-household `initial_prompt` hook** (P0-3 from Reva, adapted) — pull from `rooms` + `users` + KB top titles. Plugin hook so each household auto-derives its own bias string. Important architecture decision: the bias should be per-request, not per-WhisperService-instance, so it can adapt to which family member just spoke.
- **Backend audio-preprocessing offload** — moves noise reduction off Pi Zero 2 W (already on satellite tech-debt list). Reduces satellite CPU pressure under sustained voice use.

### Phase C — Defer, signal-gated

These have clear scope but no firing trigger today. Pull forward only when the signal lands.

- **Streaming TTS** (P1-2 from Reva) for kitchen-tablet web chat — only if family complains about TTS lag in the kitchen.
- **Server-side VAD** (P1-3 from Reva) for web-chat path — only if browser-side endpointing causes missed inputs.
- **Kokoro-82M side-by-side German MOS test** — if family feels Thorsten sounds robotic in daily use. Apache 2.0 + 80 ms + multilingual + CPU-fine make it a strong candidate.
- **Prometheus metrics** (P3-3 from Reva, renamed) — defer until there's a Renfield Grafana dashboard to consume them.
- **Per-user TTS voice** — defer until a family member actually asks for it.

### Phase D — Future / experimental

- End-to-end voice models (Moshi-class, gpt-realtime equivalents) — same future-future bucket as the Reva plan's P4. Re-evaluate when German support matures.

---

## 5. Comparison-first protocol (mirrors Reva's)

Same idea, Renfield-flavored corpus.

### Variants

| Variant | What's swapped |
|---|---|
| A — control | Today's pipeline (`openai-whisper` base, Piper CLI subprocess) |
| B — Phase A | `faster-whisper` + `large-v3` + GPU + dedup singleton + in-process Piper |
| C — Phase B | B plus TTS cache + worker pool + per-household prompt bias |

### Test corpus

A 30-utterance German + English household-native voice corpus:

- 8 smart-home turns ("Mach das Licht im Wohnzimmer an", "Wie warm ist es draußen?", "Spiel die Playlist von Mama")
- 6 KB / second-brain queries ("Was hat der Arzt zu Omas Blutdruck gesagt?", "Wann ist die nächste Inspektion?")
- 6 multi-user / speaker-recognition turns (different family members say the same trigger; verify enrollment + identification)
- 4 room/device entity disambiguation turns ("Ist die Küchenleuchte an?" — must not pick up "Schlafzimmerleuchte")
- 6 turns with kitchen background noise (running water, dishwasher, family conversation in background) to exercise the preprocessor and beamforming

Recordings live under `tests/fixtures/voice/` (to be created when Phase A enters its measurement window). Each utterance has a known-good transcript for WER scoring **and** a known-good speaker-id for identification accuracy scoring.

### Quality dimensions

| Dimension | Measure |
|---|---|
| Word Error Rate (WER) overall | `jiwer` against ground-truth |
| Named-entity recognition | Manual: did the room/family name come through correctly? |
| Speaker identification accuracy | Did auto-enroll create the right number of speakers, and did identification pick the right one on repeat? |
| Tool-call accuracy downstream | Re-run an E2E suite with audio-fed inputs vs. text-fed baseline |
| TTS naturalness (Kokoro test) | Blind A/B/C on 10 sample household responses, household raters, 1–5 MOS |

### Acceptance criteria (Phase A)

Promote to PRD only if all hold:

1. Quality non-regression vs A — overall WER ≤ A; **named-entity WER ≥ 5 percentage-points better** than A. (The Reva sibling doc projects 20–40 pp lift from `base` → `large-v3` on noisy domain audio; 5 pp is the floor below which a marginal improvement isn't worth the model-size + GPU-VRAM cost. If named-entity WER landed at <5 pp better, hold for tuning instead of rubber-stamping.)
2. Latency improvement — STT p50 ≥ 50 % faster than A on PRD's GPU node.
3. Concurrency — 5 concurrent voice requests survive without errors (smoke-tested in Phase A; full worker-pool guarantee comes in Phase B).
4. Speaker-recognition non-regression — auto-enrollment still creates new speakers on first hear; identification accuracy on repeat speakers ≥ A.
5. No regression in the text path — existing E2E markers unaffected.

> **Note on web-chat blocking round-trip.** Phase A does NOT change the kitchen-tablet `/voice-chat` endpoint's blocking serial round-trip; it only makes each leg faster. Streaming TTS (Reva P1-2) is scoped for Phase C, signal-gated on real complaints about TTS lag.

---

## 6. Cross-project coordination

The boundary the Reva doc states is the right one: STT/TTS code in Renfield, integration code in each consumer. Phase A landing in Renfield is therefore a win for both Renfield and Reva. Phase B is where the projects diverge cleanly:

- Renfield gets per-household prompt bias, multi-user TTS voices, household confirmation cache.
- Reva gets per-role prompt bias, Teams transport, web-chat mic button.

Both via the same plugin-hook architecture in Renfield, customized at integration time. **If Phase B requires fork-pressure-inducing patches in Renfield core paths, that's a signal the framework abstraction is breaking** — and the right move is to escalate, not to paper over with branching. See `docs/STRATEGY.md` for the framework-unification strategic frame this connects to.

---

## 7. Out of scope

- **Echo cancellation / diarization** for live meeting scenarios. Renfield has no Teams Calls integration; Reva's doc lists it out of scope too.
- **Wake-word retraining** — covered by `src/satellite/TECHNICAL_DEBT.md` low-priority section, separate work.
- **Multi-speaker simultaneous input** — single-speaker per session for now.
- **Languages beyond `de`/`en`** — `supported_languages="de,en"` is the current commitment.

---

## 8. See also

- [`../../reva/docs/architecture/voice-pipeline-enhancements.md`](../../reva/docs/architecture/voice-pipeline-enhancements.md) — Reva's perspective on the same engineering, written from Reva's use case (assumes the sibling-checkout layout `projects.ai/{renfield,reva}`; link is dead in CI runners that only check out `renfield/`)
- [`../src/satellite/TECHNICAL_DEBT.md`](../src/satellite/TECHNICAL_DEBT.md) — satellite-side audio pipeline future work (Opus, AEC, 4-mic beamforming, backend preprocessing offload)
- [`STRATEGY.md`](./STRATEGY.md) — strategic frame for the Reva-unification thesis that this work tests in practice (lands via PR #508; merge that first, or this link will 404 on `main` until it does)
- `src/backend/services/whisper_service.py` — STT service (modified by Phase A)
- `src/backend/services/piper_service.py` — TTS service (modified by Phase A)
- `src/backend/api/routes/voice.py` — voice API surface (singleton dedup in Phase A)
