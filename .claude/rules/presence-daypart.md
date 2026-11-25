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
- A 5-min watcher fires `daypart_changed` on TRANSITIONS only; consumers must not assume a periodic tick.

## Night LED Dimming (`ha_glue/services/led_dimming_service.py`) — backend-driven
- Consumes `daypart_changed` → pushes `led_config{brightness}` to every connected satellite over the existing WS.
- **The `register_ack` carries the current brightness** — without it a satellite reconnecting mid-night stays bright.
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
- `internal.bluetooth_scan` (role `smart_home`) fans a scan out to ALL satellites over `bt_scan_request` /
  `bt_scan_result` (the `capture_snapshot` request-response pattern).
- Each satellite runs Classic-BT inquiry + BLE discovery; the backend dedups by MAC, keeps the STRONGEST RSSI,
  groups per room, maps OUI → vendor. Only advertising/discoverable devices appear; a scan takes ~15–30 s.

## Live readings (`/ws/presence-debug`, tab "Readings" on `/admin/presence`)
- **Never poll, never work unwatched:** push only, `note_ble_report()` is a no-op without a connected viewer; ONE
  builder (`presence_debug.build_sightings_payload`) for socket + REST. ADMIN, fail-closed; device key stays a hash.
- Refusal = `accept()` THEN close (a pre-accept close reaches the browser as 1006 = "network down"): 4403 ONLY for a
  loaded non-admin (final), 4001 for "could not verify" (client retries — a lookup error must not lock out); the socket
  ends itself after `MAX_SESSION_SECONDS` so the role is re-checked. The view mirrors `_should_switch_filtered` —
  change the switch rule (hold, pin, margins) and `summarizeRooms` in `LiveReadings.tsx` must follow.

## Placement v2 — stage 1 shadow (`PRESENCE_PLACEMENT_V2_SHADOW_ENABLED`), stage 2 state (both default off)
- `presence_placement.decide` stays PURE (no I/O, no module state, clock passed in): the labelled walk in
  `tests/backend/fixtures/` replays through it. In a `placed` verdict a gate uses `rooms` (the whole zone); `room` is a hint.
- A silent receiver is CENSORED (at or below its cutoff, or deaf), never "far"; an uncalibrated one keeps a user home
  and never places, nor does NaN/±inf or a level outside −127…+20 dBm (gates use `_at_least`). Receiver numbers: NO defaults.
- Nothing may READ a verdict from the SHADOW: it logs and adds `placement_v2` to the payload. Flag off = not imported.
- Stage 2 (`PRESENCE_PLACEMENT_V2_STATE_ENABLED`, off): `PlacementState` books the state, legacy does not run. ONE rule:
  the source that changed zone LAST wins (voice = a claim timed at the sighting; decide it AT ONCE). Contested / no room
  / stale evidence = uncertain, last room stays booked. One `call_later` timer, no polling; `unavailable` changes
  nothing. Go/no-go: `renfield_presence_voice_check_total`.

## IRK push
IRKs are location-tracking keys: with enrollment on they go ONLY to authenticated satellites
(`presence_service.irks_for_satellite` + `push_macs_to_satellites` key on `SatelliteInfo.authenticated`) —
see `satellite-trust-ota.md`. The IRK itself is never returned by any API.
