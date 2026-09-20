# Command Center — live constellation of the running system

> **SUPERSEDED (decommissioned 2026-07).** The admin Command Center
> (`/admin/command-center`, `AgentConstellation` / `useCommandCenterModel`, and
> the `/api/command-center/*` REST feeds) has been **removed**. The **kiosk**
> (`/kiosk`) is the surviving wall-display surface, and it is now **event-push,
> not poll** — see [`kiosk-active-subsystem-plan.md`](../../tasks/kiosk-active-subsystem-plan.md)
> and FEATURES.md §Fullscreen-Kiosk. This document is kept as institutional
> memory: it records *why* the board originally used a 3s poll and *why* that was
> later reversed (the "no polling" directive), so the next person doesn't
> reintroduce a poll. Everything below describes the now-removed admin board.

Status: **Phase 1+2+3 IMPLEMENTED** (2026-07, `feature/command-center-kiosk`):
`/admin/command-center` is routed (`<AdminRoute>` + nav entry), fed live by
`useCommandCenterModel`, with drill-downs, a decaying pulse trail, hover
reach-edges, a content-free activity rail, and a grouped-list fallback below
`lg`.

The core shows **no state word** — its LED colour + the legend carry the status
(only the active room name surfaces, during a live voice turn); the live state
is exposed as `data-core-state` for tests. The kiosk is **orientation-agnostic**:
the SVG uses `preserveAspectRatio="xMidYMid meet"` (the whole constellation is
always visible, never cropped) and the warm base is a CSS gradient on the
wrapper div (fills any aspect ratio), so the landscape wall TVs and the
**portrait room tablets** both render correctly.

**Phase 3 — fullscreen kiosk (SHIPPED).** `/kiosk` (a separate route OUTSIDE
the app Layout, `<AdminRoute>`) renders the cinematic wall-display variant
(`KioskConstellation.tsx`, driven by `useKioskModel.ts`): a glowing bloom core
that reacts to real household voice activity (idle/listening/processing/
speaking, from the satellites' own `state`), concentric role/tool/room/peer
rings, a "Kiosk" launch button on the admin header, and an alive dark field
(drifting nebula + twinkling stars + a slow radar sweep, all reduced-motion
gated). It DELIBERATELY breaks DESIGN.md's restraint (glow/bloom) per TODOS.md
line 315 — the glow aesthetic lives ONLY here, never in the restrained admin
board. Content-free by construction (counts, role names, room names).

Two **ambient tiles** (2026-07-03) add glanceable household context, each fed
by a new read-only ADMIN endpoint and self-hiding when unavailable:
- **Weather** (under the wordmark) — `GET /api/command-center/weather` calls the
  weather MCP for the configured home location (`KIOSK_WEATHER_LOCATION`, env
  only — never committed), process-local ~10-min TTL cache; `null` when weather
  is disabled / no location / MCP down (reuses the chat weather artifact's
  WMO→icon mapping).
- **Now-playing** (bottom-center) — `GET /api/command-center/now-playing` off
  `MediaFollowService.active_sessions()` (one entry per room, PLAYING-only,
  content-minimal — room + what's playing + media kind, **no user ids**); empty
  when media-follow is off or nothing plays.

**Kiosk status colours = the physical satellite LED ring (2026-07-03).** The
kiosk colour-codes voice STATUS to match the LEDs the household sees on the
devices (`src/satellite/renfield_satellite/hardware/led.py`): **idle=blue,
listening=green, processing=yellow, speaking=cyan, error=red**, offline=dark
dashed. Applied to the core orb (`CORE_COLOR`), the per-room dots (coloured by
`RoomNode.state` — the most-significant live state across a room's online
satellites, aggregated in `useCommandCenterModel`), and the legend. The **whole ambient field follows the
core** — the halo, nebula, radar sweep and the CSS base gradient (`tintDark`) are
all tinted to the core's current LED colour, so the background tracks the state
(blue at idle, green while listening, …) and stays cohesive with the orb instead
of a fixed hue. (This superseded the earlier turquoise/crimson room encoding and
the interim fixed-warm-amber ambient.)

Two reality corrections vs the plan below, discovered during implementation:

1. **Agent roles had no REST surface** — `/api/roles` is RBAC system roles,
   not agent roles. New read-only ADMIN endpoint
   `GET /api/command-center/roles` exposes `app.state.agent_router.roles`
   (name, localized description, `mcp_servers`/`internal_tools` — the latter
   back the hover reach-edges).
2. **The chat WS can't drive the pulse** — it is chat-page-local
   (`useChatWebSocket`, no global WS context) and per-session, so an admin
   page riding it would only see its *own* turns. Instead the board polls
   `GET /api/command-center/activity` (3s): recent role activations read from
   the persisted `message_metadata.agent_role` (role-surfacing). Content-free
   by construction (role + timestamp + action_success only — no message text,
   no user ids), household-wide, and kiosk-safe — strictly better than the WS
   idea for this surface.

Scope: a single **read-first "mission control"** surface that shows, at a glance,
what the running Renfield is *doing right now* — which agent role is answering,
which MCP tools/integrations are healthy, which satellites/rooms are live and
occupied, and which federation peers are reachable. It does **not** add control
logic; it composes data Renfield already emits and drills down into the existing
admin pages.

## Why this doc exists

The trigger was a reference UI ("Apex" by Reznikov Engineering): a radial
"mission control" with a glowing central core surrounded by labelled agents
(Strategist, Researcher, Chief of staff, …) and tools (Calendar, Email, Memory,
Drive). The pattern is compelling because it answers one question instantly:
**"what is my agent system, and what is it doing?"**

Renfield already produces every datum such a view needs — but the data is
scattered across six separate admin pages (`/admin/routing`,
`/admin/tool-health`, `/admin/trajectories`, `/admin/satellites`,
`/admin/presence`, `/admin/integrations`). No surface unifies them, and none is
*alive* (reflecting the current turn as it happens). The Command Center is that
unifying, live surface.

## The reference, and what to borrow vs reject

**Borrow** (the interaction concept):

- A **central core** = the orchestrator/assistant, with the **currently-active
  agent role** surfaced on it live.
- **Concentric rings of labelled nodes** = the system's capabilities, grouped by
  kind (roles, tools, rooms, peers).
- **Node state encodes live status** (health / presence / online) by colour +
  a non-colour channel.
- An **ambient, at-a-glance** read — legible from across the room, good on a
  wall tablet / kiosk.

**Reject** (the aesthetic): the Apex look is a glowing sci-fi orb with energy
blooms and radial gradients. **This directly violates `DESIGN.md`**, which is
locked and explicit:

> Decoration level: INTENTIONAL — light grain on cream surfaces, **no decorative
> blobs / gradients / icons-in-circles**.

and the AI-slop blacklist forbids *"purple/violet/indigo gradients"* and
*"icon-in-circle decoration"*. Renfield's identity is **warm, restrained,
serif** — crimson + turquoise + cream + Cormorant, "for HOME, not work." So the
Command Center must be a **structural constellation**, not a light show:

| Apex (reject) | Renfield Command Center (adopt) |
|---|---|
| Glowing energy orb, radial gradient core | Solid crimson core disc, thin ring, Cormorant wordmark |
| Bloom/particle connectors | Thin 1–2px connectors; only the **active** edge animates (a subtle dash), `prefers-reduced-motion` honoured |
| Sci-fi neon palette | DESIGN.md tier/brand tokens only (`--color-primary-*`, `--color-accent-*`, cream) |
| Decorative; impressionistic | Legible; every node is a real entity that drills down |

This is the central design decision: **the *layout* is borrowed, the *vibe* is
Renfield's.** Calibrate every pixel against `DESIGN.md` at build time.

## The constellation: rings and what feeds them

```
                    ◦ peers (federation, outer arc)
              ○────────────────────────────────○
           ○        rooms / satellites            ○
        ○        ◇──────────────────────◇           ○
      ◇       tools / MCP integrations      ◇         ◦
     ◇     ●──────────────────────────●       ◇
    ◇     ●      agent roles            ●      ◇
    ◇    ●         ┌─────────┐          ●     ◇
         ●         │ RENFIELD│  ← active role surfaced here
    ◇    ●         │  core   │          ●     ◇
    ◇     ●        └─────────┘         ●      ◇
     ◇     ●──────────────────────────●      ◇
      ◇        ◇──────────────────────◇     ◦
        ○         (rooms ring)            ○
           ○                            ○
              ○────────────────────────○
```

| Ring | Nodes | Live state shown | Backend source (already exists) |
|---|---|---|---|
| **Core** | Renfield orchestrator | the **currently active agent role** | WS `done` frame `agent_role` + `role_hint` (role-surfacing item 6, already on the wire) |
| **Roles** | `agent_roles.yaml` roles (smart_home, knowledge, media, presence, general, conversation, …) | which role just answered (pulse), idle vs active | `GET /api/roles`; live highlight from the chat WS `done` frame |
| **Tools / MCP** | Home Assistant, Paperless, Jellyfin, Calendar, Email, Search, n8n, DLNA, Weather, News, Radio | health: healthy / degraded / down / unknown | `GET /api/tool-health`, `GET /api/intents/integrations/summary` |
| **Rooms / satellites** | each satellite/room | online/offline + occupant count (presence) | `GET /api/satellites`, `GET /api/presence/rooms` |
| **Peers** (optional outer arc) | federation instances | reachable / unreachable | federation status endpoint (Federation Audit already lists peers) |

Node colour encoding (DESIGN.md tokens, **always paired with a non-colour
channel** per WCAG 1.4.1 — a ring style, icon, or label, never colour alone):

- **Healthy / online / occupied** → `--color-accent-500` turquoise.
- **Degraded / warn** → `--color-primary-300` light crimson.
- **Down / offline** → `--color-primary-700` deep crimson, **plus** a dashed/hollow ring.
- **Unknown / idle** → `--color-gray-400`, hollow.
- **Active role** (this turn) → turquoise fill + the only animated connector.

## Live data

Two channels, no new inference, no new polling burden:

1. **Push (the "alive" part):** the Command Center subscribes to the same chat WS
   stream the app already maintains. On each `done` frame it reads `agent_role`
   and lights that role node + pulses its connector for ~2s. This is the cheap,
   high-impact signal — it makes the board *breathe* with real household activity
   without any extra backend work (role-surfacing already plumbed it).
2. **Poll (the "status" part):** tool-health, satellites, presence, and peers
   poll on the existing React-Query cadence (`refetchInterval`, e.g. 5s for
   presence/satellites which already auto-refresh, 30s for tool-health). No new
   endpoints — all six already exist and are admin-gated.

## Interaction model

**Read-first.** Every node is a **drill-down**, not a control:

- Click a **role** → `/admin/routing` (+ optional pin-role-for-next-turn, reusing
  the existing routing-only `role_hint`; *display* and *pin* are both already
  permission-safe — pinning never escalates, every tool stays gated at execute).
- Click a **tool/MCP** → `/admin/tool-health` (or `/admin/integrations`) filtered
  to that tool.
- Click a **satellite/room** → `/admin/satellites/{id}` or `/admin/presence`.
- Click a **peer** → `/brain/audit` (Federation Audit).

No write actions in v1. (The interactive device widgets already own the
artifact→action write-back channel inside chat; the Command Center stays an
**awareness** surface, which keeps its security surface trivial.)

## Permissions & circles

- **v1 is `Permission.ADMIN`-gated** (route `/admin/command-center`, same
  `<AdminRoute>` + backend `require_permission(ADMIN)` as the six pages it
  composes). No new authz.
- **A future household/kiosk "ambient" mode** (Phase 3) would need a non-admin,
  **circle-aware** projection: a household member sees rooms/presence they're
  permitted to and *never* sees another member's private activity. That is a real
  authz design (reuse the presence service's existing per-user room visibility +
  `circle_sql` rules for anything atom-bearing) — explicitly **out of v1**.
- The live role pulse must **not** leak *content* (no message text, no who-asked
  on a shared display) — it shows only the *role* and *node status*. Keep it that
  way for the kiosk story.

## Interaction states (not designed until these are)

Per the DESIGN.md / chat-roadmap discipline, every node has all four:

| Node | loading | empty | error | live |
|---|---|---|---|---|
| Core | skeleton disc | n/a (always present) | "backend unreachable" banner | active-role label |
| Role | dim outline | role list empty → hide ring | role fetch failed → grey ring + retry | turquoise + pulse |
| Tool | dim outline | no tools configured → hide ring | health fetch failed → all `unknown` | health colour |
| Room/sat | dim outline | **no satellites → warm empty state, not blank** | presence fetch failed → `unknown` | online + occupant dot |
| Peer | hidden until loaded | no peers → omit the outer arc entirely | unreachable → hollow dashed | turquoise |

Global: **offline / GPU-saturated** state (a first-class Renfield condition) must
render as a calm "system busy" core treatment, not an error — it's routine for a
shared-GPU household.

## Where it fits

1. **Phase 1 — admin page** `/admin/command-center`: the unified ops view. Pure
   composition of the six existing endpoints + the chat WS pulse. This is the
   high-value, low-risk slice; it replaces "open six tabs to understand the
   system" with one board.
2. **Phase 2 — live pulse + drill-downs** wired to the real WS stream and the
   admin pages.
3. **Phase 3 — household/kiosk ambient mode** (circle-aware, non-admin,
   content-free): a wall-tablet "what's the house doing" display. This is the
   Renfield-unique payoff — no survey chat UI has satellites/rooms to show — but
   it carries the authz work above and should follow only if the kiosk use case
   is real.

## Relationship to the chat-UI modernization roadmap

The Command Center is **not** a chat-UI roadmap item — it's an **ops/awareness**
surface, a sibling to the admin pages, not part of `/chat`. But it **reuses two
roadmap outputs directly**: role-surfacing (item 6, the `agent_role` on the wire)
for the live pulse, and the `presence_map` data (item 10) for the rooms ring. It
ships independently of the roadmap's open items.

## Open questions (resolve when scheduling Phase 1)

1. **Ring crowding.** 11+ MCP tools on one ring at 375px is unreadable. Mobile
   likely needs a collapsed/list fallback (the constellation is a
   desktop/kiosk-first layout). Define the responsive breakpoint behaviour.
2. **Role↔tool edges.** Apex draws every node to the core. Showing *which tools a
   role can use* (the `agent_roles.yaml` `mcp_tools`/`internal_tools` lists) as
   on-hover edges is richer but risks spaghetti — decide hover-only vs always-on.
3. **Pulse history.** Should the board show only the *current* turn, or a short
   decaying trail of the last N role activations (a heartbeat)? Trail is prettier
   but needs a small client-side ring buffer.
4. **Federation arc.** Include peers in v1 or defer? (Federation is itself
   relatively new; the data exists but the visual adds an outer ring.)
5. **Premise.** Same caveat as the chat roadmap: is an ops board worth building,
   or is the value really the **Phase 3 kiosk**? If the kiosk is the goal, design
   the circle-aware projection first so Phase 1 doesn't bake in admin-only
   assumptions.

## Background moved from CLAUDE.md (2026-09-20)

The must-not-break invariants for kiosk code live in `.claude/rules/kiosk.md`. This section keeps the history and
rationale that used to sit in CLAUDE.md.

### Decommission of the admin board

- The admin Command Center `/admin/command-center` was DECOMMISSIONED 2026-07: its route, page, `AgentConstellation`,
  `useCommandCenterModel`, the `command_center.py` router and the `/api/command-center/*` REST feeds are removed.
- The kiosk is the surviving surface, reachable via the `nav.kiosk` sidebar entry.
- The shared read logic (weather + content-free role activity) MOVED to the kiosk-owned `api/websocket/kiosk_data.py`.
  The ambient tiles described above as `GET /api/command-center/weather` / `…/now-playing` are therefore no longer REST
  endpoints; they arrive in the `/ws/kiosk` snapshot and as `weather_updated` / `now_playing_changed` deltas.
- The plan for the active-subsystem pulse is `tasks/kiosk-active-subsystem-plan.md`.

### What the kiosk shows

A live radial constellation of the running system: the core shows the active agent role + voice state; the rings are
agent roles / MCP tools by health (healthy/degraded/down) / rooms+satellites by presence / federation peers, plus the
active-subsystem pulse. Files: `components/kiosk/KioskConstellation.tsx` + `useKioskModel.ts`, fed by `useKioskSocket.ts`.

### Event-push data path (replaced the react-query poll chain)

- `useKioskSocket.ts` → the ADMIN-gated `/ws/kiosk` hub (`api/websocket/kiosk_handler.py`): ONE content-free `snapshot`
  on connect, then deltas — `satellite_state`, `satellite_online`/`satellite_offline`, `presence_changed`,
  `now_playing_changed`, `tool_health_changed`, `internal_health_changed` (knowledge/presence/media health),
  `weather_updated`, `turn_activity`.
- `turn_activity` is the active-subsystem pulse: which MCP/`internal.*` node lit up this turn, `{subsystem_id, at}`
  derived from `agent_tool_results`, never utterance/entity/user. `mcp.<server>.*` maps to the server node;
  `internal.*` maps through `INTERNAL_SUBSYSTEM_LABELS` to {knowledge, presence, homeassistant, weather, media}.
  (CLAUDE.md located that map in `chat_handler.py`; a comment there says the pulse helpers moved to
  `api.websocket.kiosk_data` so the voice path can emit the same pulse.)
- The kiosk renders synthetic pseudo-nodes for the three internal-only ids knowledge/presence/media
  (`INTERNAL_SUBSYSTEM_NODES` in `useKioskModel.ts`); the other two are real MCP servers.

### Health: why a node can be degraded, and why the success-rate signal is windowed

- `get_status()` folds connectivity AND functionality, so **degraded** = connected but a bound startup plugin failed to
  load [`PLUGIN_MCP_BINDINGS`] or the server exposes 0 tools — a green-reachable-but-functionally-dead node can no
  longer read healthy.
- The secondary per-tool success-rate signal (`tool_health[]` in `build_kiosk_snapshot`) is windowed by
  `_TOOL_HEALTH_RECENT_HOURS` (24h) and `_TOOL_HEALTH_MIN_SAMPLES` (3). The `ToolOutcomeStat` counters are cumulative
  over all time (no window/decay); without the guard a days-old cluster of failures pinned a node red indefinitely even
  after full recovery. The 2026-08-31 xidra kiosk showed `search` stale-red from failures 2-3 days prior while
  `/api/mcp/status` read healthy.
- The pseudo-nodes used to carry pulses only. They now carry a real verdict from `compute_internal_subsystem_health()`
  in `kiosk_data.py`. The presence `degraded` case — an enrolled satellite that is connected but unauthenticated gets no
  IRK push, so presence goes silently blind — is the 2026-07-09 failure mode. The verdict ships in the snapshot plus a
  diff-gated `internal_health_changed` delta from a `_kiosk_clients`-gated backend refresher (same no-poll model as the
  weather tile). The new `'off'` `NodeHealth` renders muted.

### Liveness is backend-authoritative — and until #1209 it was only a promise

- The backend pushes `satellite_offline` on unregister/heartbeat-timeout. The heartbeat sweep
  (`SatelliteManager.cleanup_stale`) is scheduled from `ha_glue.bootstrap._schedule_satellite_cleanup`. Until #1209 it
  had NO caller at all, so a satellite that vanished without closing its socket stayed "online" until the pod restarted.
- Scheduling the sweep armed two dormant timeouts at once, so both were calibrated in the same change (session timeout =
  max RECORDING duration; eviction exemptions + socket close — see the rule file).
- The session timeout is only a BACKSTOP for a hung device. The binding recording limit is the satellite's own
  `vad_max_recording_seconds` (fleet 60 s), which ends the turn via `audio_end` and measures the turn with the
  `_recorded_chunks` counter. Until v1.4.9 it read the length of a write-only audio buffer capped at 500 chunks = 40.0 s,
  so after the 20 → 60 s raise the limit silently never fired and the recording died unanswered in this 120 s backstop
  (the 2026-09-19 regression; the buffer is removed — never derive a duration from a bounded container).
- All four knobs live in `k8s/configmap.yaml`. #1277 tracks the still-unscheduled `DeviceManager` sweep. A resumed
  satellite is reinstated via `satellite_online`.
- Consequence for the frontend: NO wall-clock decay of frozen snapshot values; a reconnect re-anchors from a fresh
  snapshot. Federation peers get the live `peer_status_changed` delta (#969, `kiosk_data.py` → `useKioskSocket.ts`);
  the wall-clock freshness backstop (`PEER_OFFLINE_MS` in `useKioskModel.ts`) is kept as a deliberate second guard.
