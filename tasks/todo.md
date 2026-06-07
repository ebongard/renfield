# Generic Output Providers — implementation plan

Branch: `feat/output-providers`. Source of truth: `docs/design/output-providers.md`
(eng-reviewed 2026-06-07). Big-bang feature PR, phases 1-4, all behind
`OUTPUT_PROVIDERS_ENABLED` (additive schema only; destructive cleanup is a P2
follow-up). Flag OFF = byte-identical to today.

## Phase 1 — Foundation (config + Protocol + MCP registry)  ✅ DONE (20/20 tests green on .159)
- [x] P1.1 `config.py`: `output_providers_enabled: bool = False`
- [x] P1.2 `mcp_client.py`: `MCPServerConfig.output_provider: dict | None = None` + parse in `load_config`
- [x] P1.3 new `ha_glue/services/output_providers.py`:
      - dataclasses: `OutputTarget`, `MediaRef`, `PlayResult`, `ControlResult`, `TargetStatus`
      - capability constants (audio/video/power/transport/queue) + `OutputProvider` Protocol
      - `McpOutputProvider` (maps the 4 contract methods onto `mcp.<server>.<tool>`, envelope+shape normalize)
      - `build_mcp_output_providers(mcp_manager)` → {key: McpOutputProvider} from stanzas
      - NOTE: built-in renfield/HA adapters deferred to Phase 4 (aggregation), where their
        discover() is consumed + dispatch wiring exists — not shipping half-baked stubs.
- [x] P1.4 unit tests (`tests/backend/test_output_providers.py`): 20 tests — registry build,
      capability parsing (known + unknown-kept), stanza validation (malformed/no-discover skipped),
      McpOutputProvider discover/play/control/status normalization, tool-failure + transport-error
      → OutputProviderError, payload extraction, target-shape normalization (contract + legacy).

## Phase 2 — Additive model migration + dual-read  ✅ DONE (11 PG + 35 routing tests green on .159)
- [x] P2.1 `RoomOutputDevice`: add `output_provider` + `output_target_id` columns (nullable)
- [x] P2.2 migration `pc20260610_output_target` (additive add cols + CASE/COALESCE backfill; NO drop)
- [x] P2.3 `target_id`/`target_type` props prefer new cols, fall back to old (dual-read)
- [x] P2.4 `add_output_device` dual-writes the pair from a legacy arg + accepts an explicit
      `(provider, target_id)` path (samsung, no legacy col); CRUD create/response schemas + route gain the pair
- [x] P2.5 real-PG tests (`test_output_providers_phase2_pg.py`): backfill SQL across all 3 legacy
      types + idempotent skip-already-paired, dual-read (prefer/fallback/both/empty), add_output_device
      dual-write + explicit-pair + 3 validation paths. Full plugin-aware `alembic upgrade` verified at
      deploy time (repo convention: ha_glue room tables live in a separate plugin migration tree, so a
      core-only from-scratch upgrade can't build them — backfill LOGIC tested via create_all'd PG instead).

## Phase 3 — config-driven adapter + generic dispatch + power-on  ✅ DONE (58 tests; 0 regressions)
- [x] P3a config-driven MCP adapter (bridge in renfield, not in MCP servers) — McpOutputProvider
      maps contract↔native tools via per-method arg-templates; control per-action; 25 tests
- [x] P3.1 `_resolve_room_player` + `_play_in_room` route via registry when flag on (samsung);
      dlna/HA/renfield keep their legacy branches. `_check_device_availability` uses dual-read
      target_type so generic-provider rows resolve as available.
- [x] P3.2 power-on: status off/unreachable → control('on') → `_poll_provider_ready` (bounded by
      boot_timeout, iteration-based) → play; never-wakes → honest "could not wake" error
- [x] P3.4 samsung `output_provider` stanza (quoted "on"/"off" keys — YAML-bool footgun; parser
      also coerces bool keys defensively). dlna stays legacy (gapless capability preserved).
- [x] P3.5 tests: 9 dispatch (already-on / power-on / never-wakes / control-fail / play-fail /
      non-power / poll bounds) + 25 adapter; existing internal_tools = same 6 pre-existing
      stale-tree failures with AND without my change (proven zero regression).
- N/A P3.3 shims: `internal.play_*_on_dlna` unchanged (dlna stays legacy this phase); they delegate
      to the generic resolver only when dlna itself moves onto the contract (deferred w/ dlna).

## Phase 4 — Aggregation + frontend  ✅ DONE (5 aggregation + 4 RTL tests; typecheck clean)
- [x] P4.1 `get_aggregated_outputs` (built-ins via existing methods + registry providers via
      parallel `asyncio.gather` discover, per-provider `output_provider_discover_timeout`,
      **degraded-not-dropped**); `available-outputs` returns `output_targets` when flag on (None off).
- [x] P4.2 `RoomOutputSettings.tsx` data-driven: generic mode (output_targets present) → one unified
      picker over all providers + capability badges + unreachable-disabled + submits (provider,
      target_id) pair; flag off → byte-identical legacy type-buttons. `roomOutputs.ts` types extended.
- [x] P4.3 RTL (`RoomOutputSettings.test.tsx`, 4 tests): unified picker / no type-buttons / unreachable
      disabled / submits the pair / legacy fallback when output_targets absent.

## Gate
- Flag OFF byte-identical ✅ (verified: dual-read identical, dispatch/aggregation gated, frontend legacy path).
- Real-PG for migration ✅ (backfill logic + dual-read on real PG; full plugin-aware upgrade at deploy).
- Totals: 85 backend + 4 RTL tests green; existing internal_tools = same 6 pre-existing stale-tree fails.
- NEXT: /review + adversarial → docs sweep → one big-bang PR.

## Review (filled at the end)
_(pending — run /review on the full branch diff)_
