"""WebSocket handlers owned by ha_glue.

- `ha_glue.api.websocket.device_handler` — unified WebSocket endpoint
  for Renfield satellites (Pi Zero) and stationary web clients
  (wall-mounted iPads for home automation). Handles wake-word events,
  audio streaming, TTS output routing, and device state tracking.

Mounted on the platform FastAPI app via the `register_routes` hook
from `ha_glue.bootstrap`. Platform-only deploys (no ha_glue) don't
mount the router — the `/ws/device` endpoint simply doesn't exist.
"""
