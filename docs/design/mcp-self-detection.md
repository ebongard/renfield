# MCP Self-Detection + Self-Healing

Status: **Phase 1 + 2 SHIPPED** · Phase 3 designed, not built.
Flags: `MCP_HEALTH_MONITOR_ENABLED` (default `false`) gates the whole monitor;
`MCP_HEALTH_SELF_HEAL_ENABLED` (default `true`) gates the Phase-2 probe+reconnect.

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
| any client (A) | transport disconnect | yes (`down`) | ✅ tick → alert | ✅ P2 active-probe reconnect (`probe_server()`) |
| any client (A) | plugin bind failed / 0 tools | yes (`degraded`) | ✅ tick → alert | — (config problem, human-gated; probe can't fix → still alerts) |
| any client (A) | **connected but calls time out** | ✅ P2 `calls_failing` (rolling timeout window; app errors excluded) | ✅ tick → alert | ✅ P2 probe reconnects a wedged session |
| paperless / news / carrier (A) | 429 / Retry-After throttle | NO — treated as generic error | — | Phase 3 backoff + honor Retry-After |
| dedicated MCP pods | pod crash-loop / not-ready | k8s only, not in renfield's model | — | Phase 3 liveness/readiness probes |

## Phase 2 — functional health + self-heal (SHIPPED, dark-safe)

All three items shipped; the self-heal is gated `mcp_health_self_heal_enabled`
(default on when the monitor is on) — flag off → Phase-1 detect-only behavior.

1. **Functional health in `_server_health`** (`mcp_client.py`) — `MCPServerState`
   keeps a rolling window (`recent_outcomes`, `mcp_health_call_window`) of the last N
   **health-correlated** tool-call outcomes: `True` on a clean result, `False` on a
   **timeout** (the server didn't respond). Deliberately **NOT** recorded — an
   `isError`/inner-error result (an APPLICATION outcome: a device off, a parcel not
   found, a workflow returning `success:false` — says nothing about the server's
   health), caller rejects (permission / validation / rate-limit), and session-death
   (which already flips `connected=False` → `down`). This is the key correctness
   guard: folding plain app errors would falsely flag a healthy server whose *target*
   failed (caught in `/review`). When a CONNECTED server's recent timeout share ≥
   `mcp_health_call_fail_ratio` over ≥ `mcp_health_call_min_samples` calls →
   `_server_health` folds `degraded` / `calls_failing`. The window is cleared on
   (re)connect so a reconnect that fixed the server isn't left falsely flagged.
2. **Active-probe + reconnect** (`mcp_health_monitor.py`) — `monitor_tick` now runs a
   self-heal pass FIRST: for each degraded/down server it calls `probe_server()`
   (active `tools/list` + single-shot reconnect), then re-reads `get_status()` and
   alerts only on servers STILL broken (message says "Selbstheilung versucht, ohne
   Erfolg"). A `down` server the reconnect fixes is silently healed (no alert, ledger
   cleared); a `plugin_failed`/upstream-dead server a reconnect can't fix stays
   degraded and alerts. Capped `mcp_health_self_heal_max_per_tick` per tick.
   **Hang-guards (2026-08-22, #1107):** the reconnect a probe drives could wedge
   indefinitely on a pathological upstream (observed: the run_at_boot tick hung in
   the twin reconnect while its service had no endpoints, silencing the WHOLE
   monitor loop for the outage's duration — no probe, no alert). Now bounded at
   three layers, all same-task `asyncio.timeout` (anyio-cancel-scope-safe, never
   `wait_for`): each self-heal probe (`mcp_health_self_heal_probe_timeout`, 45s),
   the transport `__aenter__` in `_connect_server` (`mcp_connect_timeout` — init/
   tools-list were always bounded, the transport connect was not), and every
   exit-stack teardown (`_TEARDOWN_TIMEOUT_S`, runs under `reconnect_lock`). A
   cancelled connect hands its partially-entered stack to a detached bounded
   closer (a rare leak beats a frozen loop); the failed-connect path now closes
   the LOCAL exit stack (previously leaked — only the always-None
   `state.exit_stack` was closed). Observability: every COMPLETED tick increments
   `renfield_mcp_health_ticks_total` + sets `renfield_mcp_health_problem_servers`
   (a flatlining counter under a running backend = the monitor is stuck — exactly
   the ambiguity that cost the diagnosis) plus a DEBUG tick-complete line.
3. **Email-ingest backend-recovery re-reconcile** (`renfield-mcp-email-ingest`) —
   closes the asymmetry: `RenfieldPusher.health()` + a daemon `_health_poll_loop`
   (`EMAIL_HEALTH_POLL_SECONDS`, default 30s) re-reconcile every mailbox on a
   backend down→up edge, and `MessageEngine.recover()` **un-parks `_exhausted`** mail
   (that exhausted its retries during the outage, still UNSEEN) and re-dispatches it —
   no manual restart, mirroring the filesystem MCP.

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
