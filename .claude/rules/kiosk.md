---
paths:
  - "src/frontend/src/components/kiosk/**"
  - "src/backend/api/websocket/kiosk_*.py"
---
# Kiosk (`kiosk.view` wall display)

Loaded only when a kiosk file is read. Long form: `docs/design/command-center.md` (history, "why not poll"),
`tasks/kiosk-active-subsystem-plan.md`. The admin Command Center (`/admin/command-center`, `/api/command-center/*`)
is REMOVED — the kiosk (`/kiosk`, `<ProtectedRoute permission="kiosk.view">` OUTSIDE the app Layout, sidebar entry
`nav.kiosk`) is the only surface.

## The gate is `kiosk.view`, and `admin` is NOT a wildcard on the backend
A display in the hallway must not hold admin rights (auth-on cutover D-5): the page, the sidebar entry and
`/ws/kiosk` all ask for `kiosk.view`; the Kiosk role (`kiosk.view` + `rooms.read`, nothing else) is held by a DEVICE
account. **The frontend treats `admin` as a wildcard (`AuthContext.hasPermission`), the backend does not**
(`PERMISSION_HIERARCHY[ADMIN] == set()`) — so any route admins must keep needs the permission written into the Admin
ROLE, which is why `kiosk.view` is in `DEFAULT_ROLES["Admin"]`. `ensure_default_roles` merges it into existing system
roles at startup. The gate fails closed: a lookup error denies.

## Never
- **Never poll.** Data path = event-push: `useKioskSocket.ts` → `kiosk.view`-gated `/ws/kiosk` (`api/websocket/kiosk_handler.py`):
  ONE `snapshot` on connect, then deltas (`satellite_state`, `satellite_online`/`satellite_offline`, `presence_changed`,
  `now_playing_changed`, `tool_health_changed`, `internal_health_changed`, `weather_updated`, `turn_activity`). Backend
  refreshers (weather, internal health) are `_kiosk_clients`-gated and diff-gated; the gate resets on kiosk-connect and
  advances only after a successful broadcast.
- **Every payload is content-free** — counts, role/room names, `{subsystem_id, at}`; never utterance, entity or user id.
  Now-playing: one per room, PLAYING-only, no user ids. `KIOSK_WEATHER_LOCATION` is env-only, never committed.
- **Do not "fix" the glow to match DESIGN.md.** The kiosk DELIBERATELY breaks it (sanctioned, TODOS.md line 315); the
  glow/bloom lives ONLY here. Motion (nebula/stars/radar sweep) stays reduced-motion-gated.
- **No wall-clock decay in the frontend.** Liveness is backend-authoritative: a satellite in the roster IS online; the
  backend pushes `satellite_offline`, a reconnect re-anchors from a fresh snapshot. Federation peers get the
  `peer_status_changed` delta (#969) and additionally keep a wall-clock freshness backstop (`PEER_OFFLINE_MS`) in
  `useKioskModel.ts` — a second guard behind the delta, not a substitute for it.

## Health verdicts
- Primary = `get_status()`, which folds connectivity AND functionality: **degraded** = connected but a bound startup
  plugin failed to load (`PLUGIN_MCP_BINDINGS`) or 0 tools — not merely a low success rate.
- Secondary = per-tool success rate (`tool_health[]` in `build_kiosk_snapshot`, folded per server in `useKioskModel.ts`
  ONLY when the primary verdict is healthy). It is **windowed**: a tool counts only if its latest call is within
  `_TOOL_HEALTH_RECENT_HOURS` (24h) AND it has ≥ `_TOOL_HEALTH_MIN_SAMPLES` (3) calls, else it is omitted and the node
  falls back to connectivity. Reason: `ToolOutcomeStat` counters are cumulative (no decay), so old failures pinned a
  node red forever.
- `NodeHealth`: `'off'` = feature disabled (muted) ≠ `down` (red outage) ≠ `unknown` (awaiting verdict). The localized
  `impaired_code` is the `<title>` tooltip.
- Pseudo-nodes knowledge/presence/media: `INTERNAL_SUBSYSTEM_NODES` (`useKioskModel.ts`) MUST stay in sync with the
  backend `INTERNAL_SUBSYSTEM_LABELS` map ({knowledge, presence, homeassistant, weather, media}; homeassistant and
  weather are real MCP servers, not pseudo-nodes). Verdict from `compute_internal_subsystem_health()` in `kiosk_data.py`: presence `off` when
  `presence_enabled=false`, `degraded` on an enrolled-but-unauthenticated satellite or none online; knowledge
  `degraded` on a dead ingest worker or high live backlog (`ingest_worker_and_backlog()` → XPENDING, NOT the stream
  length); media `off` when media-follow is disabled, else `healthy` (no honest probe yet). Pseudo-nodes stay OUT of
  the MCP tool-health COUNTS.

## Liveness sweep (`SatelliteManager.cleanup_stale`, scheduled by `ha_glue.bootstrap._schedule_satellite_cleanup`)
- The session timeout is a max RECORDING duration: only a `listening` session expires. The whole turn runs inline in
  the receive loop — timing out the TURN destroys the session mid-answer and `send_tts_audio` drops it silently.
- Eviction EXEMPTS a satellite with a live session or a running OTA (installer blocks its event loop ~150 s vs a 60 s
  deadline) and must CLOSE the socket — otherwise the receive loop keeps acking heartbeats and the device never
  re-registers (mute forever). All four knobs live in `k8s/configmap.yaml`.
- **Web/browser devices have NO sweep** (the dead `DeviceManager.cleanup_stale` was removed 2026-09-21): a device
  leaves the roster when its socket closes (`device_handler` → `unregister`). Do not re-add a heartbeat eviction
  there without first measuring which devices it would evict.

## Colours
Status colours mirror the satellite LED ring (`src/satellite/renfield_satellite/hardware/led.py`): idle=blue,
listening=green, processing=yellow, speaking=cyan, error=red, offline=dark-dashed — core orb, room dots
(`RoomNode.state` = most-significant live state in the room), legend, and the whole ambient field tint.
