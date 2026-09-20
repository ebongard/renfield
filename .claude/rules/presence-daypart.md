---
paths:
  - "src/backend/services/daypart_service.py"
  - "src/backend/ha_glue/services/led_dimming_service.py"
  - "src/backend/ha_glue/services/presence*.py"
---
# Day/night awareness, night LED dimming, presence history, Bluetooth scan

Loaded only when the daypart / LED-dimming / presence services are read. BLE/IRK resolution and room arbitration:
`docs/design/ble-presence-improvement.md` + `.claude/rules/satellites.md`. Env reference: `docs/ENVIRONMENT_VARIABLES.md`.

## Day/Night Awareness (`services/daypart_service.py`, platform core)
- day / evening / night from configurable clock windows in the LOCAL timezone: `DAYPART_NIGHT_START`,
  `DAYPART_NIGHT_END`, `DAYPART_EVENING_START`, `DAYPART_TIMEZONE`. The night window wraps midnight.
- Injects a `{time_context}` line into EVERY agent prompt.
- A 5-min lifecycle watcher fires the `daypart_changed` hook on TRANSITIONS only; consumers must not assume a
  periodic tick.

## Night LED Dimming (`ha_glue/services/led_dimming_service.py`) — backend-driven
- Consumes `daypart_changed` → pushes `led_config{brightness}` to every connected satellite over the existing WS.
- **The `register_ack` carries the current brightness**, so a satellite reconnecting mid-night comes up dimmed.
  Removing that leaves a reconnecting satellite bright until the next transition.
- The satellite SCALES `leds.brightness`, so animations keep running.
- Symmetric: ANY transition out of night restores `LED_DAY_BRIGHTNESS`=20; night = `LED_NIGHT_BRIGHTNESS`=5.

## Persistent Presence History (`PRESENCE_HISTORY_ENABLED`, code default on)
- `presence_events.satellite_id` column (migration `pc20260616`) + timeline query methods;
  routes `/api/presence/analytics/{timeline,last-seen-by-room,room-window}`; chat tool `internal.presence_history`
  ("where was X", "who was in room Y"), advertised via the `presence` role in `config/agent_roles.yaml`.
- **Cross-user reads require `ROOMS_MANAGE`.**
- The flag gates the routes + the tool; events are persisted regardless. **In-memory live presence is untouched** —
  history is a read path, never a second source of truth for the current room.

## Bluetooth Device Scan (`BT_SCAN_ENABLED`, default off)
- "scanne die Bluetooth-Geräte" → `internal.bluetooth_scan` (role `smart_home`) fans a discovery scan out to ALL
  satellites over the `bt_scan_request` / `bt_scan_result` WS protocol — the same request-response pattern as
  `capture_snapshot`.
- Each satellite runs Classic-BT inquiry + BLE discovery; the backend dedups by MAC, keeps the STRONGEST RSSI,
  groups per room, maps OUI → vendor. Only advertising/discoverable devices appear; a scan takes ~15–30 s.

## IRK push
IRKs are location-tracking keys: with enrollment on they go ONLY to authenticated satellites
(`presence_service.irks_for_satellite` + `push_macs_to_satellites` key on `SatelliteInfo.authenticated`) —
see `satellite-trust-ota.md`. The IRK itself is never returned by any API.
