---
paths:
  - "src/backend/services/user_events.py"
  - "src/backend/api/websocket/user_events_handler.py"
  - "src/frontend/src/hooks/useUserEvents.ts"
---
# User-Events WebSocket (`/ws/user`)

Loaded only when a user-events file is read. Long form + threat model: `docs/design/user-events-ws.md`.
Flag `USER_EVENTS_ENABLED` — **default ON** (a kill-switch, not dark). Off ⇒ no router include, no subscriber,
byte-identical to before. Exposed to the browser via `/api/config/features` (`user_events_enabled`).

## Never
- **Emitters NEVER touch the registry.** Every emitter `PUBLISH`es to the ONE Redis channel `renfield:events:user`
  (`publish_user_event`, `emit_documents_changed`); each API pod runs ONE subscriber
  (`_schedule_user_events_subscriber` in `api/lifecycle.py` → `run_user_events_subscriber`) that fans out to ITS local
  `UserEventRegistry`. Reason: ingest completion runs in the `document-worker` pod, which cannot reach the API pod's
  in-memory sockets — and every replica subscribes, so this is the only shape that is worker-reachable AND replica-safe.
- **Payload is content-free:** `{type, reason}` — no doc id, title or filename. The browser's follow-up fetch is
  re-filtered server-side, which is what keeps it circle/privacy-safe.
- **Emit points are best-effort and after-commit** — an emit failure must never break the caller.

## Targeting
- `fan_out(target=<int>)` → ONLY that user's sockets (a normal event never spams admins).
  `fan_out(target=None)` → ONLY the `_ALL` bucket.
- `_ALL` = auth-off household sockets + (auth-on) admin sockets, so an unattributable change (`owner=None`, e.g.
  null-KB) still reaches someone.
- Auth-off (`auth_enabled=false` — the single auth flag; `WS_AUTH_ENABLED` is retired): sockets register under `_ALL`
  and `emit_documents_changed` forces `target=None`. Owner lookup: `resolve_document_owner` (prefers the atom owner).

## Events and emit points
- `documents_changed`, reasons `ingested` / `paperless` / `deleted`: `rag_service.py` ingest-complete (worker) +
  `delete_document`; `folder_ingest_paperless.py` — all 5 terminal `paperless_state` writes via
  `_emit_paperless_changed`.
- **A new event** (`obligations_changed`, `notes_changed`, …) = a new `type` + a frontend `case`. No new socket,
  no new pub/sub plumbing.
- Storm control is two-sided: server `EventCoalescer` window (`USER_EVENTS_COALESCE_WINDOW_SECONDS`, 1 s) AND a client
  debounce. The frontend invalidates `keys.knowledge.list()` only.

## Frontend
`hooks/useUserEvents.ts` opens `/ws/user` via `/api/ws/token` + `getWebSocketUrl`, heartbeat ping, jittered reconnect
tolerant of deploy-window WS 404s. Mounted once, app-wide, in `App.tsx` `AppRoutes`, gated
`user_events_enabled && (!authEnabled || isAuthenticated)`.
