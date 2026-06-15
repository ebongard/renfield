# Plan — Command palette (chat-ui roadmap item 4)

Source: blueprint from feature-dev:code-architect agent (2026-06-15). `/` in the
composer (or a touch button) opens an action+navigation palette. Dark-flagged.

## Locked decisions (with user)
- Tool actions **stage into the composer** (no auto-send); user reviews + sends.
  Navigate = immediate client nav. Set-role = stage a next-turn role hint.
- Role hint = **next-turn only**, auto-clears on `done` (dismissible badge).
- **Build now, dark** behind `command_palette_enabled` (frontend flag; backend
  `role_hint` is always-present no-op when absent → flip needs no backend redeploy).

## Authz model
- **Display** (frontend, UX courtesy): `usePaletteActions` filters the static
  registry by `AuthContext.hasPermission/hasAnyPermission` + `isFeatureEnabled`.
- **Execute** (real gate, server-side, unchanged): tool actions dispatch as
  natural language via `sendMessage` → `agent_router` → `agent_roles.yaml` tool
  gating → `require_permission`. The palette adds NO new dispatch path.
- `AUTH_ENABLED=false` (prod) → hasPermission returns true → all actions visible.

## Backend
- [ ] `utils/config.py` + `api/routes/config.py` (FeatureFlags) — `command_palette_enabled: bool = False`.
- [ ] chat WS handler — accept optional `role_hint` on the `text` message; validate
  against `agent_roles.yaml` role keys; pass as a soft routing hint to the router;
  drop silently if unknown. Always present (no flag). Unit test.

## Frontend
- [ ] `components/chat/palette/paletteActions.ts` (NEW) — static `PaletteAction[]`.
- [ ] `components/chat/palette/usePaletteActions.ts` (NEW) — filter by perms + flag + query.
- [ ] `components/chat/palette/PaletteContext.tsx` (NEW) — open state + pendingRoleHint.
- [ ] `components/chat/palette/CommandPalette.tsx` (NEW) — dialog overlay (portal),
  search + grouped listbox, Arrow/Enter/Esc, focus trap+restore, warm empty state,
  44px, dark + i18n. Navigate→useNavigate; tool→setInput (stage); set-role→hint.
- [ ] `ChatInput.tsx` — `/`-key when empty + touch trigger button + role-hint badge.
- [ ] `ChatPage/index.tsx` — PaletteProvider + mount CommandPalette portal (flag-gated).
- [ ] `ChatContext.tsx` — inject `role_hint` from pendingRoleHint into the WS message; clear on `done`.
- [ ] i18n de+en — `chat.palette.*`.

## Tests
- [ ] Backend unit: role_hint accepted (valid) / dropped (unknown) / None passes.
- [ ] Frontend RTL `usePaletteActions.test.tsx` + `CommandPalette.test.tsx`.

## Out of scope (v1)
- localStorage recents; per-user customisation; direct mcp.* dispatch; voice-trigger;
  admin-management actions; multi-step-input actions.

## Status: IMPLEMENTED
- Backend: `command_palette_enabled` flag (config + FeatureFlags route); `role_hint`
  on WSChatMessage → `classify_with_context` Layer-0 short-circuit (valid role) →
  passed from chat_handler. Always-present (no flag); unknown hint falls through.
- Frontend: palette state folded into ChatContext (open + pendingRoleHint, role_hint
  injected on the WS frame + consumed next-turn); `paletteActions.ts` registry +
  `usePaletteActions` perm/flag/query filter + `CommandPalette.tsx` overlay (portal,
  search, arrow/enter nav, document-level Escape, focus restore, 44px, dark+i18n);
  ChatInput `/`-trigger + touch button + role badge; mounted in ChatPage; i18n de+en.
- Tool actions STAGE into composer (no auto-send); role hint next-turn-only; UI gated by flag.

## Review
- Authz: display filter is frontend UX (hasPermission); real gate stays server-side
  (NL command → agent_router → agent_roles.yaml tool gating). No new dispatch path.
- Improved on blueprint: palette state lives IN ChatContext (a nested PaletteContext
  couldn't feed role_hint into ChatContext.sendMessageInternal). Escape made
  document-level (robust modal close, not focus-dependent).
- Tests: backend role_hint short-circuit (passed; 3 config-loading failures are
  PRE-EXISTING — they read the ConfigMap-served agent_roles.yaml absent from the
  build-box image). Frontend 18/18 (palette 7 + usePaletteActions 4 + chips 7). tsc clean.
- Pending: `npm run build` (prod Tailwind) + `/review`.
