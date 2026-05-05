# Open Follow-Ups — post llama-server migration

The Dual-GPU-VM plan that originally lived here has shipped — see PR #527 (text-LLM tier on `llama-server-agent` + `llama-server-embed`, `:llama-rc5` in production).

Remaining open items:

---

## 0. Voice-Pipeline Streaming + GPU (top priority)

The current STT→LLM→TTS pipeline is request-response with three sequential blocking stages, all on CPU for the voice ends. Symptom: long answers TTS-timeout in the browser at 30 s, long recordings STT-timeout at 30 s. Architecturally we want:

> **Streaming-first voice pipeline with VAD-driven barge-in, and GPU-resident voice models when hardware allows. Speech-to-Speech as the long-term endgame.**

### Phase A — Streaming-First-Pipeline (2-3 days, no new hardware)

Decided as the next concrete step. User already pushed back on patches (timeout bumps, frontend sentence-chunking) — we go straight to the structural fix.

**Goals:**
- First word audible within 1-2 s regardless of total answer length
- No artificial axios timeouts; the connection stays open for as long as data flows
- STT partial-transcript visibility while user is still speaking

**Backend:**
- New WebSocket `/ws/voice` (bidirectional). Frontend pushes audio chunks; backend pushes partial-transcript events + audio chunks.
- STT: switch from `whisper.transcribe(file)` to `faster-whisper` streaming mode with `vad_filter=True`. Emit `{type: "partial", text: ...}` events as they arrive, then `{type: "final", text: ...}` on end-of-utterance.
- TTS: split incoming text on sentence boundaries, run Piper per-sentence, write each WAV chunk to the WS as `{type: "audio_chunk", bytes: ...}`. End with `{type: "audio_end"}`.
- Same backend pod, same Piper instance, same Whisper instance — no GPU change yet.

**Frontend:**
- New `useVoiceStream` hook to replace the request-response of `useAudioRecording` + the `speakText` REST call.
- Capture: `MediaRecorder` chunked output to the WS.
- Playback: `MediaSource` API with audio chunks appended to a SourceBuffer. Or continuous `AudioContext` BufferSource queue (simpler, slightly higher latency).
- Drop the `_ttsErrorShown` window flag, drop the long-message console.warn — both gone with the new path.

**Out of scope for Phase A:** barge-in (cancelling TTS when user starts speaking again). Mark for Phase B.

### Phase B — Edge STT (offload to client device)

Reduces backend STT load and latency by ~70 % for the macOS browser. Frontend uses `window.SpeechRecognition` (Web Speech API on Chrome/Safari) to do STT in-browser, sends only the final text. Satellites (Pi Zero) keep on-device VOSK or Whisper-tiny.

Decision point: do we trust browser-side STT enough to skip Whisper for that path? For accessibility / multi-language, probably yes for the chat UI on a Mac. Satellites stay backend-driven.

### Phase C — Voice-Tier auf GPU (medium-term, **needs the spare GPU**)

User mentioned an unused GPU. Three places it could land:

1. **As a third k8s node** (`k8s-gpu-3`) dedicated to voice: Piper-CUDA + faster-whisper-GPU + (later) Coqui XTTS-v2. ~150 ms TTS per sentence, <2 s STT for 10 s audio. Cleanest separation; LLM tier untouched. Needs k8s join + driver setup like the existing GPU nodes.
2. **In the existing dual-GPU VM (`k8s-gpu-1`)** — but that node already has both GPUs claimed exclusively by `llama-server-agent`. Adding a third GPU to the same VM and reserving it for voice via taints/tolerations. Cleaner k8s-wise.
3. **Separate dedicated host** outside k8s, accessed via OpenAI-compatible HTTP. Skips k8s overhead but adds an external dependency.

**Action item for evdb: identify which physical machine the spare GPU fits into and how it slots into the k8s topology / VM layout.** Once that's known, write the manifest (`k8s/voice-server.yaml`) similar to `llama-server-embed.yaml`.

### Phase D — Speech-to-Speech-Modell (long-term, watching)

Replace STT+LLM+TTS pipeline with one multi-modal model. Candidates:
- **Moshi** (Kyutai, open-source, ~7 B): full-duplex, on-device-capable, ~300 ms total round-trip, barge-in built in.
- Future Llama-Voice / Qwen-Voice variants if they ship as open weights.

Not actionable today — track upstream releases. When a stable open-weights model fits in 16-32 GB VRAM with acceptable quality, plan migration.

### What we are explicitly NOT doing

- **Bumping axios timeout to 180 s.** Hides the symptom, doesn't fix the architecture. Reverted.
- **Frontend sentence-chunking with N parallel REST calls.** Half-streaming, leaves the pipeline shape unchanged. Reverted.

---

## 1. Reva-Cross-Migration (3-stage convergence)

Reva (`/Users/evdb/projects.ai/reva`) consumes Renfield as a git submodule pinned at `48e493e` (3 commits behind main, pre-#527). Reva also has its own parallel adapter at `src/reva/llm/openai_client.py` (173 lines) — same Protocol pattern as Renfield's new `OpenAICompatibleClient`, but richer (Anthropic + vLLM backends, file trace logging). Done independently, before the Renfield migration landed.

**Goal:** Converge the two adapters without losing Reva's extra features. Three stages, each its own Reva-side PR. None of these are urgent — Reva works as-is today.

### Stage 1 — submodule bump (5 min)

```bash
cd /Users/evdb/projects.ai/reva
git submodule update --remote renfield
cd renfield && git checkout origin/main
cd .. && git add renfield
git commit -m "chore(renfield): bump submodule to post-llama-server migration"
```

Then run Reva's test suite. Expected breakage: none of substance — Reva's per-role `ollama_url: "http://cuda.local:8081/v1"` workarounds in `config/agent_roles.yaml` still work because Renfield's old code path is unchanged when `LLM_OPENAI_BASE_URL` is unset.

### Stage 2 — fold Reva's `OpenAIClient` into a subclass (1-2 h)

Make `reva.llm.openai_client.OpenAIClient` extend `renfield.utils.llm_client.OpenAICompatibleClient`. Inherits:

- `_OllamaShapedMessage` / `_OllamaShapedResponse` wrappers (no more drift)
- `_options_to_openai`, `_think_extra_body`, `_convert_messages` (so Qwen3 thinking-mode workaround applies to Reva too)
- Tool-call passthrough, embedding `.embedding`-shape

Reva keeps as overrides:

- `_log_llm_exchange` trace logging (file-based observability — not a Renfield platform feature today)
- Multi-backend dispatch (Anthropic via `anthropic_client.py`, vLLM via `vllm_client.py`)

### Stage 3 — drop Reva's per-role `ollama_url` workaround (1 h + Prod-Validierung)

Replace `config/agent_roles.yaml` per-role `ollama_url: "http://cuda.local:8081/v1"` lines with the platform-level `LLM_OPENAI_BASE_URL` env var (same value), and the per-tier `LLM_OPENAI_FOR_*` flags where roles need to opt out individually. Cleaner config model, single point of truth.

Validate against Reva's production cuda.local llama-server before merging.

---

## 2. Vision-Tier (`Qwen3-VL`) wieder aktivieren

`OLLAMA_VISION_MODEL=""` ist im aktuellen Configmap deaktiviert. Drei mögliche Wege:

a) **3. llama-server-Pod auf k8s-gpu-2** (CPU-only) mit `Qwen3-VL` GGUF + mmproj. Vision-Latenz auf CPU: 30-60 s/Bild — nutzbar für sporadische Use-Cases (Satellite-Camera, Paperless-Vision-Fallback).

b) **Reva-Modell auf cuda.local mitverwenden.** Reva hat Qwen3-VL bereits konfiguriert (`config/agent_roles.yaml`). Renfield könnte einen externen Vision-Endpoint via neuer ENV-Variable `LLM_OPENAI_VISION_BASE_URL` ansprechen. Cross-Project-Coupling — sollte mit dem Reva-Eigentümer abgestimmt werden.

c) **Vision streichen** wenn die Use-Cases den CPU-Aufwand nicht rechtfertigen. Satellite-Camera-Roundtrip, Paperless-Audit-OCR-Fallback — beides selten.

Empfehlung: (a) mit kleinem Quant (`Qwen3-VL-4B-Q4_K_M`), separater Pod, eigener PR.

---

## 3. Image-Versionierung

`:llama-rc5` ist ein Release-Candidate-Tag. Sobald das System ein paar Tage stabil läuft:

- `make release` für Versions-Tag (z.B. `v2.5.0`)
- Auf `.159`: das Image als `:v2.5.0` und `:latest` re-taggen + pushen (siehe `deploy-production` Skill)
- `k8s/backend.yaml` von hartcodiertem `:llama-rc5` auf `:latest` mit `imagePullPolicy: Always` zurück, oder auf den Versions-Tag pinnen

---

## 4. Kosmetik: `AGENT_MODEL=qwen3.6` für saubere Logs

llama-server akzeptiert beliebige Tag-Namen, aktuell loggen wir aber `qwen3:14b` als angefordertes Modell — irreführend. `k8s/configmap.yaml` `AGENT_MODEL: "qwen3:14b"` → `"qwen3.6"`. Funktional egal, aber Log-Lesbarkeit besser.

---

## 5. Lokales Repo-Cleanup

- Lokale Branches gone-prune nach Merge: `git fetch -p && git branch -vv | awk '/: gone]/ {print $1}' | xargs -r git branch -D`
- Stash from `fix/frontend-same-origin-default` ist obsolet (das war nur die WIP von der Migration, ist alles gemerged) — entfernen falls noch da: `git stash list | head -3`

---

*Letzte Aktualisierung: 2026-05-04, nach Merge von #526 (frontend-same-origin), #527 (llama-server migration). PR #528 (LLM-client tests) noch offen.*
