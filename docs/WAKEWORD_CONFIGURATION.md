# Wake Word Configuration

Centralized management of wake word settings for all connected devices (satellites, web panels, browsers).

## Overview

Wake word settings are managed centrally in the backend and automatically pushed to all connected devices when changed. This ensures consistent behavior across all input devices.

### Key Features

- **Centralized Configuration**: Single point of configuration in the admin UI
- **Automatic Sync**: Changes are pushed to all connected devices via WebSocket
- **Device Sync Status**: Real-time visibility of which devices have applied the new config
- **Persistent Storage**: Settings are stored in the database and survive restarts
- **Automatic Model Download**: Satellites download required TFLite models from backend

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Admin UI                                │
│                  (SettingsPage.jsx)                         │
└─────────────────────┬───────────────────────────────────────┘
                      │ PUT /api/settings/wakeword
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                  WakeWordConfigManager                      │
│           (Database + WebSocket Broadcast)                  │
└────────┬──────────────────────┬─────────────────────────────┘
         │                      │
         ▼                      ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Satellite 1   │    │   Satellite 2   │    │   Web Panel     │
│  (config_update)│    │  (config_update)│    │  (config_update)│
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## Configuration Options

| Setting | Description | Range | Default |
|---------|-------------|-------|---------|
| **Keyword** | Active wake word(s) — comma-separated to load several at once | hey_renfield, renfield_de, renfield_en, renfield_it, alexa, hey_jarvis, hey_mycroft | alexa |
| **Threshold** | Detection sensitivity | 0.1 - 1.0 | 0.5 |
| **Cooldown** | Minimum time between detections | 500ms - 10000ms | 2000ms |

### Per-language "Renfield" models

`hey_renfield` is English-pronunciation only. For a single-word ("Renfield")
wake word tuned to a specific language, three per-language models ship:

| Model | Language | Trained on |
|-------|----------|------------|
| **`renfield_de`** | German | German pronunciation |
| **`renfield_en`** | English (US + UK) | English pronunciation |
| **`renfield_it`** | Italian | Italian pronunciation |

Because the detector loads a **list** of keywords, several per-language models
run together — set the **Keyword** to a comma-separated set
(e.g. `renfield_de,renfield_en,renfield_it`) and any selected language's
"Renfield" wakes the device. In the admin UI this is a multi-select (see
**Frontend Integration → Admin Settings Page**); the value persists as a single
comma-joined string and is validated element-by-element (one unknown keyword
rejects the whole set). A single keyword (no comma) is unchanged and
backward-compatible.

The model files live in `data/wakeword-models/` (served to satellites) and
`src/frontend/public/wakeword-models/` (served to the browser); training recipe
and how to add a language: `src/satellite/wakeword-training/README.md`.

### Threshold Explanation

- **Lower values (0.1-0.3)**: More sensitive, may trigger false positives
- **Medium values (0.4-0.6)**: Balanced sensitivity (recommended)
- **Higher values (0.7-1.0)**: Less sensitive, fewer false positives

> **The threshold is not the false-positive fix.** A model that scores 0.90-0.99
> on a room's noise cannot be thresholded out without destroying recall —
> `renfield_de` v1 measured ~16 FP/hr on synthetic speech and fired ~500/hr in
> real rooms. Mic-gain and filter levers reduce false positives; only
> room-specific hard negatives eliminate them. Every new installation must pass
> [`SATELLITE_ACOUSTIC_COMMISSIONING.md`](SATELLITE_ACOUSTIC_COMMISSIONING.md)
> before it counts as live.

## API Endpoints

### Get Current Settings

```http
GET /api/settings/wakeword
```

**Response:**
```json
{
  "keyword": "alexa",
  "threshold": 0.5,
  "cooldown_ms": 2000,
  "enabled": true,
  "subscriber_count": 3,
  "available_keywords": [
    {"id": "alexa", "label": "Alexa", "description": "Pre-trained wake word (recommended)"},
    {"id": "hey_jarvis", "label": "Hey Jarvis", "description": "Pre-trained wake word"},
    {"id": "hey_mycroft", "label": "Hey Mycroft", "description": "Pre-trained wake word"}
  ]
}
```

### Update Settings (Admin Only)

```http
PUT /api/settings/wakeword
Authorization: Bearer <admin_token>
Content-Type: application/json

{
  "keyword": "hey_jarvis",
  "threshold": 0.6,
  "cooldown_ms": 3000
}
```

**Response:**
```json
{
  "keyword": "hey_jarvis",
  "threshold": 0.6,
  "cooldown_ms": 3000,
  "enabled": true,
  "subscriber_count": 3,
  "available_keywords": [...]
}
```

### Get Device Sync Status

```http
GET /api/settings/wakeword/sync-status
```

**Response:**
```json
{
  "all_synced": false,
  "synced_count": 2,
  "pending_count": 1,
  "failed_count": 0,
  "devices": [
    {
      "device_id": "satellite-living-room",
      "device_type": "satellite",
      "synced": true,
      "active_keywords": ["hey_jarvis"],
      "last_ack_time": "2025-01-25T12:00:00Z"
    },
    {
      "device_id": "web-panel-kitchen",
      "device_type": "web_panel",
      "synced": false,
      "pending_since": "2025-01-25T12:00:00Z"
    }
  ]
}
```

### Download TFLite Model (for Satellites)

```http
GET /api/settings/wakeword/models/{model_id}
```

Returns the TFLite model file for the specified wake word.

## WebSocket Protocol

### Config Update Message (Server → Device)

When settings are changed, the backend broadcasts:

```json
{
  "type": "config_update",
  "config": {
    "wake_words": ["hey_jarvis"],
    "threshold": 0.6,
    "cooldown_ms": 3000
  },
  "version": 2
}
```

### Config Acknowledgment (Device → Server)

Devices confirm successful application:

```json
{
  "type": "config_ack",
  "success": true,
  "version": 2,
  "active_keywords": ["hey_jarvis"],
  "failed_keywords": []
}
```

Or report failures:

```json
{
  "type": "config_ack",
  "success": false,
  "version": 2,
  "active_keywords": [],
  "failed_keywords": ["hey_jarvis"],
  "error": "Model download failed"
}
```

## Database Schema

Settings are stored in the `system_settings` table:

| Key | Example Value | Description |
|-----|---------------|-------------|
| `wakeword.keyword` | `hey_jarvis` | Active wake word |
| `wakeword.threshold` | `0.6` | Detection threshold |
| `wakeword.cooldown_ms` | `3000` | Cooldown period |

## Satellite Integration

### Automatic Model Download

When a satellite receives a config_update for a wake word it doesn't have locally:

1. Satellite checks if model exists locally
2. If missing, downloads from `/api/settings/wakeword/models/{model_id}`
3. Saves to `~/.cache/renfield/models/`
4. Loads the new model
5. Sends config_ack with success/failure

### Runtime Config Update

The satellite's wake word detector can update settings at runtime without restart:

```python
# Called when config_update is received
detector.update_config(
    keywords=["hey_jarvis"],
    threshold=0.6
)
```

## Frontend Integration

### Admin Settings Page

Access via: **Settings** → **Wake Word Configuration**

Features:
- Keyword multi-select (checkboxes) — pick one or more wake words; selecting
  several per-language "Renfield" models loads them all at once
- Threshold slider (0.1 - 1.0) with sensitivity labels
- Cooldown slider (0.5s - 10s)
- Device sync status display after saving
- Connected device count

### Web Device Config Updates

Web devices (panels, browsers) receive config updates and apply them to their local wake word detection:

```javascript
// In useDeviceConnection.js
case 'config_update':
  window.dispatchEvent(new CustomEvent('wakeword-config-update', {
    detail: data.config
  }));
  break;
```

The browser detector loads the **full** pushed set — every id in `wake_words`,
not just the first — so a multi-language household (e.g. `renfield_de` +
`renfield_en`) wakes on any of them, matching the satellites. `useWakeWord`
persists the active set (comma-joined in `localStorage`, survives reload),
filters it to keywords a model ships for, and — because the engine can only load
models chosen at construction — **rebuilds** the engine (stop → drop → recreate
with the full set → start) whenever the set changes; it never relies on
`setActiveKeywords` (which can only toggle among already-loaded models). The
ChatHeader keyword picker is a multi-checkbox set (at least one stays selected).

### Web Listening Recovery

The browser wake-word engine (`useWakeWord` + onnxruntime WASM) runs **locally**
on the mic and is independent of the backend chat WebSocket. It can still be left
stranded paused (`isEnabled: true`, `isListening: false`, nothing detecting) in
three ways, all of which previously needed a manual page reload:

1. A **backend `Recreate` rollout** (deploy / ConfigMap reload) drops the chat WS
   *mid-turn*. On wake-word detection the engine is paused for recording and only
   resumes on the backend's `done` frame — which never arrives once the socket is
   gone. This is the "doesn't listen any longer after a deploy" symptom.
2. An **engine error** (audio pipeline disruption). `useWakeWord` now flips
   `isListening: false` on error so the status dot is honest (yellow, not a lying
   green) and recovery can fire.
3. **Tab suspend / laptop sleep / network loss** stalling the engine.

`ChatContext` re-arms the engine on three recovery triggers — chat-WS **reconnect**
(edge-triggered on `wsConnected`), tab becoming **visible** (`visibilitychange`),
and network coming back **online**. A pure `shouldRearmWakeWord` predicate
(`pages/ChatPage/context/wakeWordRecovery.ts`) gates the resume: it skips the
happy path (already listening), an active capture, and a live wake-word turn.
`useWakeWord` reconciles the engine to a desired state: **turn-ON transitions**
(enable/resume/keyword-rebuild) are serialized through one "arm" promise chain,
while **turn-OFF transitions** (disable/pause) and the error handler are
**pre-emptive** — they run immediately and bump a generation counter that an
in-flight arm re-checks after every await and aborts on. So simultaneous
triggers (a laptop wake firing all three at once) or a server config push
landing mid-start can't double-start the engine; a hung `start()` can never
block mic-off; an engine error can't leave a green-but-dead UI; and an unmount
mid-build never leaks the mic/AudioContext.

On reconnect, a chat turn that was streaming over the dropped WS is dead: the
stuck "thinking" spinner is cleared, the half-streamed bubble is finalized, and an
`errors.connectionInterrupted` message is shown. This is keyed on a
streaming-turn flag (not raw `loading`) so a REST-fallback turn, which completes
on its own, is never falsely interrupted.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `WAKE_WORD_ENABLED` | Enable wake word detection | `true` |
| `WAKE_WORD_DEFAULT` | Initial default keyword | `alexa` |
| `WAKE_WORD_THRESHOLD` | Initial default threshold | `0.5` |
| `WAKE_WORD_COOLDOWN_MS` | Initial default cooldown | `2000` |

Note: These are only used as defaults if no database settings exist.

## Permissions

| Action | Required Permission |
|--------|---------------------|
| View settings | None (public) |
| Update settings | `admin` or `settings.manage` |

## Testing

Run wake word configuration tests:

```bash
make test-backend ARGS="tests/backend/test_wakeword_config.py -v"
```

## Troubleshooting

### Device Not Syncing

1. Check WebSocket connection in device logs
2. Verify device is registered (`GET /api/settings/wakeword/sync-status`)
3. Check for model download errors in satellite logs

### Wrong Wake Word Active

1. Check database: `SELECT * FROM system_settings WHERE key LIKE 'wakeword.%'`
2. Trigger manual reload: Disconnect and reconnect device
3. Clear device cache and restart

### Model Download Fails

1. Verify backend has model files in `src/frontend/node_modules/openwakeword-wasm-browser/models/`
2. Check network connectivity from satellite to backend
3. Check available disk space on satellite

### Web Wake Word Stops Listening (after a deploy / sleep)

The browser engine auto-recovers on chat-WS reconnect, tab-visible, and
network-online (see **Frontend Integration → Web Listening Recovery**). If it
stays silent:

1. Confirm the chat WS is connected (chat header shows "Verbunden"). A backend
   `Recreate` rollout briefly drops it; recovery fires once it reconnects.
2. Check the wake-word status dot: green pulse = listening, yellow = paused/errored.
3. Verify mic permission is still granted and the `/wakeword-models/*.onnx` +
   `/ort/*` assets load (200) — these are static, not `/api`.
4. Last resort: hard-reload the page (Cmd+Shift+R).
