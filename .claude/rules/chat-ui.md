---
paths:
  - "src/backend/api/websocket/chat_handler.py"
  - "src/backend/services/artifact_service.py"
  - "src/backend/services/widget_tools.py"
  - "src/backend/services/followup_service.py"
  - "src/frontend/src/components/chat/**"
  - "src/backend/services/agent_router.py"
---
# Chat UI affordances & typed artifacts

Loaded only when the chat handler, artifact/widget/follow-up services or a chat component is read. Branching:
`chat-branching.md`. Long form: `docs/design/chat-ui-modernization.md`, `docs/design/chat-artifacts-sandbox.md`.

## Flags (config default)
`FOLLOWUP_CHIPS_ENABLED`, `COMMAND_PALETTE_ENABLED` (frontend-only gate), `ROLE_SURFACING_ENABLED`,
`MESSAGE_SEARCH_ENABLED`, `ARTIFACTS_TYPED_ENABLED` — all dark. `ARTIFACTS_HTML_SANDBOX_ENABLED` = Lane B placeholder,
**deferred and not wired** (needs its own security review). Provenance chips are always on. The weather widget also
needs `WEATHER_ENABLED`.

## Turn frames
- **Provenance chips:** `knowledge_tool` emits `data.sources`; `chat_handler._extract_agent_sources` rides them on the
  `done` frame + `message_metadata.sources`. Circle-safe ONLY because the sources come from `rag.search(user_id)`.
- **Follow-up chips** (`services/followup_service.py`): best-effort small-model call in the background AFTER the `done`
  frame, via a separate `followups` frame — never delay the spinner/TTS/wakeword. Skipped on TTS/error/very-short turns.
- **`role_hint` is ROUTING-ONLY** (palette, role badge via `pendingRoleHint`, "Neu beantworten" via `corrected_intent` →
  `agent_router.role_for_intent`): validated against `agent_roles.yaml` in `agent_router` Layer-0. Every tool stays
  permission-gated at execute time — a hint never escalates. Palette tool actions STAGE into the composer, no auto-send.
- The agent role rides the `done` frame and persists as `message_metadata.agent_role` (rehydrates on history reload).

## Message search
`GET /api/chat/messages/search`: Postgres FTS on the GENERATED `messages.search_vector` (migration `pc20260617`), ranked
by `ts_rank`. Scoped strictly by **conversation ownership** — messages are NOT atoms, so deliberately NOT routed through
`circle_sql`. Highlighting is sentinel→`<mark>` (XSS-safe); never pass raw DB/headline markup to the DOM.

## Typed artifacts (Lane A)
- Typed JSON → real React components. **NO model HTML/SVG**; React's escape boundary is the whole security story (same
  model as `AdaptiveCardRenderer`). `chart` is hand-rolled SVG from typed series, no charting dependency.
- Widget data = tool ARGS validated by `artifact_service.validate_artifact` — NEVER parsed from the agent's free text.
- Validation is split by concern: backend `services/artifact_service.py` = kind allowlist + size/row/series/point caps
  (DoS gate); frontend `artifactSchema.ts` (zod discriminated union) = authoritative shape.
- **Fail-closed:** invalid shape / throwing sub-renderer / unknown kind / stuck `partial` → escaped code-block
  fallback, never raw markup. Valid-but-empty → per-kind empty state.
- One path for every producer: validate → emit → persist. Hook/sub-intent/orchestration → `_emit_turn_artifacts`;
  render tools (`services/widget_tools.py`) return `data={"artifacts":[…]}`, gathered by
  `chat_handler._collect_tool_artifacts` from `agent_tool_results`.
- Persisted as `message_metadata.artifacts[]` keyed by `id`; a same-`id` streaming patch must stay idempotent.
  Lane A relies on the enforcing baseline CSP in `nginx.conf`.

## `device_action` write-back (first artifact→action channel)
- The WS frame is intercepted BEFORE `WSChatMessage` validation (like the Paperless-confirm card).
- **Fail-closed on `Permission.HA_CONTROL`:** a device/satellite token with `user_id=None` is denied when auth is on;
  only auth-disabled single-user mode actuates without a permission list. The widget grants nothing the agent path
  would not.
- `internal.device_action` re-validates domain (`_CONTROL_DOMAINS`: light/switch/scene/climate) + action
  (`_DOMAIN_ACTIONS`) + entity existence (`get_state` probe) before `call_service` — a crafted frame must not reach an
  arbitrary service.
- It is in `_HANDLERS` only, NOT `TOOLS`: frame-dispatched, never agent-advertised. Keep it that way.
- Numeric `value` is bool-excluded and clamped server-side: brightness 0-100, temperature to the entity's
  `min_temp`/`max_temp`. The result frame echoes `brightness`/`targetTemp` so the widget reconciles.
- `internal.device_controls` reads fresh `get_states()`, not the 60s entity-map cache (initial values must be real).
