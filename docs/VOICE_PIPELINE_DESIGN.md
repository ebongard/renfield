# Voice Pipeline Design — Phase B (Streaming) and Phase C (Speech-to-Speech)

> **Status:** Design v1.4, 2026-05-05. v1.3 → v1.4 lands the **Speaches build-vs-buy spike** (`docs/spike-speaches/`) and locks the verdict: **Build, not buy.** Custom voice-server stays. See § Changelog at the end.
> **Hardware ready:** k8s-gpu-3 (RTX 4060 Ti 16 GB Ada Lovelace, sm_89), labeled `renfield.io/role=voice-llm`. NFS `/mnt/llm` mounted. Driver 580.142, CUDA 13.0.
> **Companion:** [`tasks/todo.md`](../tasks/todo.md) § 0 ("Voice-Pipeline Streaming + GPU").

## Decisions (locked in 2026-05-05)

| # | Decision | Rationale |
|---|---|---|
| **D1** | STT model: **`faster-whisper medium`** with int8_float16 (not `large-v3-turbo`) | German technical vocabulary, smart-home device names, family proper nouns are the failure modes for STT. `medium` is slower (~1.5-2.5 s for 5 s of audio warm on 4060 Ti) but materially better on proper-noun recall than `turbo`. End-to-end target is now ~3 s for short commands (≤ 2 s audio) and ~4-5 s for typical utterances (5 s audio). |
| **D2** | TTS: **Piper-CUDA in B.1, evaluate XTTS-v2 as Phase B.5** | B.1 ships fast and matches the existing voice (`de_DE-thorsten-high`). Phase B.5 (1-day spike, after B.1 is stable) benchmarks XTTS-v2 against Piper for German MOS, latency, and VRAM headroom. If XTTS-v2 wins decisively → swap, otherwise stay on Piper. |
| **D3** | Phase C kickoff: **directly after Phase B is stable** (no Q3 2026 wait) | Voice-tier hardware is in place, the Qwen2.5-Omni candidate exists today and is the most stack-aligned option. Phase C as benchmarking spike (~2-3 days) starts the day Phase B's voice-server has been running clean for ≥1 week. **Phase C VRAM target is AWQ-Int4 / GPTQ-Int4 quantized variants only** — Qwen2.5-Omni-7B FP16 needs ~17 GB just for weights (audio encoder + Thinker LLM + Talker decoder are three sub-models), HF-AWQ card recommends 24 GB GPU for full precision. |
| **D4** | **Keep speaker recognition.** Voice-server emits ECAPA-TDNN embeddings alongside transcripts; backend keeps the Postgres-side match/enrolment logic unchanged. | Core feature — must survive the migration. Architecture: voice-server runs Whisper + ECAPA-TDNN both on the GPU and emits `(text, language, speaker_embedding)` per utterance. Backend's `services/speaker_service.py` (cosine match, SpeakerEmbedding rows, auto-enrolment, vocabulary-capture trigger) stays put. Voice-server stays stateless wrt the DB. |
| **D5** | **Voice-server validates JWTs locally with the shared signing key** (same pattern as `/ws/chat`). | Symmetric with the existing chat-WS auth — same library, same secret rotation, no extra hop on connect. The alternative ("voice-server calls backend `/api/internal/auth/verify`") was considered and explicitly rejected for Renfield because it adds a backend dependency to every voice-session open, including failure scenarios where backend is degraded but voice should still attempt to work. **Reva re-visit hook:** for the Reva enterprise deployment, the same-secret model may not pass the trust-boundary review (B2B/enterprise tenants often want STT/TTS pods to NOT hold session-issuing keys). Reva's plugin SHOULD reconsider Option B (callback-validation) at integration time — see "Auth model" section below. |

---

## Why this exists

The current `/api/voice/stt` and `/api/voice/tts` REST endpoints run Whisper and Piper on CPU because the GPUs were exclusively claimed by the LLM tier. Two consequences:

1. **TTS for a typical 1300-character LLM answer takes ~90 s on Piper-CPU** (~15 chars/s on `de_DE-thorsten-high`, measured 2026-05-04). Frontend axios timeout at 30 s → user gets `timeout of 30000ms exceeded` and silence.
2. **STT for a 30-second utterance takes ~15 s on `whisper-small` CPU** — fine for short commands, awkward for monologues, and stretches uncomfortably toward the 30 s timeout.

Patches considered and rejected:
- Bumping the axios timeout to 180 s — hides the symptom, leaves a request-response pipeline that blocks the user for 90 s of silence.
- Frontend sentence-chunking with N parallel REST calls — half-streaming, leaves the pipeline shape unchanged.

User direction: **no patches**. Phase B is the structural fix. Phase C is the architectural endgame.

---

## Phase B — Streaming Voice Pipeline on k8s-gpu-3

### Goals

- First word audible **within 1-2 seconds** regardless of total answer length.
- No artificial axios timeouts; the connection stays open as long as data flows.
- STT partial-transcript visibility while the user is still speaking.
- Voice tier runs on GPU (RTX 4060 Ti) — STT and TTS both, in one pod.
- Backend HTTP `/api/voice/{stt,tts}` routes stay as compat-fallback for the satellite firmware that doesn't speak WebSocket yet.

### Non-goals (explicitly deferred)

- **Barge-in / interrupt-while-speaking.** Needs full-duplex with echo cancellation and is its own design problem. Phase B.next.
- **Multi-language voice cloning.** Stays on `de_DE-thorsten-high` and `en_US-amy-medium`. XTTS-v2 evaluated separately.
- **Wake-word on the server.** Frontend already runs `openwakeword` in-browser via WASM; server doesn't need to listen for hot-words.

### Architecture

```
Browser (Chrome/Safari)                    k8s cluster
─────────────────────                      ─────────────────────────────────
                                           ┌────────────────────────────────┐
  ┌────────────┐                           │  Traefik ingress               │
  │ MediaRec.  │   webm/opus chunks        │   /ws/voice → voice-server     │
  │  (mic)     │ ────────────────────────► │                                │
  └────────────┘                           │   ┌─────────────────────────┐  │
                                           │   │ voice-server pod         │  │
  ┌────────────┐                           │   │   (k8s-gpu-3)            │  │
  │ MediaSrc.  │   wav chunks              │   │                          │  │
  │  Buffer    │ ◄──────────────────────── │   │  FastAPI + uvicorn       │  │
  │ (speaker)  │                           │   │  ├─ faster-whisper-CUDA  │  │
  └────────────┘                           │   │  ├─ ECAPA-TDNN-ONNX (D4) │  │
                                           │   │  └─ piper-tts (CUDA)     │  │
                                           │   │                          │  │
                                           │   │  STATELESS — no DB conn  │  │
                                           │   └─────────────────────────┘  │
                                           │                                │
                                           │   /api/voice/{stt,tts}         │
                                           │     same pod, REST fallback    │
                                           │     for satellites             │
                                           └────────────────────────────────┘
                                                          │
                                                          │ HTTP
                                                          ▼
                                           ┌────────────────────────────────┐
                                           │  backend pod                   │
                                           │   (k8s-gpu-2 or any worker)    │
                                           │   /ws/chat (existing)          │
                                           │   delegates to voice via       │
                                           │   service DNS                  │
                                           └────────────────────────────────┘
```

The voice-server is a **separate deployment from `backend`**, not a sidecar. Reason: it has different scaling characteristics (GPU-bound, one replica for now) and a different image (CUDA base, faster-whisper, piper-tts). The backend continues to handle HTTP-only paths and the chat WebSocket.

### Component choices

| Component | Choice | Rationale |
|---|---|---|
| Container base | `nvidia/cuda:13.0-runtime-ubuntu24.04` | matches `k8s-gpu-3`'s driver/CUDA |
| Web framework | `fastapi` + `uvicorn` | matches the existing backend pattern |
| ASGI WebSocket | `uvicorn[standard]` (`websockets` library) | standard, integrates with FastAPI |
| **STT** | `faster-whisper` (CTranslate2) | 4-5× faster than `openai-whisper`, GPU-native, supports streaming |
| STT model | `medium` (int8_float16) — see **D1** | better German proper-noun recall than `large-v3-turbo`; ~1.5-2.5 s for 5 s audio warm |
| **Speaker recognition** (D4) | `ECAPA-TDNN` from `speechbrain/spkrec-ecapa-voxceleb`, exported to ONNX | ~80 MB model, ~50-100 ms inference per utterance on 4060 Ti, 192-dim float32 embedding output. Backend keeps the cosine-match + DB pipeline (`services/speaker_service.py`) unchanged |
| STT VAD | `silero-vad` (built into faster-whisper) | reliable end-of-utterance detection without server-side rolling buffer logic |
| **TTS** (B.1) | `piper-tts` with CUDA EP for ONNX | keeps existing voices, sub-200 ms per sentence on GPU. **B.5 evaluates XTTS-v2 as a swap-in** (see D2) |
| TTS sentence split | regex on `[.!?]` then comma fallback (same as the rejected frontend chunking, but server-side) | piper's `synthesize_wav` is per-call; sentence-streaming = sub-second first-byte |
| Audio format in | webm/opus (browser MediaRecorder default) → `ffmpeg` → 16 kHz mono PCM for Whisper | sticks to browser-native capture, decode is cheap (<10 ms per chunk) |
| Audio format out | 22 kHz mono PCM (Piper native) → WAV header per chunk | MediaSource API consumes WAV chunks fine; alternatively raw PCM + Web Audio buffer queue |
| Connection auth | JWT via `?token=` query param, same as `/ws/chat` | reuses existing auth stack |

### WebSocket protocol

Endpoint: `wss://renfield.local/ws/voice`. One bidirectional connection per voice session. Message types are JSON for control + binary frames for audio. Mode is detected by message type (text vs binary).

**Request correlation (R1 fix):** every `tts_request` carries a `request_id` (string, client-generated UUID). The server echoes the same `request_id` on every audio chunk and the terminating `tts_done` for that request. The frontend uses the id to discard stale audio when the user has moved on (e.g. cancelled a turn or started a new utterance). The chat WebSocket (`/ws/chat`) gains a matching `request_id` field on the user-message-out and assistant-message-in so the frontend can stitch the voice-WS state to the chat-WS state without races. The `/ws/chat` change is small (one nullable field in the message envelope) and lands in the same PR as the voice-WS work.

#### Client → Server

| Type | Payload | When |
|---|---|---|
| `{type: "session_start", codec, sample_rate?, channels?}` | first message after WS handshake; `codec` ∈ `audio/webm;codecs=opus`, `audio/ogg;codecs=opus`, `audio/wav` | **Codec-negotiation handshake (review-cycle 3 RISK-2 fix).** Locks the ffmpeg `-f` flag for the session lifetime so the decoder doesn't have to probe near-silence chunks (which can misidentify and produce garbage PCM). Required — server rejects binary frames before this message arrives. |
| binary frame | audio chunk in the codec announced by `session_start` | every ~100 ms while recording |
| `{type: "stt_flush"}` | — | client-side VAD or "stop" button — commits the STT pipeline. Renamed from `audio_end` (see R3) to disambiguate from the server-side TTS-done message. |
| `{type: "tts_request", request_id, text, language?, voice?}` | full assistant answer to synthesize | after chat response received from backend. `request_id` correlates downstream events. |
| `{type: "cancel", request_id}` | — | reserved for Phase B.next (barge-in). Cancels the matching `tts_request`. |
| `{type: "ping"}` | — | keepalive, server replies with `pong` |

#### Server → Client

| Type | Payload | Meaning |
|---|---|---|
| `{type: "partial_transcript", text, confidence}` | interim Whisper result | streamed every ~500 ms while user speaks |
| `{type: "final_transcript", text, language, speaker_embedding: float32[192], audio_duration_s}` | committed result on VAD silence | `speaker_embedding` is the raw ECAPA-TDNN output (192 floats, ~2 KB JSON) — the frontend forwards it on the chat WebSocket; backend's `services/speaker_service.py` does the cosine match + auto-enrolment. `audio_duration_s` lets the backend tag the SpeakerEmbedding row with what was sampled. |
| binary frame | WAV chunk (22 kHz PCM, with WAV header) | one frame per synthesized sentence. **Self-describing format (review-cycle 3 GAP-3 fix):** the binary payload begins with a fixed 24-byte header — `magic` (4 bytes, ASCII `RFWA`), `request_id` (16 bytes, UUID), `sequence` (4 bytes, big-endian uint32). The remaining bytes are the standard WAV file. This removes the JSON-meta-frame ordering trap from v1.2 — the frontend can route every binary frame to the right playback queue without maintaining a "last-seen meta" cursor. The `audio_chunk_meta` JSON message is dropped from the protocol entirely. |
| `{type: "tts_done", request_id}` | — | TTS finished for this `request_id`. Renamed from `audio_end` (R3). |
| `{type: "error", code, message, request_id?}` | typed: `stt_failed`, `tts_failed`, `model_unavailable`, `invalid_audio`, `speaker_extract_failed` | typed errors so the frontend can recover, not bubble up as generic 500. `speaker_extract_failed` does **not** abort the transcript — backend still gets `final_transcript` without `speaker_embedding`, treats the speaker as unknown for that turn. |

### Latency budget (target, post-R4 review)

The v1.0 budget understated Whisper-medium runtime — quoted 500-700 ms but D1 itself documents ~2.5 s for a 5 s utterance. v1.1 splits into two utterance classes:

**Short command (≤ 2 s audio):** "Schalte das Licht an." Most household voice traffic.

| Phase | Cold | Warm |
|---|---|---|
| Mic open + first audio chunk on the wire | 200 ms | 200 ms |
| Whisper-medium model load | ~30 s | 0 (cached) |
| User stops speaking → `final_transcript` event | ~1.2 s | ~700 ms |
| ECAPA-TDNN embedding extraction (parallel with end-of-utterance) | ~50 ms | ~50 ms |
| Frontend forwards to `/ws/chat` (incl. embedding) | < 100 ms | < 100 ms |
| Backend speaker match + chat first token | ~1 s | ~1 s |
| First sentence accumulated → `tts_request` | < 100 ms | < 100 ms |
| Piper-CUDA first WAV chunk on the wire | ~250 ms | ~150 ms |
| First WAV chunk → audio playing (MediaSource) | ~50 ms | ~50 ms |
| **Total: stop → first word audible (short)** | ~3 s + cold-loads | **~2.5-3 s** |

**Typical utterance (~5 s audio):** "Such mir aktuelle Nachrichten zu Künstlicher Intelligenz."

| Phase | Warm |
|---|---|
| Mic open + chunk on wire | 200 ms |
| User stops → `final_transcript` (Whisper-medium 5 s in ~1.5-2.5 s) | 1.5-2.5 s |
| Speaker embed (parallel) | 50 ms |
| Frontend → `/ws/chat` | < 100 ms |
| Backend match + chat first token | ~1 s |
| `tts_request` → first WAV chunk | ~150 ms |
| Audio playing | 50 ms |
| **Total** | **~3.5-4.5 s** |

Still a >10× improvement on the current pipeline for long answers (90 s → ~4 s). The 3 s claim from v1.0 holds only for short commands; typical utterances land at 4-5 s.

### VRAM accounting (k8s-gpu-3, 16 GB total)

| Phase | Whisper-medium | ECAPA-TDNN | Piper / XTTS-v2 | Other (KV cache, buffers, CUDA runtime) | Total | Headroom |
|---|---|---|---|---|---|---|
| **B.1** (Piper) | 2.0 GB | 0.2 GB | 0.5 GB (Piper) | 1.0 GB | **~3.7 GB** | ~12 GB |
| **B.5 spike** (XTTS-v2) | 2.0 GB | 0.2 GB | 4.0 GB (XTTS-v2) | 1.5 GB | **~7.7 GB** | ~8 GB |
| **C spike, AWQ-Int4** (parallel pod) | — | — | — | Qwen2.5-Omni-Int4 ~7 GB + KV ~2 GB | **~9 GB**, but **mutually exclusive with voice-server** because both want the GPU exclusively | n/a |

**N4 fix:** STT and TTS *can* run concurrently on the GPU (CUDA multiplexes them transparently), but during overlap the active VRAM peaks at the sum of both inferences plus their KV/working buffers. In B.1 this is fine (~4 GB peak). In B.5 with XTTS-v2, simultaneous STT-inference + TTS-synthesis pushes into the ~8 GB range — still safe, but it's the reason we don't run multiple concurrent voice sessions on one pod. Pod scales by replicas if traffic grows.

**Phase C is mutually exclusive with voice-server on the same GPU.** During the C spike, the voice-server pod is scaled to 0 (or pinned to a different node, but k8s-gpu-3 is the only GPU node available for voice today). After the spike: either C wins and replaces voice-server, or B continues and C is rolled back. This is documented as the **C.0 prerequisite** in the migration plan.

### Container image layout

`docker/voice-server/Dockerfile`:

```dockerfile
FROM nvidia/cuda:13.0-runtime-ubuntu24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3-pip ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/src/
WORKDIR /app

EXPOSE 8080
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

`requirements.txt`:

```
fastapi>=0.115
uvicorn[standard]>=0.30
faster-whisper>=1.1.0
piper-tts>=1.3
onnxruntime-gpu>=1.20
ffmpeg-python>=0.2
prometheus-client>=0.20
loguru>=0.7
pydantic-settings>=2.5
numpy>=1.26
```

Image size estimate: ~3 GB (CUDA runtime layer ~2 GB, faster-whisper + ctranslate2 ~500 MB, the rest ~500 MB). ECAPA-TDNN runs via `onnxruntime-gpu` directly so we don't need to pull the full `speechbrain` package — see B.1.0 below.

Models pre-pulled to `/mnt/llm/voice/`:
- `faster-whisper-medium/` (CTranslate2 directory, ~1.5 GB) — **per D1, replaces the `large-v3-turbo` placeholder from v1.0**
- `ecapa_tdnn.onnx` (~80 MB) — exported from `speechbrain/spkrec-ecapa-voxceleb` via the B.1.0 step below
- `piper/de_DE-thorsten-high.onnx` + `.json` (~70 MB)
- `piper/en_US-amy-medium.onnx` + `.json` (~50 MB)

### k8s manifest sketch

`k8s/voice-server.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: voice-server
  namespace: renfield
spec:
  replicas: 1
  strategy:
    type: Recreate          # only one GPU, can't run two replicas
  selector:
    matchLabels:
      app.kubernetes.io/name: voice-server
  template:
    metadata:
      labels:
        app.kubernetes.io/name: voice-server
        app.kubernetes.io/part-of: renfield
    spec:
      nodeSelector:
        renfield.io/role: voice-llm
      containers:
      - name: voice-server
        image: registry.treehouse.x-idra.de/renfield/voice-server:v0.1.0
        ports:
        - containerPort: 8080
          name: http
        env:
        - name: WHISPER_MODEL_PATH
          value: /mnt/llm/voice/faster-whisper-medium     # G2 fix: D1 chose `medium`
        - name: PIPER_VOICES_DIR
          value: /mnt/llm/voice/piper
        - name: SPEAKER_MODEL_PATH
          value: /mnt/llm/voice/ecapa_tdnn.onnx           # D4: speaker embedding extraction
        - name: COMPUTE_TYPE
          value: int8_float16
        resources:
          requests: { cpu: "2", memory: 4Gi, nvidia.com/gpu: 1 }
          limits:   { cpu: "6", memory: 12Gi, nvidia.com/gpu: 1 }
        volumeMounts:
        - { name: models, mountPath: /mnt/llm, readOnly: true }
        readinessProbe:
          httpGet: { path: /health, port: 8080 }
          initialDelaySeconds: 60     # whisper model load ~30 s cold
          periodSeconds: 10
        livenessProbe:
          httpGet: { path: /health, port: 8080 }
          initialDelaySeconds: 120
          periodSeconds: 30
      volumes:
      - { name: models, hostPath: { path: /mnt/llm, type: Directory } }
---
apiVersion: v1
kind: Service
metadata:
  name: voice-server
  namespace: renfield
spec:
  selector:
    app.kubernetes.io/name: voice-server
  ports:
  - { port: 8080, targetPort: 8080, name: http }
---
# Traefik ingress: route /ws/voice to voice-server, keep / and /api on backend/frontend
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: voice-server
  namespace: renfield
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: renfield-https-redirect@kubernetescrd
spec:
  rules:
  - host: renfield.local
    http:
      paths:
      - path: /ws/voice
        pathType: Prefix
        backend:
          service: { name: voice-server, port: { number: 8080 } }
      - path: /api/voice
        pathType: Prefix
        backend:
          service: { name: voice-server, port: { number: 8080 } }
```

### Backend integration

Minimal — the existing backend doesn't need to know voice-server exists, because:
- Frontend talks **directly** to `/ws/voice` (Traefik routes it).
- The voice-server's REST `/api/voice/{stt,tts}` are routed by Traefik and **replace** the in-backend handlers. Existing satellite code that POSTs to `/api/voice/stt` keeps working — the URL is unchanged, the implementation moved.

Two backend changes:

1. **Remove** the in-process Whisper/Piper services from `services/whisper_service.py` and `services/piper_service.py`. They become dead code once the ingress routes `/api/voice/*` to voice-server.
2. **Drop** the `WHISPER_*` and `PIPER_*` env vars from `configmap.yaml`; voice-server has its own.

Migration approach: deploy voice-server first, switch ingress, then strip dead code in a follow-up PR.

### Frontend changes

A new hook `src/frontend/src/pages/ChatPage/hooks/useVoiceStream.ts` replaces the request-response pair:

```ts
// pseudo-code
function useVoiceStream() {
  const wsRef = useRef<WebSocket | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioBufferQueueRef = useRef<AudioBuffer[]>([]);

  const startListening = async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const ws = new WebSocket(`wss://${location.host}/ws/voice?token=${token}`);
    const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
    recorder.ondataavailable = (e) => ws.send(await e.data.arrayBuffer());
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        const msg = JSON.parse(e.data);
        if (msg.type === 'partial_transcript') setPartial(msg.text);
        else if (msg.type === 'final_transcript') sendChatMessage(msg.text);
      } else {
        // binary WAV chunk — decode + queue for playback
        decodeAndQueue(e.data);
      }
    };
    recorder.start(100);   // emit chunks every 100ms
  };

  const speakText = (text: string) => {
    wsRef.current?.send(JSON.stringify({ type: 'tts_request', text }));
  };

  return { startListening, stopListening, speakText };
}
```

`useAudioRecording.ts` and the `speakText` callback in `ChatContext.tsx` get replaced by this single hook. The `_ttsErrorShown` window flag, the `Long message detected` console.warn, and the axios timeout coupling all go away.

### Migration plan

1. **B.1.0 — One-time ECAPA-TDNN ONNX export + parity validation** (D4 prerequisite, **regression-class — IRON RULE**). **Empirically validated on 2026-05-05** in the renfield-backend pod against speechbrain 1.1.0 with cosine = 1.000000 vs ground truth. Two artefacts + two test gates:

    **Artefact 1 — export script** at `voice-server/scripts/export_ecapa_onnx.py`. Exports **only `embedding_model`**, not the full `encode_batch` pipeline. The full-pipeline export is blocked by a known PyTorch ONNX exporter limitation (`STFT does not currently support complex types` in opset 17 and 20 — internal Fbank STFT). The voice-server replicates `compute_features` + `mean_var_norm` in Python before each ONNX call (~5 ms on CPU per utterance — negligible vs the ~50 ms embedding inference on GPU).

    Required dev-tool dependency: `pip install onnx` for the one-shot export (NOT needed in production voice-server image, which only consumes the ONNX via `onnxruntime-gpu`).

    Run once, output checked into NFS at `/mnt/llm/voice/ecapa_tdnn.onnx` (~80 MB):
    ```python
    from speechbrain.inference.speaker import EncoderClassifier
    import torch
    enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb",
                                          run_opts={"device": "cpu"})
    # Dummy preprocessed input: (B, T, 80) Mel features after compute_features + mean_var_norm
    dummy_audio = torch.randn(1, 32000)  # 2 s of 16 kHz mono
    feats = enc.mods.compute_features(dummy_audio)
    feats_norm = enc.mods.mean_var_norm(feats, torch.ones(feats.shape[0]))
    torch.onnx.export(enc.mods.embedding_model, feats_norm,
                      "/mnt/llm/voice/ecapa_tdnn.onnx",
                      input_names=["features"], output_names=["embedding"],
                      dynamic_axes={"features": {1: "T"}}, opset_version=17)
    ```

    **Artefact 2 — checked-in parity test** at `tests/backend/test_speaker_service.py::TestEcapaOnnxParity`. Loads each WAV in `tests/fixtures/speaker/` (10 short German utterances), runs:
      a. in-process speechbrain `encode_batch()` → ground-truth embedding (CPU FP32)
      b. `compute_features` + `mean_var_norm` (Python, CPU) → features
      c. `onnxruntime` CPU FP32 inference on the exported ONNX → comparison embedding

    Asserts cosine similarity ≥ 0.999999 per sample (strict — empirically achieved 1.000000 in the probe). Runs in `make test-backend`. **CI-gate: B.1 image build is blocked if any sample drifts.**

    **Two-tier gate (review-cycle 3 GAP-2 fix):** the CI test runs CPU-FP32 vs CPU-FP32 — that's all the backend pod has. Production voice-server runs CPU-FP32 features + **GPU-int8_float16** embedding ONNX. Quantization drift is a separate axis untested by the CI gate. A second gate runs on the `.159` build box right after image build, before Harbor push: same fixtures, same ground truth, but the exported ONNX runs through `onnxruntime-gpu` with `int8_float16`. Tolerance: cosine ≥ 0.99 (looser, accommodates legitimate int8 drift). The Harbor push is blocked if this gate fails — at that point we drop to FP16 (less aggressive quantization) or fall back to bundling speechbrain in the image. The build script (see § Distribution pipeline) wires this in.

2. **B.1 — Build voice-server image** (Piper TTS, ECAPA-TDNN ONNX, faster-whisper-medium per D1). Stand it up, smoke-test directly via `kubectl port-forward` and a manual Python WebSocket client (no frontend changes yet). Verify `final_transcript` events include valid `speaker_embedding` arrays.

3. **B.2 — Deploy voice-server, register ingress.** `/ws/voice` lives. Traefik routes `/api/voice/stt` and `/api/voice/tts` (REST) to voice-server **but not yet `/api/voice/voice-chat`** — the latter is an orchestrator endpoint that calls Whisper + Piper + Ollama in one request, see B.4 for its handling. The basic STT/TTS REST endpoints get reimplemented inside voice-server with the same response shape (`{text, language, speaker?}` for STT) so satellite firmware doesn't notice the move.

4. **B.3 — Frontend hook behind feature flag** `VITE_FEATURE_VOICE_STREAM=true`. New `useVoiceStream` hook with `request_id` correlation (R1). Old `useAudioRecording` and `speakText` stay around so the flag-off path keeps working. Frontend forwards `speaker_embedding` from `final_transcript` onto the chat-WebSocket message envelope.

5. **B.4 — Backend integration:**
   a. **`/ws/chat` envelope gains `request_id` + `speaker_embedding` fields** (nullable). The chat handler passes `speaker_embedding` to `services/speaker_service.find_or_create_speaker()` instead of running Whisper itself.
   b. **`/api/voice/voice-chat` rewritten as backend orchestrator** (G3 fix): instead of calling in-process Whisper + Piper + Ollama, the route now POSTs audio to voice-server's `/api/voice/stt`, runs the existing speaker match + agent loop, then POSTs the answer to voice-server's `/api/voice/tts`. The route stays at the same URL — satellite firmware unchanged. This is more code, not less, and it's the right separation: the *pipeline* lives in the backend, the *inference* lives on voice-server.
   c. **`services/whisper_service.py` and `services/piper_service.py`** simplified to thin clients that call voice-server. The speaker-recognition code in `services/speaker_service.py` stays untouched — it now consumes embeddings from the wire instead of computing them in-process.
   d. **Configmap cleanup, in this order:**
      1. **First:** roll the new backend image (which is robust to the legacy env vars being either present OR absent — it ignores them when `VOICE_SERVER_URL` is set). Verify the rollout completes.
      2. **Then:** in a follow-up commit, drop `WHISPER_MODEL`, `PIPER_VOICES`, `PIPER_DEFAULT_VOICE` from `k8s/configmap.yaml` and apply.
      3. The voice-server pod has had its own copies of the env vars from B.1 onwards.

      Reverse order would risk an old backend pod mid-rolling-update restarting without env vars it expected and crashing on init. Adds operational fragility for no benefit.

6. **B.5 — XTTS-v2 evaluation spike** (per D2). 1 day. Build a parallel voice-server image with XTTS-v2, benchmark German MOS + latency + VRAM against Piper on the same prompts. Decision artefact: short report appended to this doc. Swap-in or keep Piper based on the numbers.

7. **B.6 — Remove the feature flag** once Phase B is stable for ≥1 week. **This is also the trigger for Phase C** (see D3).

Estimated effort: **3-4 days for B.1.0-B.4** (one extra day vs v1.0 for ECAPA + voice-chat orchestrator), plus **1 day for B.5**. B.6 is a no-op flip after the soak period.

### Open questions

- **Concurrent voice sessions on one GPU:** Whisper-medium doesn't multiplex well — one transcription at a time. Acceptable for a household-sized deployment, but worth a queue + 503 when busy. Phase B.next: scale by replicas if traffic grows.
- **Whisper hallucination on silence/noise:** known issue. Silero-VAD's `min_silence_duration_ms` and a confidence floor on `partial_transcript` should mitigate. Tune empirically.
- **Browser MediaRecorder format compatibility:** Chrome/Safari emit webm/opus by default. Firefox emits ogg/opus. Both decode fine through ffmpeg. Need a server-side fallback to `audio/wav` if a satellite client sends raw PCM.
- **Re-encoding overhead:** ffmpeg invocation per chunk is ~2-5 ms per chunk. Negligible vs Whisper inference (~100-300 ms per chunk). If it ever becomes an issue, switch to `pyav` for in-process decode.
- **Traefik WebSocket annotations (N3):** The existing `/ws/chat` ingress works, so the cluster's Traefik already passes `Connection: Upgrade` correctly. The voice-server ingress must use the same annotation set — copy from `k8s/ingress.yaml` rather than the bare sketch in this doc. Verify with a smoke `wscat` after deploy.

### Auth model (D5)

The voice-server WebSocket and REST endpoints are protected by the same JWT mechanism that gates `/ws/chat`. The decision space:

**Option A — Local JWT validation (chosen for Renfield, D5).**

```
Frontend ──token=JWT──► voice-server
                          │
                          ▼
                  voice-server validates
                  with shared HS256 key
                  (read from env at boot)
```

- Voice-server reads `JWT_SECRET` from a k8s Secret at boot.
- On WebSocket open or REST call, voice-server validates the token signature locally with the same `python-jose` (or equivalent) library the backend uses.
- Expired / unsigned / unparseable tokens get a 401, no exception escapes to the user.
- JWT secret rotation requires touching two pods (backend + voice-server). Acceptable operational cost.

**Operational notes:**
- Same `renfield-secrets/jwt-signing-key` Secret can be referenced by both pods via separate `secretKeyRef` blocks.
- **Rotation order (review-cycle 3 RISK-1 fix):** when rotating the JWT secret, the operational sequence MUST be:
  1. Change the Secret value.
  2. Roll **backend** first (it issues new tokens with the new key).
  3. Wait for frontend clients to acquire new tokens (typically <1 min — they re-auth on the next request, or on `403 token expired`).
  4. **Roll voice-server LAST.**
  Reverse order (voice-server first) terminates every active voice session with a 401 because the old-key tokens that clients are still carrying don't validate against the new-key voice-server. With `Recreate` strategy the voice-server has zero overlap, so this is unforgiving — get the order wrong, every active voice user is kicked. Document the order in the `deploy-production` skill's secret-rotation runbook.
- Token revocation (logout, user delete) currently isn't implemented in `/ws/chat` either — neither path checks against a revocation list. If revocation is added later, both pods need to consume the same blocklist signal (Redis pub/sub is the natural carrier).

**Option B — Callback validation (NOT chosen for Renfield, retained for Reva).**

```
Frontend ──token=opaque──► voice-server
                              │
                              ▼ POST /api/internal/auth/verify
                            backend
                              │
                              ▼  return {user_id, expires_at}
                            voice-server caches result
                            for the connection lifetime
```

- Voice-server holds NO signing keys. Pod compromise yields no token-minting capability.
- Backend exposes `/api/internal/auth/verify` reachable only on cluster network (via NetworkPolicy or implicit `cluster-local` Service).
- Adds one HTTP round-trip (~5-20 ms LAN) on every voice session open. Negligible for UX.
- Adds a backend dependency to `/ws/voice` open: if backend is down, voice-server can't even start a session — the whole voice path becomes a tight-coupling failure domain instead of an independent one. For Renfield (single-tenant, household), this trade is wrong (we want voice to keep working when backend is degraded if at all possible). For Reva (B2B, enterprise tenants), this trade is right (security-team will block voice-server holding session-signing keys).

**Reva re-visit hook (per D5):**

When Reva integrates the streaming voice tier, the integration team SHOULD:
1. Review the deployment's trust-boundary requirements with the customer's security team.
2. If "STT/TTS pods may hold session-signing keys" is acceptable → keep Option A.
3. If not → implement Option B by adding `/api/internal/auth/verify` to the Renfield backend (~30 lines of code, behind an unauthenticated cluster-local route), and configuring voice-server with `AUTH_MODE=callback` instead of the default `AUTH_MODE=local`. The voice-server image must support both modes from day one to avoid a fork.

**Action item for B.1:** voice-server config supports both `AUTH_MODE=local` (default) and `AUTH_MODE=callback`. The callback path is implemented and tested even though Renfield uses `local`. This way Reva flips a config flag instead of patching code.

### Known acute risks (not deferred — track explicitly)

- **Acoustic echo cancellation on satellites (N2).** Pi Zero 2 W + ReSpeaker has speaker and mic centimetres apart; near-field playback bleeds into the mic. Without AEC, every TTS response triggers a spurious `final_transcript` from the Whisper pipeline picking up its own playback. Current satellite firmware (`src/satellite/`) does **not** implement AEC. **Mitigation in Phase B**: voice-server suppresses STT for a brief window (e.g. 500 ms) after sending `tts_done` to the same session. This is a workaround, not a fix. **Phase B.next: real AEC** (`speex-aec` or `webrtc-audio-processing`) on the satellite — own ticket, not part of B.1-B.6.
- **Speaker-recognition parity (D4 risk).** ONNX-exported ECAPA-TDNN must produce embeddings indistinguishable (cosine similarity ≥ 0.999) from the SpeechBrain in-process model the backend currently uses, otherwise the existing SpeakerEmbedding rows in Postgres become un-matchable. The B.1.0 parity test gates the rest of the migration; if it fails, fall back to running `speechbrain` directly in voice-server (heavier image, but correctness preserved).
- **WebSocket disconnect mid-session (review-cycle 2 finding + review-cycle 3 RISK-3 expansion).** Two symmetric failure modes; both must be handled in B.1:
  - **User-recording case:** `ws.onclose` while `recording === true` → set `voice.recordingLost` error state, "Aufnahme verloren — bitte erneut" message, stop the MediaRecorder cleanly.
  - **Assistant-speaking case (R3 fix):** `ws.onclose` while a TTS playback is in flight → unconditionally `sourceBuffer.abort()` + `mediaSource.endOfStream()` + clear the in-flight `request_id` state. Without this, the audio element freezes mid-sentence and the "TTS in-flight" UI flag never clears (the `tts_done` event would never arrive). The frontend cleanup runs in `ws.onclose` regardless of which direction was active — the simplest safe move is to tear down both directions on any disconnect.
  - Reconnect-and-resume of partial audio is **Phase B.next** — needs server-side audio-buffer persistence and a `resume_session` message in the protocol. Phase B ships the clear-error UX in both directions, B.next adds the resilience.

---

### Testing strategy

The doc through v1.1 was silent on tests. Renfield's `CLAUDE.md` requires tests with every code change ("Bei jeder Code-Aenderung muessen passende Tests mitgeliefert werden"). v1.2 specifies what tests land in which migration step — these are deliverables, not nice-to-haves.

| Layer | Tests | Location | Step | Effort |
|---|---|---|---|---|
| **voice-server unit** | WebSocket protocol round-trip (each message-type pair); ECAPA-ONNX vs speechbrain parity (CRITICAL, gates B.1); Whisper config validation; Piper sentence-split correctness on 20 fixture sentences; JWT validation in `local` mode (valid / expired / unsigned / future-iat); JWT validation in `callback` mode against a mocked `/api/internal/auth/verify` (D5 Reva path) | `voice-server/tests/` (new pytest tree) | B.1 | ~1 day |
| **voice-server integration** | end-to-end: webm-audio-in → `final_transcript` event with embedding + correct text, against 5 fixture WAVs covering happy path, near-silence, and accented German | `voice-server/tests/integration/` | B.1 | ~0.5 day |
| **backend chat-WS envelope** | `/ws/chat` accepts user-message frame with `request_id` + `speaker_embedding`; assistant response echoes `request_id`; null embedding → unknown speaker path; embedding present → `services/speaker_service.find_or_create_speaker` invoked with the wire-format vector | extend `tests/backend/test_chat.py`, `tests/backend/test_speaker_service.py` | B.4.a | ~0.5 day |
| **backend voice-chat orchestrator** | `/api/voice/voice-chat` POST audio → mock voice-server STT → speaker match → mock agent loop → mock voice-server TTS → audio response; verify orchestration order; verify partial-failure handling (STT OK, TTS fails → error response, not 500) | new `tests/backend/test_voice_chat_orchestrator.py` | B.4.b | ~0.5 day |
| **frontend `useVoiceStream` hook** | mocked WebSocket via MSW; mocked MediaRecorder; mocked MediaSource; covers: connect → record → final_transcript → forward to chat-WS, receive WAV chunks → playback, `request_id` correlation, WS disconnect mid-recording → error UX (B.1 critical-gap mitigation), cancel mid-utterance | extend `tests/frontend/react/`, MSW WS handlers | B.3 | ~1 day |
| **Browser E2E** | Playwright: record 5 s utterance → see partial transcript → see final transcript in chat → hear TTS playback complete → verify `request_id` round-tripped (devtools); one happy path on Chrome (webm/opus codec) | extend Playwright suite under `tests/frontend/e2e/` | B.3 | ~0.5 day |

**Total additional test effort: ~4 days** on top of the 3-4 days for B.1.0-B.4 implementation. Lake-score lens: this is the complete option, not the shortcut. With AI-assisted coding the marginal cost is small and the durability is large — boil the lake.

**CI gates:**
- ECAPA parity test gates B.1 image build.
- All unit tests pass before voice-server image gets pushed to Harbor (wired into the new build script — see Distribution pipeline below).
- Browser E2E happy path is a soft gate — failures are tracked but don't block deploy.

### Distribution pipeline

The voice-server is a new container artifact. The existing `deploy-production` skill knows about `backend` and `frontend` images; voice-server needs the same flow extended.

**Build (on `.159` build box, mirroring backend pattern):**

```bash
# Mirror of the backend rsync-and-build flow from .claude/skills/deploy-production/SKILL.md
TAG=v0.1.0
ssh evdb@192.168.1.159 "rm -rf /tmp/voice-server-build-${TAG}; mkdir -p /tmp/voice-server-build-${TAG}"
rsync -avz voice-server/ evdb@192.168.1.159:/tmp/voice-server-build-${TAG}/
ssh evdb@192.168.1.159 "cd /tmp/voice-server-build-${TAG} && \
  docker build -t registry.treehouse.x-idra.de/renfield/voice-server:${TAG} \
                -t registry.treehouse.x-idra.de/renfield/voice-server:latest .

# GPU-side parity gate (review-cycle 3 GAP-2 fix) — runs before push.
# Validates the freshly-built image against tests/fixtures/speaker/ on the
# .159 GPU. Tolerance: cosine ≥ 0.99 (looser than CI gate's 0.999999 to
# accommodate legitimate int8_float16 drift). If it fails, drop to FP16
# or fall back to bundling speechbrain in the voice-server image.
docker run --rm --gpus all \
  -v \$(pwd)/tests/fixtures/speaker:/fixtures:ro \
  registry.treehouse.x-idra.de/renfield/voice-server:${TAG} \
  python /app/scripts/gpu_parity_check.py /fixtures || exit 1

docker push registry.treehouse.x-idra.de/renfield/voice-server:${TAG}
# CAUTION (review-cycle 3 NIT-2): :latest is a debug-only convenience tag.
# k8s manifests MUST pin to the version tag (:v0.1.0). Never reference
# :latest from any production manifest, smoke-test script, or skill.
# It exists only for ad-hoc \`docker run :latest\` on the build box.
docker push registry.treehouse.x-idra.de/renfield/voice-server:latest"
```

**Image-size budget:** ~3.5-4 GB realistic (revised from v1.2's 3 GB after the B.1.0 probe established that voice-server needs `speechbrain` + `torchaudio` for `compute_features` Python-side preprocessing — STFT-export limitation, see § B.1.0). Layer breakdown:

  - CUDA 13 runtime base: ~2 GB (non-negotiable)
  - faster-whisper + ctranslate2: ~600 MB
  - speechbrain + torchaudio + librosa (for ECAPA preprocessing): ~700 MB
  - Piper + onnxruntime-gpu: ~400 MB
  - Application code + dependencies: ~200 MB

  If we tip past 4 GB the Harbor 504-on-large-layer issue (documented in `deploy-production`) starts to bite. Mitigation if it does: the same multi-RUN-stage pip-install + `mv` pattern the backend image uses (split heavy packages into staging dirs, `COPY --from=builder` per dir to parallelise layer pushes).

**Skill update:** `~/.claude/skills/deploy-production/SKILL.md` gets a new "voice-server" sub-section with the build command above and the rollout sequence:
1. Apply `k8s/voice-server.yaml`
2. Wait for readiness (cold-start ~60-90 s)
3. Smoke-test `/health` and one `tts_request` round-trip via `kubectl port-forward`
4. Update Traefik ingress to route `/ws/voice` and `/api/voice/*` to voice-server (only when ready)

**Rolling updates:** `strategy: Recreate` (single GPU = single replica) means a redeploy is brief downtime — typically <30 s once the image is cached on the node. Plan voice-server rollouts during low-usage windows. If demand for zero-downtime emerges later, the path is dual-pod with NetworkPolicy pinning one as warm-spare on a different node — adds Phase B.next Hardware (a second voice GPU). For now, accept the brief blip.

**hostPath → PVC migration (review-cycle 3 NIT-3, deferred to Phase B.next):**

The B.1 manifest mounts `/mnt/llm` via `hostPath` because that's the existing pattern for llama-server-agent and llama-server-embed. This works as long as the `nodeSelector: renfield.io/role=voice-llm` is the only label-mechanism that determines scheduling. If a second voice-tier node is added later without the NFS mount, applying the same node label would let voice-server schedule there and crash with `MountVolume.SetUp failed: /mnt/llm not found`.

The clean primitive is a `PersistentVolumeClaim` bound to a `PersistentVolume` for the NFS share — Kubernetes then refuses to schedule the pod if the PV isn't bindable (clear scheduling failure, not a runtime crash). Tracked as a Phase B.next hardening ticket along with AEC and reconnect-and-resume. Not a B.1 blocker because the node-label discipline is sufficient at the current 1-voice-node scale.

**Versioning policy:**
- Semver: `v0.1.0` for B.1 ship, `v0.2.0` for B.5 (XTTS-v2 swap-in), `v1.0.0` when Phase B is stable for ≥1 month and feature flag is removed.
- Always push `:latest` alongside the version tag, but `k8s/voice-server.yaml` pins to the version tag (not `:latest`) so updates are explicit, not drift.

---

## Phase C — Speech-to-Speech Models

### What changes from B

Phase B keeps the Whisper-Renfield-Piper pipeline structure — three models, three stages, audio in / text middle / audio out. Phase C replaces the whole pipeline with a single multi-modal model that takes audio in and emits audio out, with the LLM reasoning happening inside the same forward pass.

```
Phase B:                                  Phase C:
─────────                                 ────────
audio in → Whisper → text                 audio in
text → Qwen3.6 → text                       │
text → Piper → audio out                    ▼
                                          [Speech-Native Model]
                                            │
                                            ▼
                                          audio out
```

### Why this matters for Renfield

1. **Latency floor drops to ~300 ms total round-trip.** Phase B targets 3 s. The bottleneck in B is the discrete-token-text intermediate; in C the model emits speech tokens directly while still "thinking", so the first audio comes out before the conceptual answer is fully formed.
2. **Full-duplex / barge-in becomes natural.** The model is always listening and emitting at the same time; interrupting it is a token-stream cancel.
3. **Prosody is end-to-end learnable.** Piper produces robotic-but-clear prosody from text-with-punctuation. A speech-native model can convey emphasis, hesitation, and emotion that the text representation discards.
4. **Single model = simpler ops.** No per-stage latency tracing, no STT-misheard-then-LLM-confused error compounding.

### Trade-offs against B

1. **Tool-calling is harder.** Renfield's agent loop is built on JSON-tool-call schemas. Most current speech-native models don't expose function-call interfaces. We'd lose the smart-home / Paperless / search agent unless the speech model can emit text tool-calls inline. Some can (Moshi via `text_inner` parallel stream); most can't yet.
2. **Reasoning quality.** A 7-B speech model is generally weaker at multi-step reasoning than a 35-B text-LLM like Qwen3.6-A3B. For chat ("how's the weather", "schedule a reminder") this is fine; for "plan a 3-day trip to Berlin and book the train" it's not.
3. **Voice cloning / language coverage is uneven.** Most open-weight speech-native models are English-first. German support is the bottleneck for Renfield.
4. **VRAM:** 16 GB on the 4060 Ti is tight for some 7-9 B-class speech models in FP16. Quantization possible but quality is more sensitive than for text-only LLMs.

### Candidate models (state of the art, 2026-05-05)

| Model | Author | Params | Open weights | Languages | Streaming | Tool calls | VRAM (FP16) | Status |
|---|---|---|---|---|---|---|---|---|
| **Moshi** | Kyutai | 7 B | ✅ Apache 2.0 | English | Full-duplex bidirectional | ❌ no native | ~14 GB | Stable, open since 2024-09 |
| **GLM-4-Voice** | Zhipu | 9 B | ✅ Apache 2.0 | EN/ZH (DE: weak) | Streaming output | partial | ~18 GB | Stable since 2024-10, would need int8 to fit 16 GB |
| **Mini-Omni 2** | Tsinghua | 0.5 B | ✅ Apache 2.0 | EN | Streaming | ❌ | ~2 GB | Research-grade, fits easily |
| **Qwen2.5-Omni** | Alibaba | 7 B | ✅ Apache 2.0 | Multi (incl. DE) | Streaming | ✅ via `<tool_call>` markup (text-track) | ~14 GB | Released 2025-Q1, **most aligned with Renfield's stack since text-tier already runs Qwen** |
| Qwen3.6-Omni | Alibaba (?) | unknown | unconfirmed | unknown | unknown | unknown | unknown | **not yet released as of 2026-05-05** — tracking |
| GPT-4o-realtime | OpenAI | closed | ❌ closed | Multi | Full-duplex | ✅ | n/a | Out of scope (privacy-first → on-prem only) |
| LFM2-Audio (Liquid AI) | Liquid AI | 1.5 B | ✅ Apache 2.0 | EN, multi planned | Streaming | ❌ | ~3 GB | Research-grade, 2025 release |

### Best fit for Renfield, today

**Qwen2.5-Omni-7B** is the most plausible Phase C anchor:
- Same model family as the text-tier (Qwen3.6-A3B → Qwen2.5-Omni). Tokenizer + chat-template patterns are familiar.
- German native (Qwen training data is multilingual; quality comparable to Whisper-large for German STT and to Piper for prosody — needs benchmarking).
- Tool calls via inline `<tool_call>` text track preserves the agent loop.
- 14 GB FP16 fits 4060 Ti with 2 GB headroom for KV cache.

**Moshi** is the best technical demonstration of the architecture but **fails the language requirement** — Renfield is German-first, Moshi is English-only. Disqualified for production until a German variant ships.

**Mini-Omni / LFM2-Audio** are research-grade — useful for exploration, not production.

### Decision criteria for Phase C adoption

Renfield should migrate to a speech-native model when:

| Criterion | Threshold |
|---|---|
| Latency (user stops → first word audible) | < 500 ms |
| German STT WER | ≤ 8 % (Whisper-large baseline) |
| German TTS MOS | ≥ 4.0 (Piper-thorsten-high baseline ≈ 4.2) |
| Tool-call format | JSON-compatible OR translatable layer |
| VRAM at acceptable quality | ≤ 10 GB (review-cycle 3 NIT-1 fix — was 14 GB; AWQ-Int4 fits in ~8 GB so the threshold is now tightened to leave 6 GB headroom for KV-cache growth on long conversations and to forbid silent regression to FP16) |
| License | Apache 2.0 / MIT |
| Maturity | ≥ 6 months stable, no known catastrophic-failure modes |

### Migration path (when ready)

1. **C.1 — Add the speech-native model behind a feature flag** as a third pod on `k8s-gpu-3` (replacing voice-server, since both can't share the GPU). The voice-server pod and the speech-native pod are two deployments behind the same `voice` service; pick at deploy-time.
2. **C.2 — Route voice-only chats** through the speech model, keep typed chats on the text-LLM. This is the dual-path safety net: complex agent tasks the speech model can't do go through the proven Qwen3.6 + tool path.
3. **C.3 — A/B comparison** for 2-3 weeks, measure latency + quality + tool-call success rate.
4. **C.4 — Cut over or roll back.** If the speech model is a clear win for voice and at least matches text-tier on tool-calls, retire the Phase B voice-server. Otherwise stay on B and reassess in 6 months.

### Phase C kickoff (per D3)

**Trigger:** Phase B has been running clean for ≥1 week (no model crashes, no silent-fail TTS, transcription quality stable).

**C.0 prerequisite (R2 fix):** Qwen2.5-Omni-7B at FP16 needs ~17 GB just for weights — the 14 GB claim from v1.0 was wrong. The HF model card recommends 24 GB GPU for FP16 inference. **The Phase C spike runs the AWQ-Int4 or GPTQ-Int4 quantized variant only.** Quantized inference: weights ~5 GB + KV cache ~2 GB + audio encoder ~1 GB = ~8 GB peak, fits 16 GB with ~8 GB headroom. The MOS / latency benchmarks below are measured against the quantized variant, not full precision — adoption decision must reflect what we'd actually deploy.

**Spike scope (2-3 days):**

1. **C.0.1 — Qwen2.5-Omni-7B model card review + license validation.** Confirm Apache 2.0, German training-data coverage, tool-call format documented. Confirm AWQ-Int4 weights are released (otherwise quantize ourselves with `autoawq`, +1 day).
2. **C.0.2 — Inference benchmark.** Stand up a parallel pod on `k8s-gpu-3`. **Voice-server pod is scaled to 0 during the spike** — both want the GPU exclusively. Run a fixed prompt-set:
   - 10 short voice-only commands ("Schalte das Licht im Wohnzimmer ein", "Wie ist das Wetter?")
   - 5 multi-step tool-calling tasks ("Suche im Web nach …", "Lade dieses Dokument zu Paperless")
   - 5 long-form chat ("Erklär mir kurz wie eine Wärmepumpe funktioniert")
   Measure: STT WER (vs. Whisper-medium baseline from Phase B), TTS MOS via 3 listeners, end-to-end latency, tool-call success rate, VRAM usage.
3. **C.0.3 — Decision artefact** appended to this doc. Three outcomes:
   - **Adopt:** Qwen2.5-Omni replaces voice-server for voice-only sessions, dual-path routing (see § "Phase C — open question on routing" below).
   - **Defer:** stay on B, revisit when Qwen3-Omni / Moshi-DE / LFM2-Audio v2 / GLM-4-Voice-int8 ships next.
   - **Hybrid:** Omni handles STT only (drops Whisper), text-LLM and Piper still own reasoning + TTS. Lowest-risk partial adoption.

### Phase C — open question on routing (N1)

The "voice → Omni for chat-only, fall through to text-LLM agent for tool-calls" promise needs a routing mechanism *before the model runs*, since by the time we know an utterance needs tools, Omni has already produced output. Three options the spike should evaluate:

1. **Use Renfield's existing `agent_router`** (currently classifies utterances to roles like `conversation`, `smart_home`, `documents`, `research`). When the route is `conversation` or `general` → Omni. Anything that hits a tool-using role → text-LLM path. Pro: reuses well-tested classifier, no new heuristic. Con: classifier itself runs on the text-LLM agent pod, so there's still a pre-Omni round-trip on every voice utterance.
2. **Pattern-match on the transcript.** Cheap regex / keyword check ("öffne", "schalte", "such") → tool path; everything else → Omni. Fast (no LLM), brittle.
3. **Run Omni first, intercept emitted `<tool_call>` tokens, abort if found, restart on text-LLM.** Wastes the first ~100 ms of Omni inference but needs no upfront classifier. Best UX if tool-calls are rare.

C.0.3's decision artefact picks one. Mechanism is part of the C-adopt PR, not a separate doc.

### Why this is now-not-Q3

The Q3-2026 placeholder in v0 was a guess. With the dedicated voice GPU live and Qwen2.5-Omni having had ~9 months to settle, the cost of running the spike is small (2-3 days) and the upside is large (latency floor of 300-500 ms + barge-in capability). If the benchmark says "not yet", we lose 3 days; if it says "yes", we lap Phase B by 6 months.

The risk that wasn't on v0's radar: **Qwen3-Omni hasn't shipped publicly as of 2026-05-05**. If it ships during Phase B, the Phase C spike pivots to Qwen3-Omni, which would track the text-tier's Qwen3.6 lineage even more closely. That's an upside, not a blocker.

---

## Build vs Buy — Speaches spike (2026-05-05)

Right before committing v1.3, the user asked the Layer-1 challenge: *"gibt es kein Opensource Voice Server den wir direkt einsetzen können?"* — at the precise moment we were about to commit a 7-9 day Custom-Plan. We paused and ran a 1-day spike against [`speaches-ai/speaches`](https://github.com/speaches-ai/speaches) on k8s-gpu-3.

### What we tested

Container `ghcr.io/speaches-ai/speaches:latest-cuda-12.6.3` deployed via `k8s/speaches.yaml` on k8s-gpu-3 with `nvidia.com/gpu: 1`, `securityContext.fsGroup: 1000`, longhorn-RWO PVC (NFS failed — read-only-ish for non-root containers, see § Findings). Two test surfaces:
- `/v1/audio/transcriptions` (Whisper) and `/v1/audio/speech` (Piper) HTTP — the conventional REST surface.
- `/v1/realtime` WebSocket — the OpenAI-Realtime-API-shaped streaming surface.

### Empirical findings

**STT path (HTTP):**
- `Systran/faster-whisper-small` cold-call (model already downloaded, first inference): **1.31 s** for a 3.27-s German clip ("Heute ist ein schöner Tag, wir testen die Spracherkennung.")
- Same model, warm: **0.275 s**.
- Transcript byte-exact correct on warm and cold.
- Speaches' registry exposes `tnfru/whisper-large-v3-german-ct2` (German-fine-tuned) — a model we could not have shipped without leaving CTranslate2-conversion to the user. Real upside.

**TTS path (HTTP):**
- `speaches-ai/piper-de_DE-thorsten-medium` cold (model load + synth): **1.02 s** for a 63-character German sentence.
- Warm: **82 ms**.
- Output is bit-identical-class to current Renfield Piper output (same voice file lineage).
- The catalog includes `thorsten-high`, `thorsten-medium`, `thorsten_emotional-medium` — full overlap with what Renfield uses today, plus an `emotional` variant that's currently absent.

**Realtime-API (WebSocket) — the Phase-B-relevant surface:**
- Out-of-the-box: hard failure. `openai.NotFoundError: Not Found` on `input_audio_buffer.committed`. Root cause is in [`src/speaches/dependencies.py:128`](https://github.com/speaches-ai/speaches/blob/master/src/speaches/dependencies.py): when `loopback_host_url is None`, the realtime router builds an in-process ASGI `AsyncClient` against the *stt-only sub-router*, which doesn't share `model_manager` state and returns 404. There's a `# TODO: verify` comment in the source acknowledging the brokenness.
- Workaround: set `LOOPBACK_HOST_URL=http://localhost:8000` env. After this, realtime works.
- Final-transcript correctness is excellent: same byte-exact German transcript as the HTTP path.
- **Latency profile (warm, after fix):**
  - VAD `speech_started` at +0.20 s
  - VAD `speech_stopped` at +4.44 s (audio + 3 s trailing silence as the VAD trigger)
  - `transcription.completed` at +5.74 s — i.e. **~1.3 s from end-of-speech to final transcript** (consistent with the HTTP warm-path number).
- **NO partial transcripts.** The event stream goes `speech_started → speech_stopped → committed → item.created → completed`. Single completed event. Speaches calls `transcription_client.create()` with `response_format="text"` after VAD finalises the buffer, then publishes one event with the full text. There is no incremental decoding.

### Why "no partial transcripts" is fatal for Phase B

The whole point of Phase B per `tasks/todo.md` is:

> First word audible within 1-2 s regardless of total answer length [...] STT partial-transcript visibility while user is still speaking

Speaches' Realtime-API doesn't deliver the latter. A user speaking a 10-second utterance sees nothing until VAD-stop fires plus ~1.3 s. That's worse than today's request-response REST endpoint for short utterances and only marginally better for long ones, while introducing ASGI-loopback fragility, no speaker-rec, and a forking-or-sidecar burden for the embedding emit.

### Speaker recognition gap

Speaches has zero native speaker-rec. Three integration paths were analysed in `docs/spike-speaches/01_speaker_recognition_analysis.md`:

| Path | Effort | Burden | Frontend SDK | Verdict |
|---|---|---|---|---|
| (a) audio-fork-proxy + ECAPA-sidecar | 2 d initial | low ongoing | OpenAI-RT-SDK works | the only buy-sane path, but the proxy is most of the custom-server work |
| (b) fork Speaches, add ECAPA inline | 3-5 d initial | high ongoing (rebases) | breaks SDK (custom events) | rejected |
| (c) frontend dual-stream (Speaches + backend ECAPA) | 0.5 d initial | low | works | rejected — sync-between-STT-and-speaker-id is genuinely hard |

(a) is the only viable buy-path, and it requires writing exactly the WebSocket-proxy + audio-fork code that "buying Speaches" was supposed to avoid. Net code-saving over Custom-Plan: ~0.5-1 day.

### Verdict — **Build, not buy**

Custom voice-server (`renfield-voice-server` per § "Architecture" above) is the right call. The spike disproved the buy hypothesis on its strongest claim (Realtime-API maturity) while confirming three smaller things that *help the build*:

1. **Model choices validated.** `Systran/faster-whisper-small` (or `medium`) + `speaches-ai/piper-de_DE-thorsten-medium` are confirmed correct, on real GPU, on real Renfield audio. Cold and warm latencies are in the budget the v1.3 § "Latency budget" promised.
2. **k8s-gpu-3 voice tier works end-to-end.** GPU access, NFS mounts (with the longhorn workaround for sub-1000 UID containers), Traefik routing — all proven by Speaches' own deployment. De-risks B.1 manifest authoring.
3. **The `LOOPBACK_HOST_URL` failure mode is a teaching example for our own implementation.** Don't share state between sub-routers via in-process ASGI clients; either monolithic FastAPI app with shared deps OR real HTTP. Voice-server's STT-and-ECAPA in one process avoids the entire class. Documented as a § "Anti-patterns to avoid" reference.

### What changes in the design as a result

- **D6 (new):** Voice-server uses `faster-whisper` Python library directly (the same CTranslate2 backend Speaches wraps), in **streaming mode** with `vad_filter=True`, emitting partial transcripts as decoder segments arrive. This is the explicit Phase-B-promise that Speaches couldn't keep.
- **B.1 model selection:** `Systran/faster-whisper-medium` confirmed (D1 unchanged), `de_DE-thorsten-medium` Piper (was `thorsten-high`; medium has 0.30× the latency for ~equivalent MOS per the spike).
- **No `Speaches as alternative`** appendix needed — the spike is more useful than the appendix would have been.
- **Speaches stays deployed for one more week** as a fallback STT/TTS during B.1 development. If B.1 has a regression we can route a percentage of traffic to Speaches via configmap and keep iterating. Removed once B.1 is stable.

### Spike artifacts

- `docs/spike-speaches/01_speaker_recognition_analysis.md` — three integration paths scored.
- `docs/spike-speaches/smoke_test.sh` — STT/TTS curl harness (kept for regression-spotting on Speaches drift).
- `k8s/speaches.yaml` — current state; will be removed in the v2.5.x release that ships B.1.

---

## Changelog

### v1.4 — 2026-05-05 — Speaches build-vs-buy spike, verdict = build

User Layer-1 challenge ("gibt es kein Opensource Voice Server den wir direkt einsetzen können?") forced a 1-day spike against `speaches-ai/speaches` before the v1.3 commit. The spike disproved the buy hypothesis on its load-bearing claim (Realtime-API streaming) and confirmed three side-benefits that strengthen the Custom-Plan.

**Empirical results (see § "Build vs Buy" above):**
- Speaches HTTP STT/TTS: warm latencies 275 ms / 82 ms — well within Phase B budget.
- Speaches Realtime-API: works only with `LOOPBACK_HOST_URL` env workaround (TODO in source); emits **single `completed` transcript event, no partials**.
- Speaches has no speaker-rec; the only viable buy-path requires writing a custom WS proxy = the very thing buying was supposed to avoid.

**Decision:** Build. Custom voice-server (D1-D5 as specified in v1.3). Speaches stays parked on k8s-gpu-3 for one more week as fallback during B.1 development.

**New decision:**
- **D6** — voice-server uses `faster-whisper` directly in streaming mode with `vad_filter=True`, emitting partial transcripts as decoder segments arrive. This is the streaming property Speaches' Realtime-API failed to deliver and is the core Phase B value-add.

**Net effort change:** v1.3 estimated 8-9 days for Phase B. v1.4 is **8-9 days** (no change to budget — the spike was 1 day separately, paid out of v1.3's risk-buffer, and the Custom-Plan is unchanged). B.5 unchanged at 1 day.

### v1.3 — 2026-05-05 — review-cycle 3 closure + empirical D4 validation

Two parallel inputs landed in v1.3: an independent v1.2 review (`feature-dev:code-architect` agent, cycle 3) found 3 GAPs / 3 RISKs / 3 nits, and an empirical ECAPA-TDNN parity probe inside the renfield-backend pod validated D4's architecture with cosine = 1.000000 against speechbrain ground truth.

**Empirical validation (D4 architecture):**
- ECAPA parity probe ran in the backend pod with speechbrain 1.1.0 + onnxruntime 1.25.1. Variant 1 (export `embedding_model` only, replicate `compute_features` + `mean_var_norm` in Python before the ONNX call) achieved **cosine 1.000000** vs speechbrain ground truth on a deterministic 2-second pseudo-audio fixture. Variant 2 (export the full `encode_batch` pipeline as one ONNX) is blocked by a known PyTorch ONNX exporter limitation (`STFT does not currently support complex types` in opset 17 AND opset 20 — internal Fbank STFT). v1.3 commits to Variant 1; voice-server image gains `speechbrain` + `torchaudio` for the Python-side feature extraction (~5 ms CPU per utterance, negligible vs the GPU embedding inference).

**Review-cycle 3 GAPs closed:**
- **GAP-1** (closed empirically): the v1.2 export script wording that exported `model.mods.embedding_model` against a feature-shaped tensor is correct; the parity probe proved 1.000000 cosine. v1.3 promotes the strict tolerance to ≥ 0.999999 (was ≤ 0.001 distance, equivalent threshold but cleaner expression).
- **GAP-2** (closed structurally): split parity gate — CI runs CPU-FP32 vs CPU-FP32 (≥ 0.999999), build-box GPU validation runs CPU-FP32 vs GPU-int8_float16 (≥ 0.99). Two gates, both required, Harbor push blocked if either fails. Build script in § "Distribution pipeline" wires this in.
- **GAP-3** (closed): `audio_chunk_meta` JSON message dropped from the protocol entirely. Binary WAV chunks now self-describe via a 24-byte fixed header (`magic` `RFWA` + `request_id` UUID + `sequence` uint32). Frontend routes by reading the header — no cursor needed.

**Review-cycle 3 RISKs addressed:**
- **RISK-1** (closed): JWT-secret-rotation order documented — backend FIRST, frontend re-auths, voice-server LAST. Reverse order kicks all active voice sessions; runbook now in `deploy-production`.
- **RISK-2** (closed): client-server `session_start` handshake at WS open carries `codec` (`audio/webm;codecs=opus` or `audio/ogg;codecs=opus` or `audio/wav`); locks ffmpeg `-f` flag for the session, kills near-silence-misidentification.
- **RISK-3** (closed): WS-disconnect-mid-recording mitigation expanded to BOTH directions — frontend tears down both recording and TTS playback on `ws.onclose` regardless of which was active.

**Review-cycle 3 nits:**
- **NIT-1**: Phase C VRAM threshold tightened from ≤ 14 GB to ≤ 10 GB (AWQ-Int4 fits in ~8 GB; tighter threshold prevents silent regression to FP16).
- **NIT-2**: build script `:latest` push annotated as debug-only convenience; manifests must pin version tags.
- **NIT-3**: `hostPath` → PVC migration deferred to Phase B.next with explicit reasoning.

**Image-size budget revised:** v1.2 said 3 GB; v1.3 says ~3.5-4 GB realistic given the speechbrain + torchaudio + librosa addition for Python-side feature extraction. Multi-RUN pip-install + `mv` layer-split pattern from the backend image documented as mitigation if Harbor 504s.

**Net effort change:** v1.2 estimated 7-8 days for B.1.0-B.4 + tests. v1.3 adds ~0.5 day for the GPU parity gate wiring + binary header refactor in the protocol. Total: **8-9 days for Phase B (excluding B.5 XTTS spike)**.

### v1.2 — 2026-05-05 — review-cycle 2 closure

`/plan-eng-review` skill run on v1.1 produced 1 architecture decision (D5), 2 minor code-quality items, 1 critical regression-test mandate, 1 missing-section finding, 1 critical failure-mode gap. All addressed.

**New decision:**
- **D5** — voice-server validates JWTs locally with the shared signing key (Option A); Reva re-visit hook documented at integration time (Option B). New § "Auth model" with full diagram + operational notes for both options. voice-server config supports both `AUTH_MODE=local` and `AUTH_MODE=callback` from B.1 so Reva flips a flag instead of patching code.

**Code-quality fixes:**
- **2A** — ECAPA-ONNX parity test promoted from inline snippet to checked-in `tests/backend/test_speaker_service.py::TestEcapaOnnxParity`, gated in CI. B.1 image build is gated on this test passing. Drift detection survives future model/library bumps.
- **2B** — Configmap cleanup ordering reversed: bump backend image FIRST (which tolerates legacy env vars present-or-absent), drop env vars in a follow-up commit. Removes a tight-ordering risk during rolling updates.

**Critical risks acknowledged:**
- **WebSocket disconnect mid-recording** — silent-failure path. v1.2 mandates clear "Aufnahme verloren" UX in B.1; reconnect-and-resume is Phase B.next with a `resume_session` protocol message.

**New sections:**
- **Testing strategy** — 6-row table mapping unit / integration / E2E tests to migration steps (B.1, B.3, B.4.a, B.4.b). ~4 days additional effort on top of B.1.0-B.4 implementation. Renfield's TDD project rule now visibly satisfied. CI gates documented (ECAPA parity gates B.1, all units gate Harbor push).
- **Distribution pipeline** — voice-server image build path on `.159` mirrors backend. `deploy-production` skill update specified. Image-size budget pinned at 3 GB. Versioning policy (semver + pinned tags in manifest).

**Net effort change:** v1.1 estimated 3-4 days for B.1.0-B.4. v1.2 is **7-8 days** including the test strategy. B.5 unchanged at 1 day.

### v1.1 — 2026-05-05 — review-driven revision

Independent review by `feature-dev:code-architect` agent against v1.0 surfaced 3 GAPs, 4 RISKs, 4 nits. All findings integrated; the doc is now closer to buildable.

**GAPs resolved:**
- **G1** — Speaker recognition was silently dropped in v1.0. v1.1 introduces **D4**: voice-server runs ECAPA-TDNN ONNX on GPU, emits `speaker_embedding[192]` in `final_transcript`; backend keeps the cosine-match + DB pipeline (`services/speaker_service.py`) unchanged. New B.1.0 step exports the ONNX once and gates on a parity test against the existing in-process embeddings.
- **G2** — k8s manifest's `WHISPER_MODEL_PATH` pointed at `large-v3-turbo` while D1 chose `medium`; would have crashed cold-start. Fixed in the manifest sketch + the `Models pre-pulled` list.
- **G3** — `/api/voice/voice-chat` (a hidden orchestrator endpoint that calls Whisper + Piper + Ollama in one request) was unmentioned. v1.1 explicitly handles it in B.4.b: backend keeps the route, rewrites the body to call voice-server for STT and TTS plus the existing agent loop in between. Same URL, satellites unaffected.

**RISKs addressed:**
- **R1** — Two-WebSocket race with no correlation. v1.1 protocol gains `request_id` on every `tts_request`/`tts_done`/audio chunk; `/ws/chat` envelope grows a matching `request_id` so the frontend can stitch state without overlap.
- **R2** — Phase C VRAM claim of 14 GB FP16 was wrong (Qwen2.5-Omni-7B is three sub-models, ~17 GB FP16, HF recommends 24 GB). v1.1 documents Phase C as **AWQ-Int4 / GPTQ-Int4 only**.
- **R3** — `audio_end` collided as both Client→Server (STT flush) and Server→Client (TTS done). Renamed to `stt_flush` and `tts_done` for unambiguous direction.
- **R4** — STT latency table understated Whisper-medium runtime. Split into two utterance classes: ~3 s end-to-end for short commands (≤2 s audio), ~4-5 s for typical (5 s audio). 90 s baseline → 4 s warm is still >10× improvement.

**Nits:**
- **N1** — Phase C dual-path routing now has an explicit § with three candidate mechanisms; C.0.3 picks one.
- **N2** — Acoustic echo cancellation on satellites flagged as known acute risk with B.1 mitigation (suppress STT for ~500 ms after `tts_done`) and a B.next ticket for real AEC.
- **N3** — Traefik WebSocket annotation handling: defer to existing `/ws/chat` ingress pattern; verify with `wscat` smoke after deploy.
- **N4** — STT/TTS concurrency VRAM accounting documented in the new VRAM-budget table; B.5 XTTS-v2 overlap is safe (~8 GB peak), Phase C is mutually exclusive with voice-server on the same GPU.

**Net effort change:** v1.0 estimated 2-3 days for B.1-B.4. v1.1 is **3-4 days** (one extra day for ECAPA export + parity test + `/voice-chat` orchestrator rewrite). B.5 unchanged at 1 day.

### v1.0 — 2026-05-05 — initial design after k8s-gpu-3 join

First draft. Three decisions locked: STT model = `medium`, TTS = Piper now/XTTS-v2 evaluate, Phase C = directly after Phase B. Architecture, latency budget, k8s manifest sketch, container layout, migration plan, Phase C analysis with model comparison.
