# Satellite Camera — Visual Queries

Visual queries allow satellites with a camera to capture a snapshot at wakeword detection
and send it alongside the transcribed speech to a Vision-LLM for image-aware responses.

## Use Case

> "Hey Renfield, was steht auf diesem Zettel?"

1. Wakeword detected -> camera snapshot captured in background
2. User finishes speaking -> audio transcribed, snapshot attached to `audio_end` message
3. Backend receives image + text -> routes to Vision-LLM (`qwen3-vl:8b`)
4. Vision-LLM describes what it sees -> TTS response spoken back

## Architecture

```
Satellite                              Backend
--------                              -------
Wakeword detected
  |-> camera.capture() (async)
  |-> Start listening
  ...user speaks...
  |-> End listening
  |-> Collect snapshot
  |-> send audio_end(image=base64)  -->  satellite_handler
                                           |-> Whisper STT
                                           |-> Intent extraction (text-only)
                                           |-> Response generation:
                                           |     image present + vision model?
                                           |       YES -> chat_stream_with_image()
                                           |       NO  -> chat_stream()
                                           |-> Piper TTS
                                     <--  |-> send tts_audio
```

## Setup

### 1. Satellite Configuration

In `satellite.yaml` (or via Ansible provisioning):

```yaml
camera:
  enabled: true
  resolution: "1280x720"  # optional, default
  quality: 85             # JPEG quality, optional
```

Requires `rpicam-still` to be available on the Pi (standard with Raspberry Pi OS).

### 2. Backend Configuration

Set in `.env`:

```bash
# Vision model (must support Ollama's images parameter)
OLLAMA_VISION_MODEL=qwen3-vl:8b

# Optional: dedicated Ollama instance for vision (e.g. GPU server)
OLLAMA_VISION_URL=http://host.docker.internal:11434
```

Then pull the model:

```bash
ollama pull qwen3-vl:8b
```

If `OLLAMA_VISION_MODEL` is empty (default), visual queries are disabled and the
image is silently ignored — the satellite still captures but the backend uses the
standard text-only chat model.

**Important:** The TTS cache endpoint (`/api/voice/tts-cache/`) must be reachable
by DLNA renderers over plain HTTP. See [OUTPUT_ROUTING.md](OUTPUT_ROUTING.md) for
`ADVERTISE_HOST` / `ADVERTISE_PORT` configuration.

### 3. Provisioning

The Ansible provisioning supports camera via `camera_enabled`:

- `group_vars/satellites.yml`: `camera_enabled: false` (default for all satellites)
- `host_vars/satellite-arbeitszimmer.yml`: `camera_enabled: true` (per-satellite override)

## Supported Hardware

- **IMX219** (Pi Camera Module v2) — tested on satellite-arbeitszimmer
- Any camera supported by `rpicam-still` (Pi Camera Module v3, HQ Camera, etc.)

## How It Works

### Timing

The snapshot is triggered immediately on wakeword detection, before the user starts
speaking. By the time the user finishes their question (typically 2-5 seconds), the
snapshot is already captured and ready. This means zero additional latency.

### Graceful Degradation

- No camera hardware -> `camera.open()` returns False -> camera set to None
- `rpicam-still` not installed -> same as above
- Capture fails -> snapshot is None -> standard text-only response
- No vision model configured -> image silently ignored
- Other satellites without camera -> completely unaffected

### WebSocket Protocol

The existing `audio_end` message gains an optional `image` field:

```json
{
  "type": "audio_end",
  "session_id": "sat-arbeitszimmer-1234567890",
  "reason": "silence",
  "image": "<base64-encoded JPEG>"
}
```

No new message types are introduced. Satellites without cameras simply omit the field.

## Vision Models

Candidate models:

| Model | Size | VRAM | Quality |
|-------|------|------|---------|
| **`qwen3-vl:8b`** | ~6GB | ~12GB | Excellent scene description, good German |
| `qwen2.5vl` | ~5GB | ~10GB | Good vision, slightly older |
| `minicpm-v` | ~5.5GB | ~19GB | Good for text reading |
| `llava:7b` | ~4.7GB | ~6GB | Good general vision |
| `llava:13b` | ~8GB | ~12GB | Better quality, slower |

**Recommendation:** `qwen3-vl:8b` fits entirely in 16 GB VRAM and delivers excellent
results in German. `minicpm-v` requires ~19 GB and partially offloads to CPU on 16 GB
cards.

**Production:** `qwen3-vl:8b` is served by the in-cluster `ollama` pod on `k8s-gpu-1`
(RTX 5060 Ti, 16 GB) — `OLLAMA_VISION_URL` routes there, so no separate vision pod is
needed. A single test-snapshot inference measured ~1 s end-to-end on the GPU; image-heavy
scene descriptions with longer responses take proportionally longer.
