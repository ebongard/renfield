# MCP Self-Detection + Self-Healing

Status: **Phase 1 SHIPPED (dark)** · Phases 2–3 designed, not built.
Flag: `MCP_HEALTH_MONITOR_ENABLED` (default `false`).

## Why

Renfield already *detected* most MCP failures — they just dead-ended where nobody
looked. The folder-ingest SMB-auth outage (2026-07) is the canonical case: the
filesystem MCP logged `SMBAuthenticationError`, retried, gave up, and fired an
`OPERATOR-NOTIFY` — all of it invisible until the user asked "warum läuft der
Import nicht?". The signal existed; the *surface* didn't. This design closes the
loop **detect → surface → alert → (Phase 2) heal** for every MCP, across both
planes of the two-plane MCP architecture.

## The two planes

* **Plane-A — MCP client fleet.** The backend is the MCP client (`MCPManager`,
  `services/mcp_client.py`). Health is observable synchronously via
  `get_status()` → a folded `{healthy, degraded, down}` verdict per server.
* **Plane-B — ingest push MCPs.** `renfield-mcp-filesystem` and
  `renfield-mcp-email-ingest` are NOT clients of the backend's MCP manager — they
  PUSH into the backend over REST. `get_status()` can't see them. They detect
  their own failures and (already) fire a `WebhookNotifier` `OPERATOR-NOTIFY`.

Phase 1 unifies both onto ONE user-facing action: a privacy-aware proactive
notification to the owner admin, deduped so an ongoing problem doesn't spam.

## Phase 1 — detect + surface + alert (SHIPPED, dark)

`services/mcp_health_monitor.py`:

* **Plane-A** — `monitor_tick(app)` runs on a boot-scheduled periodic task
  (`_schedule_mcp_health_monitor`, `MCP_HEALTH_MONITOR_INTERVAL`, default 120s).
  Polls `get_status()`, alerts on a NEW `degraded`/`down`, clears the per-server
  ledger key on recovery so a later re-failure re-alerts promptly.
* **Plane-B** — `ingest_report(payload)`, fed by `POST /api/mcp-health/report`
  (`api/routes/mcp_health.py`, Bearer-auth via the existing folder-ingest token
  the MCPs already hold). Records the latest failure per source + alerts.
* **Dedup** — `_alerted` ledger keyed per issue with a re-alert TTL
  (`MCP_HEALTH_REALERT_SECONDS`, default 6h). Ongoing problem → one alert;
  recurrence or a new problem → a fresh alert.
* **Surface** — `internal.system_health` folds fresh Plane-B reports into its
  read-only "was ist kaputt?" answer (they never reach `get_status()`).
* **Alert target** — the owner admin (`auth_service.active_admin_ids`, lowest id),
  first-user fallback; `None` on an auth-off single-user install (correct — the
  notification isn't per-user-scoped there).

**Detect-and-notify ONLY.** No mutation of any MCP — healing is Phase 2. This
mirrors the existing `system_health` (read) / `credential_reconciler` (heal) split.

Flag off → byte-identical: a Plane-B report is still *recorded* (so
`system_health` can surface it) but no proactive alert fires, and `monitor_tick`
no-ops.

## Failure-mode catalog (the "what else can break" sweep)

Per-MCP failure modes and where each is (or isn't yet) caught:

| MCP / plane | Failure mode | Detected today | Surfaced (P1) | Heal (P2/P3) |
|---|---|---|---|---|
| filesystem (B) | SMB-auth / share down / retry-exhausted | yes (OPERATOR-NOTIFY) | ✅ report → alert | re-reconcile on recovery |
| email-ingest (B) | IMAP drop / BYE-timeout / bad token | yes (OPERATOR-NOTIFY) | ✅ report → alert | backend-recovery re-reconcile (asymmetry: filesystem re-reconciles, email doesn't yet) |
| any client (A) | transport disconnect | yes (`down`) | ✅ tick → alert | active-probe reconnect (`probe_server()`, currently dead code) |
| any client (A) | plugin bind failed / 0 tools | yes (`degraded`) | ✅ tick → alert | — (config problem, human-gated) |
| any client (A) | **connected but upstream resource dead** | **NO** — `get_status()` is connectivity-only | — | Phase 2 functional health |
| paperless / news / carrier (A) | 429 / Retry-After throttle | NO — treated as generic error | — | Phase 3 backoff + honor Retry-After |
| dedicated MCP pods | pod crash-loop / not-ready | k8s only, not in renfield's model | — | Phase 3 liveness/readiness probes |

## Phase 2 — functional health + self-heal (designed)

1. **Functional health in `_server_health`** — extend the fold beyond
   connectivity: a server with a high recent-call-failure rate → `degraded` even
   while its transport is up ("connected but upstream resource dead", the one gap
   Plane-A can't see today).
2. **Active-probe + reconnect** — wire `probe_server()` (currently dead code) into
   the monitor: on a `down` verdict, probe and attempt a bounded reconnect before
   alerting, so a transient blip self-heals silently.
3. **Email-ingest backend-recovery re-reconcile** — close the asymmetry:
   filesystem re-reconciles its push backlog when the backend comes back;
   email-ingest should too.

## Phase 3 — rate-limit + orchestration (designed)

1. **429 / Retry-After** — paperless, news, and carrier-tracking calls honor
   `Retry-After` with backoff instead of surfacing a throttle as a hard error.
2. **k8s probes** — liveness/readiness on the dedicated MCP pods so a crash-looping
   pod is restarted by k8s and its state reflected in the kiosk verdict.
3. **Kiosk verdict extension** — a real health color for the Plane-B ingest MCPs
   on the kiosk (today they're telemetry-excluded).

## Rollout

Dark everywhere. To enable on an instance: set `MCP_HEALTH_MONITOR_ENABLED=true`
(needs `PROACTIVE_ENABLED=true` for delivery) and point each ingest MCP's
`*_NOTIFY_WEBHOOK_URL`/`_TOKEN` at `POST /api/mcp-health/report`
(see `docs/ENVIRONMENT_VARIABLES.md`).
