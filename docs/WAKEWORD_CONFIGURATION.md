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
| **Keyword** | Active wake word | alexa, hey_jarvis, hey_mycroft | alexa |
| **Threshold** | Detection sensitivity | 0.1 - 1.0 | 0.5 |
| **Cooldown** | Minimum time between detections | 500ms - 10000ms | 2000ms |

### Threshold Explanation

- **Lower values (0.1-0.3)**: More sensitive, may trigger false positives
- **Medium values (0.4-0.6)**: Balanced sensitivity (recommended)
- **Higher values (0.7-1.0)**: Less sensitive, fewer false positives

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
- Keyword dropdown with all available wake words
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
