# Satellite Monitoring

Real-time monitoring and debugging for Renfield satellite voice assistants.

## Overview

The satellite monitoring system provides:
- **CLI Tool**: Local debugging on each satellite (`renfield-monitor`)
- **Admin Dashboard**: Centralized web interface for all satellites
- **Extended Heartbeat**: Real-time metrics sent every 30 seconds

## CLI Monitoring Tool

### Installation

The `renfield-monitor` CLI tool is included with the satellite package.

### Usage

```bash
# Live monitoring mode (default)
renfield-monitor

# Test microphone with level meter
renfield-monitor --test-mic

# Test for specific duration
renfield-monitor --test-mic --test-duration 30

# Show current status and exit
renfield-monitor --status

# Disable colored output
renfield-monitor --no-color
```

### Live Monitor Display

```
╔════════════════════════════════════════════════════════════╗
║              RENFIELD SATELLITE MONITOR                    ║
╚════════════════════════════════════════════════════════════╝
 Time: 14:32:45

 CONNECTION
 ──────────────────────────────────────
 Status: ● CONNECTED
 State:  IDLE

 AUDIO LEVELS
 ──────────────────────────────────────
 RMS:  [████████░░░░░░░░░░░░░░░░░░░░░░] 1234
 dB:   [██████████████░░░░░░░░░░░░░░░░] -18.3 dB
 VAD:  SPEECH

 WAKE WORD
 ──────────────────────────────────────
 Last:       hey_jarvis
 Confidence: 87%
 Ago:        45s

 SESSIONS
 ──────────────────────────────────────
 Total (1h):  12
 Errors (1h): 2

 SYSTEM
 ──────────────────────────────────────
 CPU:  34.2%
 Mem:  52.1%
 Temp: 48.5°C

 Press Ctrl+C to exit | q to quit
```

### Microphone Test

```bash
renfield-monitor --test-mic
```

Output:
```
Microphone Test
Testing for 10 seconds...

 [██████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░] -18.3 dB  RMS: 1234

Test complete!
  Max RMS: 5678
  Max dB:  -12.5 dB
  ✓ Audio levels OK
```

## Admin Dashboard

Access via: **Admin** → **Satellites**

### Features

- **Live Status Grid**: All connected satellites with real-time updates
- **Audio Level Meters**: Visual RMS and dB display
- **System Metrics**: CPU, memory, temperature (Raspberry Pi)
- **Session History**: Recent sessions with transcriptions
- **Error Tracking**: Error count and details

### Auto-Refresh

The dashboard auto-refreshes every 2 seconds by default. Toggle with the checkbox in the header.

## API Endpoints

### List All Satellites

```http
GET /api/satellites
```

**Response:**
```json
{
  "satellites": [
    {
      "satellite_id": "satellite-living-room",
      "room": "Living Room",
      "state": "idle",
      "connected_at": "2025-01-25T12:00:00Z",
      "last_heartbeat": "2025-01-25T14:32:45Z",
      "uptime_seconds": 9165,
      "heartbeat_ago_seconds": 2,
      "has_active_session": false,
      "capabilities": {
        "local_wakeword": true,
        "speaker": true,
        "led_count": 3,
        "button": true
      },
      "metrics": {
        "audio_rms": 1234.5,
        "audio_db": -18.3,
        "is_speech": true,
        "cpu_percent": 34.2,
        "memory_percent": 52.1,
        "temperature": 48.5,
        "session_count_1h": 12,
        "error_count_1h": 2
      }
    }
  ],
  "total_count": 1,
  "online_count": 1,
  "active_sessions": 0
}
```

### Get Single Satellite

```http
GET /api/satellites/{satellite_id}
```

### Get Satellite Metrics

```http
GET /api/satellites/{satellite_id}/metrics
```

### Get Active Session

```http
GET /api/satellites/{satellite_id}/session
```

**Response (when session active):**
```json
{
  "session_id": "satellite-living-room-abc123",
  "state": "listening",
  "started_at": "2025-01-25T14:32:40Z",
  "duration_seconds": 5.3,
  "audio_chunks_count": 45,
  "audio_buffer_bytes": 72000,
  "transcription": null
}
```

### Get Event History

```http
GET /api/satellites/{satellite_id}/history?limit=50
```

**Response:**
```json
{
  "satellite_id": "satellite-living-room",
  "events": [
    {
      "timestamp": "2025-01-25T14:32:45Z",
      "event_type": "session_end",
      "details": {
        "session_id": "...",
        "reason": "completed",
        "duration": 5.3,
        "transcription": "Turn on the lights"
      }
    }
  ],
  "total_sessions": 247,
  "successful_sessions": 240,
  "failed_sessions": 7,
  "average_session_duration": 4.2
}
```

### Ping Satellite

```http
POST /api/satellites/{satellite_id}/ping
```

## Extended Heartbeat Protocol

### Satellite → Backend

Heartbeat messages now include optional metrics:

```json
{
  "type": "heartbeat",
  "status": "idle",
  "uptime_seconds": 3600,
  "metrics": {
    "audio_rms": 1234.5,
    "audio_db": -18.3,
    "is_speech": true,
    "cpu_percent": 34.2,
    "memory_percent": 52.1,
    "temperature": 48.5,
    "last_wakeword": {
      "keyword": "hey_jarvis",
      "confidence": 0.87,
      "timestamp": 1706190765
    },
    "session_count_1h": 12,
    "error_count_1h": 2
  }
}
```

### Backend → Satellite

```json
{
  "type": "heartbeat_ack"
}
```

## Metrics Reference

| Metric | Type | Description |
|--------|------|-------------|
| `audio_rms` | float | Audio RMS level (0-32768 for 16-bit) |
| `audio_db` | float | Audio level in dB (relative to full scale) |
| `is_speech` | bool | Voice activity detection state |
| `cpu_percent` | float | CPU usage percentage |
| `memory_percent` | float | Memory usage percentage |
| `temperature` | float | CPU temperature in Celsius |
| `last_wakeword` | object | Last wake word detection info |
| `session_count_1h` | int | Sessions in last hour |
| `error_count_1h` | int | Errors in last hour |

## Troubleshooting

### No Audio Levels Shown

1. Check microphone connection
2. Run `renfield-monitor --test-mic` on the satellite
3. Verify ALSA configuration (`.asoundrc`)

### Satellite Not Appearing in Dashboard

1. Check WebSocket connection: `docker logs renfield-satellite`
2. Verify server URL in satellite config
3. Check network connectivity

### High Error Count

1. Check satellite logs for error details
2. Verify wake word model files exist
3. Check server-side logs for processing errors

### Temperature Warnings

- **>60°C**: Consider improving cooling
- **>70°C**: May cause throttling
- **>80°C**: Risk of shutdown

Improve cooling:
- Add heatsink to Raspberry Pi
- Ensure ventilation around device
- Use a case with passive cooling
