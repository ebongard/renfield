# Design: Generic Output Providers (room media/control routing)

**Status:** Reviewed (eng-review 2026-06-07) — decisions locked, ready to implement
**Author:** Eduard + Claude
**Motivates:** Samsung TV MCP integration (`renfield-mcp-samsung`); incoming Sonos / other-brand TV integrations.

## Locked decisions (eng-review 2026-06-07)

1. **Sequencing:** big-bang PR for phases 1-4 behind `OUTPUT_PROVIDERS_ENABLED` (dark). The schema change is **additive-only** here (add `output_provider`+`output_target_id`, backfill, dual-read); the **destructive `DROP COLUMN` cleanup ships as its own follow-up PR** after the additive schema has soaked in prod (a flag can't protect a column drop — outside-voice point). Samsung agent-only ships in parallel regardless.
2. **Discovery fan-out:** `asyncio.gather` the providers' `discover()` concurrently, each wrapped in a per-provider timeout. A provider that times out or raises is **surfaced as a degraded/unreachable entry**, NOT silently omitted (+ `..._unreachable_total` counter). Reuses the auth-registry *resilience mechanism* (`auth/registry.py`) but NOT its drop-on-failure UX: an asleep TV must still appear in the room list (greyed "unreachable"), because output discovery is a control surface — hiding an expected device is a silent failure (outside-voice point).
3. **Power-on:** after `control(on)`, **poll `status()` until ready** (state != off) up to a provider-declared `boot_timeout` (default ~20s), then `play()`. Never wakes → honest `could not wake <device>` error, never a fake success.
4. **Media contract:** `play()` always takes a **track list** (`items: [media_ref, ...]`, len 1 for a single track); gapless is provider-optional (a `queue` capability). dlna keeps `SetNextAVTransportURI` gapless; no album regression.
5. **Vocabulary:** `output_provider` IS the existing `target_type` value space (`renfield`|`homeassistant`|`dlna`|`samsung`|…). One source of truth; `target_type` property returns `output_provider`. No parallel naming.
6. **Protocol:** built-ins and MCP providers implement one `OutputProvider` Protocol; built-in `renfield`/`homeassistant` adapters **wrap** the existing `OutputRoutingService` methods, they do not reimplement availability logic.

## Problem

Room output routing is hardcoded to exactly three sources. Adding a device
brand currently means touching four places:

1. **Model** — `RoomOutputDevice` carries three nullable identity columns:
   `renfield_device_id`, `ha_entity_id`, `dlna_renderer_name`
   (`output_routing_service.py:284-321`). A 4th brand = a 4th column.
2. **Discovery** — one method per source: `get_available_renfield_devices`,
   `get_available_ha_media_players`, `get_available_dlna_renderers`; the last is
   wired specifically to `mcp.dlna.list_renderers` (`:455`). A 4th brand = a 4th
   method + a hardcoded MCP call.
3. **Dispatch** — brand-specific internal tools: `internal.play_album_on_dlna`,
   `internal.play_video_on_dlna`, `internal.play_from_server`
   (`ha_glue/services/internal_tools.py`). A 4th brand = more brand tools.
4. **Frontend** — `RoomOutputSettings.tsx` branches per type (renfield / HA /
   dlna). A 4th brand = a 4th UI branch.

This does not scale to Samsung + Sonos + LG/Roku/…. We want a generic layer so a
new output device is **config + a small contract implementation**, not a
four-place code change. There is also no **power-on** step today (a TV must
already be awake), which the Samsung server's Wake-on-LAN could provide.

## Goals

- Adding an output device brand = (a) an MCP server implementing a small
  contract + (b) one declaration in `mcp_servers.yaml`. **Zero** backend-routing,
  model, or frontend code per brand.
- Uniform discovery, selection, playback, and (new) power-on across all
  providers (built-in + MCP).
- Capability-aware: a target advertises `audio` / `video` / `power` / `transport`
  so the UI and dispatch only offer what a device can do.
- Backward compatible: existing renfield/HA/dlna room assignments keep working
  through the migration.

## Non-goals

- Not changing *what* media is played or the Jellyfin/queue logic — only *how a
  room's output target is discovered, stored, selected, and dispatched to*.
- Not solving device-specific limitations (e.g. a Samsung's `:9197` DLNA renderer
  being down means no media to that set via *any* server — a TV/network limit).
- Not a general HA-replacement device framework; scope is media/AV output + the
  power-on convenience.

## What already exists (the refactor is mostly rewiring, not greenfield)

The eng-review grounded these against the code — the generic entry points
already exist in hardcoded form, which lowers risk (Beck: make the change easy,
then make the easy change):

- **`internal.play_in_room`** already exists (`ha_glue/services/internal_tools.py:334`)
  and already resolves room → `OutputDecision` → dispatch. The "generic resolver"
  is a refactor of its existing `if target_type == "dlna" / elif ha` branching,
  not a new tool.
- **`RoomOutputDevice.target_id` / `target_type`** computed properties already
  normalize the three columns at read time (`ha_glue/models/database.py:340-352`).
  The model abstraction exists; the migration just *persists* the pair so a brand
  with no column (samsung) can be stored.
- **`GET /{room_id}/available-outputs`** already aggregates
  (`ha_glue/api/routes/rooms.py:896`) — it calls three hardcoded methods. The
  change swaps the body for a registry loop; the route + response stay.
- **`_media_control`** already dispatches dlna/ha through `target_type`
  (`internal_tools.py:744`) — same refactor target as `play_in_room`.
- **`RoomOutputSettings.tsx`** already uses `useTranslation()` throughout.

So the work is: introduce the `OutputProvider` registry/Protocol, point the four
existing seams at it, add the persisted `(provider, target_id)` columns, and make
the UI render the aggregated list. The built-in adapters wrap the existing
`OutputRoutingService` methods (`_check_device_availability`,
`get_available_renfield_devices`, `get_available_ha_media_players`).

## Architecture

### 0. OutputProvider Protocol (the uniformity seam)

```python
class OutputProvider(Protocol):
    key: str                      # == target_type: "renfield" | "dlna" | "samsung" | ...
    capabilities: set[str]        # {"audio","video","power","transport","queue"}
    boot_timeout: float           # power-on poll cap (seconds); 0 if no power cap
    async def discover(self) -> list[OutputTarget]: ...
    async def play(self, target_id: str, items: list[MediaRef], mode: str) -> PlayResult: ...
    async def control(self, target_id: str, action: str, value=None) -> ControlResult: ...
    async def status(self, target_id: str) -> TargetStatus: ...
```

Two impl families: **built-in** (`RenfieldProvider`, `HomeAssistantProvider`
wrapping `OutputRoutingService`) and **`McpOutputProvider`** (one instance per
declared `output_provider` stanza, mapping the four contract methods onto
`mcp.<server>.<tool>` calls). The aggregator and dispatcher see only the Protocol.

### 1. Output provider

A **provider** is a source of controllable output targets. Two kinds:

- **built-in** — the Renfield device registry (satellites/panels with speakers).
  Kept as a provider with a fixed adapter (no MCP).
- **MCP-declared** — `dlna`, `samsung`, `sonos`, `homeassistant`, … declared in
  `mcp_servers.yaml` so a new brand needs config, not code:

```yaml
- name: samsung
  # ... existing transport/url/enabled ...
  output_provider:
    capabilities: [video, audio, power, transport]
    discover: tv_discover     # → {targets: [{id, name, capabilities[], room_hint?}]}
    play:     tv_media        # ({target_id, media_url|item_ref, mode}) → status
    control:  tv_power        # ({target_id, action: on|off|key, ...})
    status:   tv_media        # ({target_id}) → {state, position?}
```

The platform reads `output_provider` stanzas to build a **provider registry**
(built-ins + declared MCP providers).

### 2. Capability contract (normalized I/O)

Every provider's contract tools speak the **same shapes** so the backend never
special-cases a brand:

- `discover() → { targets: [{ id, name, capabilities: ["audio"|"video"|"power"|"transport"], room_hint? }] }`
- `play({ target_id, media_url? , item_ref?, mode: "now"|"queue" }) → { ok, state }`
- `control({ target_id, action: "on"|"off"|"key"|"volume"|"mute", value? }) → { ok }`
- `status({ target_id }) → { state: "playing"|"paused"|"stopped"|"off", position? }`

A provider only declares the tools it has; missing tools = missing capabilities.
(dlna: audio+video+transport, no power. samsung: + power via WoL. sonos: audio.)

### 3. Target identity + room association

Replace the three nullable columns on `RoomOutputDevice` with a **provider +
target id** pair per slot:

- `output_provider: str`     (e.g. `renfield`, `dlna`, `samsung`, `homeassistant`)
- `output_target_id: str`    (provider-scoped id: device_id / renderer name / entity id / TV host)
- keep the existing `output_type` audio/visual split, priority, interruption, volume.

**Migration** backfills existing rows:
`renfield_device_id` → (`renfield`, id); `ha_entity_id` → (`homeassistant`, entity);
`dlna_renderer_name` → (`dlna`, name). Old columns dropped after a dual-read window.

### 4. Aggregated discovery

`GET /api/rooms/{id}/available-outputs` iterates the provider registry, calls each
provider's `discover` (parallel, per-provider timeout; unreachable → degraded
entry, not dropped), and returns the **capability-tagged union**, normalized to
`{ provider, target_id, name, capabilities[], reachable }`. A new provider appears
automatically — no endpoint change. Per-provider `discover()` results are cached
with a short TTL in the registry so repeated room-settings renders don't re-probe.

### 5. Generic dispatch (+ power-on)

Replace the brand-specific `internal.play_*_on_dlna` tools with one resolver:

`play_in_room(room, capability=video|audio, media)`:
1. Resolve the room's output target for that capability → `(provider, target_id)`.
2. If the provider has the `power` capability and `status` reports `off` →
   call `control(on)` first (the Wake-on-LAN-before-play the room view wanted).
3. Call the provider's `play` with the normalized media ref.

Volume/keys/power from the agent or UI route through `control` on the resolved
provider. The agent still has the raw `mcp.<brand>.*` tools for brand-specific
extras (apps, captions, cursor) — the contract covers only the common surface.

### 6. Frontend

`RoomOutputSettings.tsx` becomes data-driven: render the aggregated
`available-outputs` list grouped by provider with capability badges; selection
stores `(provider, target_id)`. One component; a new brand needs **no** UI
change. (The file already uses `useTranslation()` throughout — no i18n debt to
clear here; any new strings go in both `de.json` + `en.json` as usual.)

## How existing sources port onto it

- **dlna** — add an `output_provider` stanza (`discover: list_renderers`,
  `play: play_tracks`/`play_video`, `status: status`); capabilities
  `[audio, video, transport]`.
- **homeassistant** — built-in adapter wrapping the `media_player` domain
  (discover = list media_players; play/control = HA services).
- **renfield devices** — built-in adapter over the device registry (audio only).

## How new brands slot in (the payoff)

- **samsung** — implement the 4 contract tools (it already has `tv_discover` /
  `tv_media` / `tv_power`); add the stanza; capabilities `[video, audio, power,
  transport]`. Power-on-before-play works generically. Appears in the room view
  automatically.
- **sonos** — a future `renfield-mcp-sonos` implementing the contract; declares
  `[audio, transport]`; appears as an audio target. Zero platform code.

## Rollout / migration plan (phased, behind a flag)

**This PR (phases 1-4, behind `OUTPUT_PROVIDERS_ENABLED`):**

1. **Contract + registry + aggregation.** `available-outputs` can dual-source
   (old methods + new registry) and de-dup, so nothing breaks.
2. **Model migration — ADDITIVE ONLY.** Add `output_provider` + `output_target_id`,
   backfill from the three columns, dual-read (prefer new, fall back to old).
   The old columns stay in place this PR.
3. **Dispatch** — `play_in_room` routes via the registry; keep the old
   `internal.play_*_on_dlna` as thin shims delegating to it.
4. **Frontend** — switch `RoomOutputSettings` to the aggregated list.

**Follow-up PR (after soak):**

5. **Destructive cleanup** — drop the three columns + the brand-specific internal
   tools + the per-source discovery methods once dual-read has soaked in prod.
   Separate PR so the irreversible `DROP COLUMN` is never bundled with the
   feature and can be reverted independently.

## Outside-voice adjustments (noted, not yet design changes)

The independent challenge raised three more points kept as implementation
guidance rather than re-litigated decisions:

- **Power-on readiness must check the *media endpoint*, not just power state.**
  Samsung reports "on" before its app/input is ready (the `state=playing ≠ audio`
  class). `boot_timeout` is tuned against the real device, and "ready" means the
  play target responds — not merely that the panel powered on.
- **Agent-vs-deterministic divergence window.** While the flag is dark, Samsung is
  reachable via raw `mcp.samsung.*` (agent) but not `play_in_room`. Accepted as a
  transient soak-window state; flipping the flag closes it. Don't let the two
  control planes drift in capability semantics.
- **Single layer owns availability.** `OutputRoutingService` remains the sole owner
  of availability/power semantics; the built-in providers *delegate* to it (no
  second copy of the logic in `discover()`/`status()`).

## Resolved questions (from eng-review)

- **Built-in vs MCP uniformity** — RESOLVED: one `OutputProvider` Protocol (§0);
  built-ins wrap `OutputRoutingService`, MCP providers via `McpOutputProvider`.
- **Media-ref normalization** — RESOLVED: contract carries `items: list[MediaRef]`
  (len 1 = single track); gapless is a provider-optional `queue` capability. dlna
  keeps `SetNextAVTransportURI`. No album regression.
- **Discovery resilience** — RESOLVED: parallel `discover()` with per-provider
  timeout, skip-fail-open + counter (auth-registry pattern).
- **Power-on** — RESOLVED: poll `status()` until ready up to `boot_timeout`, then
  play; never-wakes → honest error.
- **Migration safety** — RESOLVED: standard dual-read window + a real
  `alembic upgrade head` integration test against Postgres (repo migration rule).

## Still-open questions (advisory, not blocking; lean noted)

- **room_hint vs explicit assignment** — lean manual; `room_hint` advisory only.
- **Power-off policy** — lean power-on only; power-off stays explicit.
- **Circle/permission on output targets** — out of scope; output targets stay
  un-tiered (a household member can route to any room output, as today).

## Test plan (all paths tested with the implementation)

CRITICAL (must land in the PR):
- **Migration** — real `alembic upgrade head` against PG backfilling each of the
  three columns → `(provider, target_id)`; assert old + new rows resolve identically.
- **Dual-read** — row with only old columns reads via fallback; row with new
  columns prefers them.
- **Fan-out fail-open** — aggregator with one provider that times out / raises
  returns the other providers' targets + increments the unreachable counter.
- **Power-on path** — `status==off` → `control(on)` → poll → `play`; already-awake
  skips the wait; never-wakes within `boot_timeout` returns the honest error.
- **Shim regression** — `internal.play_album_on_dlna` / `play_video_on_dlna` /
  `play_from_server` delegating to the generic resolver produce identical results
  to today (guards existing agent flows).

Unit / RTL:
- Registry build from `mcp_servers.yaml` (valid stanza, malformed stanza skipped).
- Built-in adapters wrap the existing service methods (no logic duplication).
- Contract conformance per provider (the four methods match the normalized shapes).
- Multi-item gapless on dlna vs single-item.
- Frontend renders the aggregated list grouped by provider with capability badges;
  selection stores `(provider, target_id)`.

## NOT in scope

- room_hint auto-binding, auto-power-off, circle-tiering of output targets (above).
- Changing *what* media plays or the Jellyfin/queue logic — only how a room's
  target is discovered/stored/selected/dispatched.
- A general HA-replacement device framework — scope is media/AV output + power-on.
- Distribution: no new artifact type (the Samsung MCP image is the separate
  agent-only PR; this design ships inside the existing backend image).
- The destructive `DROP COLUMN` + shim/method removal — deferred to a follow-up PR
  after prod soak (tracked in TODOS.md), per the additive-only decision.

## Out of scope (separate, ships now)

The Samsung MCP as an **agent-only** integration (chat-driven control + auto
DLNA-renderer when `:9197` is up) ships independently of this design — it needs
no room-view change and makes the TV usable while this architecture is built.
This doc is the foundation that later makes Samsung/Sonos/etc. *room-selectable*
uniformly.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 8 issues, 0 critical gaps remaining |
| Outside Voice | Claude subagent | Independent 2nd opinion | 1 | issues_found | generalize-from-1 + migration-risk challenge; 2 refinements adopted |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not run |

- **OUTSIDE VOICE:** Independent Claude subagent (Codex not installed). Adopted two refinements — (1) migration split to additive-only + destructive cleanup in a follow-up PR; (2) discovery surfaces unreachable providers as degraded entries instead of dropping them. Three further points kept as implementation guidance (media-endpoint readiness, agent/deterministic divergence window, single availability owner). The "add a 4th branch instead of a framework" recommendation was noted but NOT adopted — the generic direction is a settled user decision with a real forcing function (Sonos + other brands incoming).
- **CROSS-MODEL:** Review accepted big-bang; outside voice forced the irreversible `DROP COLUMN` out of the feature PR — net improvement, both now agree on additive-then-cleanup.
- **UNRESOLVED:** none.
- **VERDICT:** ENG CLEARED — design decisions locked, test plan complete, ready to implement. Big-bang feature PR (phases 1-4, additive schema, behind `OUTPUT_PROVIDERS_ENABLED`); destructive cleanup tracked as a P2 follow-up.
