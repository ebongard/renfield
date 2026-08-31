# User-Events WebSocket — per-user server→browser push substrate

**Status:** Design (approval-gated, not yet built). 2026-08-31.
**Motivation:** `/wissen/dokumente` (and other corpus surfaces) do not reflect server-originated changes (folder/email ingest completion, Paperless/Simba status) without a manual reload — and **polling is explicitly rejected**. This introduces a **reusable per-user event channel** so any backend surface can push a content-free "something changed, refetch" signal to that user's open browsers.
**Constraints (owner-set):** no shortcuts; robust long-term architecture; minimal tech debt; ≥80% test coverage.
**Supersedes:** the naive "in-memory broadcast like the kiosk" proposal, which an adversarial review proved **broken** for the primary trigger (see §2).

---

## 1. Goal & non-goals

**Goal.** A single, general **per-user** server→browser push channel (`/ws/user`) plus a **cross-process Redis pub/sub bridge**, so that:
- the document worker (a *separate pod*), the API pod, and background reconcilers can all emit an event that reaches the right user's open browser tabs;
- the browser reacts by **invalidating the relevant React Query key** (no polling, no manual reload);
- the substrate is **reusable** (documents now; obligations/notes/KG later) without re-architecting.

**Non-goals.**
- Not a replacement for the proactive-notification path (`/ws/device`, "notify the human" — device/room-scoped, presence-gated). This channel is "invalidate a query", not "show a toast to a person".
- No document *content* on the wire (no titles/ids/filenames) — see §5 privacy.
- Not a general RPC/bidirectional protocol — server→client events only (+ client→server heartbeat/pong).

---

## 2. Why the naive design fails (review findings, condensed)

The proposed "new `/ws/user` with an in-memory `set[WebSocket]` registry, broadcast from `rag_service.py`" is **broken**:

- **T1 (cross-process, BLOCKER):** ingest completion (`services/rag_service.py:530`, `status=COMPLETED`) runs in the **`document-worker` pod** (`k8s/document-worker.yaml`; `workers/document_processor_worker.py` must not import the FastAPI app — enforced by `test_worker_module_isolation`). A broadcast there hits an **empty** registry → silent no-op. The only worker→browser mechanism today is **polling** (`services/progress.py` Redis keys read on a client request). **No Redis pub/sub exists** in the codebase (`grep '.pubsub()|.publish('` → 0 hits).
- **T2 (multi-replica, HIGH):** `k8s/backend.yaml` is `replicas: 1` today, but an in-memory registry breaks the moment it scales — each replica holds a disjoint socket subset. Even API-pod-local emits (Simba/Paperless) only reach the replica they ran on.
- **T3/T4:** auth-off (`ws_auth_enabled=false`) has no `user_id` to target; `owner=None` docs (null-KB/global-RAG) target nobody.
- **T7:** invalidating `keys.knowledge.all` on every completion during a folder-ingest backlog → refetch storm.

The corrected design below fixes all of these **by construction**.

---

## 3. Architecture

```
 worker pod ─┐
 reconciler ─┤ publish_user_event(target, event)      (any process)
 API route  ─┘            │
                          ▼
              Redis PUBLISH  renfield:events:user   {target_user_id | null, type, reason}
                          │  (fan-out to every subscriber = every API replica)
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   API pod #1        API pod #2        API pod #N
   subscriber task   subscriber task   subscriber task     (one per pod, started in main.py lifespan)
        │                 │                 │
   local registry    local registry    local registry      dict[user_id | ALL, set[WebSocket]]
        │                 │                 │
     /ws/user          /ws/user          /ws/user
     sockets           sockets           sockets
        │
        ▼
   browser  →  debounced queryClient.invalidateQueries({queryKey: keys.knowledge.list()})
```

**Invariant that makes it correct:** emitters **NEVER** touch the WS registry directly. Every event goes **through Redis**; the per-pod subscriber is the **only** fan-out path. This is uniform across processes (worker == API) and replica-safe by construction — no double-delivery, no missed replica.

### 3.1 Backend components

| Component | File (new unless noted) | Responsibility |
|---|---|---|
| `publish_user_event(redis, target_user_id, event: dict)` | `services/user_events.py` | Serialize + `PUBLISH` to `renfield:events:user`. Callable from ANY process; workers pass their existing `aioredis` client (mirrors `services/progress.py`). `target_user_id=None` ⇒ broadcast-to-all. |
| `UserEventRegistry` | `services/user_events.py` | Per-process `dict[int|_ALL, set[WebSocket]]` + `register/unregister/fan_out(target, event)`. `fan_out` no-ops when empty; drops broken sockets (never raises). Delivers to `target`'s set **plus** the `_ALL` set. |
| `user_events_subscriber(app)` | `api/lifecycle.py` | ONE background task per pod (lifespan), `SUBSCRIBE renfield:events:user`, decode, `registry.fan_out(...)`. Reconnects on Redis blip (backoff). |
| `/ws/user` endpoint | `api/websocket/user_events_handler.py` | Authenticate via `services/websocket_auth.authenticate_websocket` (cookie→JWT→ws-token). Register socket under `user_id` (or `_ALL` in auth-off). Heartbeat ping loop; drain on disconnect. Modeled on `kiosk_handler.py`'s connection lifecycle (send-timeout, prune-on-fail), NOT its in-memory single-registry assumption. |
| `resolve_document_owner(db, document)` | `services/user_events.py` (or reuse existing) | Map a `Document` → owner `user_id` via its `kb_document` atom (circles). `None` when unowned (null-KB/global-RAG/auth-off). |

### 3.2 Emit points (all publish, none broadcast directly)

| Seam | File:line | Event |
|---|---|---|
| Ingest completion | `services/rag_service.py` (status→`COMPLETED`) | `{type:"documents_changed", reason:"ingested"}` → owner |
| Paperless filing | `services/paperless_reconciler.py` (state→done/failed) | `reason:"paperless"` → owner |
| Simba upload | `services/simba_ingest_review.py::confirm` (→`UPLOADED`) | `reason:"simba"` → owner |
| Document delete/reindex | `services/rag_service.delete_document` / reindex enqueue | `reason:"deleted"`/`"reindex"` → owner (covers cross-tab/cross-device) |

The worker path (`rag_service`) publishes through the worker's `aioredis` client — the ONLY way its completion reaches browsers. Owner is resolved at the seam (§3.1).

### 3.3 Frontend components

| Component | File | Responsibility |
|---|---|---|
| `useUserEvents()` | `src/frontend/src/hooks/useUserEvents.ts` (new) | Open `/ws/user` via `/api/ws/token` + `getWebSocketUrl()` (existing pattern from `useKioskSocket`/`useDeviceConnection`). Heartbeat ping; **jittered** reconnect backoff tolerant of deploy-window 404s. Auth-gated: only connects when authenticated (and, in cookie/JWT modes, once a token is obtainable). On `documents_changed` → **debounced** (~1s) `queryClient.invalidateQueries({queryKey: keys.knowledge.list()})`. |
| Mount point | `src/frontend/src/App.tsx` | Mount `useUserEvents()` once, app-wide, **inside** the authenticated tree (not on the login/unauth surface). |

---

## 4. Key decisions (addressing every review finding)

1. **Redis pub/sub is mandatory (T1/T2).** Not optional, not "later". It is the spine. Publish-always / subscriber-only-fan-out eliminates both the worker-unreachability and the multi-replica gaps. New channel `renfield:events:user`; JSON payload `{target: int|null, type: str, reason?: str}`.
2. **Auth-off / single-user (T3):** `ws_auth_enabled=false` ⇒ `authenticate_websocket` returns no user ⇒ the socket registers under the `_ALL` group, and every event (regardless of `target`) is delivered to `_ALL`. One household, no circle boundary — safe.
3. **`owner=None` (T4):** publish with `target=None` ⇒ delivered to the `_ALL` group only. In auth-on multi-user, `_ALL` is **admins-only** (admin sockets also join `_ALL`); a content-free "refetch" to an admin leaks nothing (the refetch itself is circle-filtered server-side). Documented so it is a decision, not an accident.
4. **Coalescing / blast radius (T7):** (a) narrow the invalidation to `keys.knowledge.list()` (not `keys.knowledge.all`); (b) **client-side debounce** (~1s) so a folder-ingest backlog of N completions collapses to one refetch per tab; (c) the server subscriber additionally coalesces same-`(target,type)` events within a short window before fan-out (defense in depth). A batch of 200 completions ⇒ ~1 refetch, not 200.
5. **Content-free payload (privacy, §5).** No id/title/filename. The refetch goes through the already circle-filtered `/api/knowledge/documents`, so visibility is re-enforced server-side; even a mis-targeted event is a harmless extra refetch, never a title leak.
6. **Lifecycle (T6):** heartbeat ping < ingress idle timeout (~60s → ping every 25–30s); jittered exponential reconnect backoff; reconnect tolerant of transient WS 404s during deploys (`reference_ws_404_transient_during_deploy`); auth-gated mount; multi-tab safe (N sockets, each debounces independently).
7. **No shortcut layer.** `refetchOnWindowFocus` is **not** shipped as a substitute. (It may optionally be enabled later as orthogonal defense-in-depth, but it is not part of this feature and not a stand-in for push.)

---

## 5. Privacy / security

- **Payload is content-free** — `{type, reason}` only. No document identity crosses the wire.
- **Targeting is owner-scoped**; `_ALL` is single-user-household or admins-only. A content-free refetch signal to a user who can't see the doc reveals nothing, and the subsequent fetch is circle-filtered (`document_chunks_circles_filter` etc.).
- **Auth on the socket** reuses `authenticate_websocket` (the same per-user chain as chat `/ws`), including the `_ws_origin_allowed` CSWSH allowlist and the cookie-first/JWT/ws-token strategies.
- **No new secret / no token in URL** beyond the existing `/api/ws/token` faucet (short-lived `scope:ws`).

---

## 6. Test plan (target ≥80% coverage on new code)

**Backend (`tests/backend/`):**
- `services/user_events.py`
  - `publish_user_event` publishes the exact JSON to the channel (fakeredis / mock `aioredis`).
  - `UserEventRegistry`: register/unregister; `fan_out(target)` delivers to `target` set **and** `_ALL`; no-op when empty; a broken socket is pruned, others still receive; concurrency (two sockets same user).
  - `resolve_document_owner`: owned doc → owner id; null-KB/global-RAG → None; auth-off → None.
- `user_events_subscriber`: given a published message, calls `registry.fan_out` with the decoded target/event; reconnects after a simulated Redis drop; coalesces same-`(target,type)` within the window.
- `/ws/user` endpoint (via FastAPI `TestClient` websocket): auth-on accepts a valid token and registers under the user; auth-on rejects an unauthenticated socket; auth-off registers under `_ALL`; heartbeat ping/pong; disconnect unregisters.
- Emit-point tests: each seam (ingest-complete, paperless, simba-confirm, delete) calls `publish_user_event` with the correct `target` + `reason` (assert via a patched publisher). The worker path is tested at the `rag_service` seam (owner resolved, publish called) — proving the worker-origin event is emitted.

**Frontend (`tests/frontend/react/`):**
- `useUserEvents` (mock WebSocket): connects when authenticated, not when unauth; on `documents_changed` invalidates `keys.knowledge.list()` (spied QueryClient); **debounce** collapses N rapid events into ONE invalidation; reconnect with backoff after a socket close; heartbeat sent on interval; cleanup on unmount closes the socket.
- Extend `tests/frontend/react/api/invalidation.test.tsx`-style contract: a `documents_changed` event drives a KnowledgePage refetch end-to-end (MSW), flipping a row without a mount/refocus.

**Coverage gate:** new backend modules run under the existing `make test-coverage` (fail-under bumped/inspected for the new files ≥80%); new frontend hook covered by vitest. CI is non-functional → run on `.159` + local vitest (per project convention).

---

## 7. Rollout

- **Flag:** `USER_EVENTS_ENABLED` (backend) + a frontend feature flag via `/api/config/features` — **dark by default**; flag-off ⇒ no `/ws/user` mount, no subscriber, byte-identical to today. This keeps risk bounded and matches the project's opt-in-dark convention.
- **Order:** backend (endpoint + subscriber + publish helper + emit points, all dark) → migration none (no schema) → frontend hook (dark) → enable on one instance, browser-verify, then the other.
- **Deploy:** backend image (API + workers share it — the worker must carry `publish_user_event`), frontend image; both instances. PWA SW cache: unregister/hard-reload after the frontend deploy (known propagation step).
- **Redis:** reuses the existing Redis (same instance as streams/progress); one new channel, negligible load (content-free, coalesced).

## 8. Reusability (why this is not a one-off)

The channel carries a typed `{type, reason}` — `documents_changed` is the first consumer. The same substrate later powers live `obligations_changed` (Fristen agenda), `notes_changed`, `kg_changed`, etc., each a new `type` + a frontend `case` that invalidates its own query key. No new socket, no new pub/sub — just an added event type. This is the long-term-sustainable shape the owner asked for, and it retires the per-feature polling that several resources still use (`presence`, `meetings`, `skills`, `paperlessAudit`, `settings`) as a follow-up.

## 9. Open decisions (for sign-off)

1. **`_ALL` in auth-on multi-user = admins-only?** (recommended) vs. no `_ALL` fan-out at all (drop `owner=None` events). Recommendation: admins-only — content-free, harmless, and keeps admin dashboards live.
2. **Server-side coalescing window** (default 1s) + **client debounce** (default 1s) — both, or client-only? Recommendation: both (cheap defense in depth), server window configurable.
3. **Scope of v1 emit points:** documents only (ingest/paperless/simba/delete) — obligations/notes deferred to a follow-up that only adds event types. Recommendation: documents-only v1.
