# MCP Self-Detection + Self-Healing

Status: **Phase 1 + 2 + 4 SHIPPED** · Phase 3 designed, not built.
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
| any client (A) | **upstream answers everything with an error** (HTTP 500) | ✅ P4 functional probe | ✅ tick → alert naming the probe reason | — (upstream problem; human-gated) |
| any client (A) | **nobody calls it, so there are no samples** | ✅ P4 functional probe | ✅ tick → alert | — |
| search (A) | reachable SearXNG, all scrapers CAPTCHA-blocked | ✅ purpose-built probe (#1162) | ✅ P4 wires its verdict into `_server_health` | — |
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

## Phase 4 — functional probes (SHIPPED, 2026-09-13)

**The reason the monitor was silent**, and it is more specific than "it only
checks connectivity". Phase 2 already had a functional signal — but
`record_call_outcome` counts **only timeouts**, because an app-level error says
nothing about the *server's* health (a device is off, a parcel is not found). That
decision is correct and must not be reversed. It left two blind spots:

1. An upstream that answers **every** call with HTTP 500 looks exactly like a run
   of app errors. Paperless was dead for 3 d 10 h with 13/13 servers green.
2. A server **nobody calls** produces no samples at all. n8n was unreachable from
   the cluster and still read "connected".

A probe escapes the bind: **we** choose a cheap read-only call that *must*
succeed, so its failure IS a health signal — without reclassifying anybody else's
app errors.

**Shape.** An optional per-server `health_probe` stanza in `mcp_servers.yaml`
(`tool`, `args`, `interval`, `timeout`, `expect.{min_items,path}`), parsed by
`_parse_health_probe` the way `notifications` already is. The verdict lives on
`MCPServerState` (`probe_consecutive_failures`, `last_probe_*`) — deliberately
**separate** from `recent_outcomes`, so the two signals cannot contaminate each
other — and folds into `_server_health` as `degraded`/`probe_failed`, **ahead of**
`calls_failing` (direct evidence beats an inference drawn from whatever the agent
happened to call).

**Ordering in the tick.** `get_status` → self-heal → re-read → **probe** →
re-read → the existing alert pass. Probing after the heal means the probe judges
the *service* on a freshly reconnected transport instead of re-reporting a
transport fault the heal already fixed; re-reading after means the verdicts reach
the same alert path as everything else. No second alert channel.

**Two decisions that are easy to get backwards:**

- **A reconnect does NOT clear a probe verdict**, even though it clears
  `recent_outcomes`. A reconnect proves the transport works and nothing about the
  service behind it — Paperless served HTTP 500 across many healthy reconnects.
  Only a successful probe clears a probe verdict.
- **Hysteresis before belief** (`mcp_health_probe_fail_threshold`, 2). A single
  miss is an upstream hiccup, a rate-limit, a restart window. Alarming on it would
  make the cure noisier than the silence.

**Bespoke probes.** Some servers cannot be judged by a generic tool call.
`search` is the worked example: a bare result count is a *documented* false-green
because Wikipedia answers almost anything, so `services/search_health.py` (#1162)
counts distinct contributing general engines instead. That probe already existed
and was better than a generic one would be — but its verdict dead-ended in
`internal.system_health`, i.e. it was only ever seen if a human thought to ask.
`_BESPOKE_PROBES` in the monitor wires it into the same channel via
`record_external_probe`. A `search` stanza in the YAML would be a regression, and
the config header says so. An `unknown` verdict records nothing: absence of
evidence must not read as evidence of failure.

**Six things `/review` caught that had already reached a commit**, each of which
would have made green mean less rather than more:

1. `expect.min_items`/`path` were evaluated against `execute_tool`'s `data`, which
   is the list of raw MCP **content parts**, never the payload — so the exact
   example in the YAML header would have fired a *critical* alert about a healthy
   server. The payload lives in the joined `message` text; that is now what is
   parsed.
2. The probe inherited `execute_tool`'s deliberate **fuzzy tool-name fallback**. A
   renamed or mistyped tool would have silently called a *different* one: false
   green if the substitute answers, false red if it fails validation. The probe now
   resolves exactly and reports a missing tool as misconfiguration.
3. A malformed stanza (`interval: 10m`) raised, and the exception escaped into the
   per-entry config `try/except` — which drops the **whole server**. Paperless
   would have vanished from the fleet. The coercions now fall back individually.
4. A probe that tripped the hang-guard recorded **no verdict**, so a permanently
   wedged server never reached the failure threshold and never alerted — while
   being re-probed every tick, each attempt costing the full guard. The one failure
   mode where the probe is most needed was the one it stayed silent on.
5. `(ok=True, detail=None)` was overloaded as "unknown", colliding with a healthy
   bespoke verdict that carried no reason string. Now an explicit tri-state
   (`None`/`True`/`False`).
6. A stable due-order plus a per-tick cap is a **blacklist, not a throttle** — the
   same prefix would win every tick and later servers (bespoke ones especially,
   appended last) would starve. The due list is now sorted longest-unprobed-first.

**Deliberate skips**, each for a reason: `per_user_auth` servers (a `user_id=None`
call is denied fail-closed, so a probe would report a permanent false failure),
federation transport, disconnected servers, and every server with no stanza.

**The flag is ON; the throttle is the YAML.** A server without a stanza is never
probed — a probe that itself generates load is worse than none. Probes run as
system calls (`user_permissions=None`, `user_id=None`), which also keeps them out
of the per-user `ToolOutcomeStat` telemetry the kiosk reads: a probe must not
colour the tool-health numbers it exists to make honest.

## The alert path moved out (2026-09-12)

`_notify`, `_should_alert`/`_clear_alert` and the admin resolution now live in
**`services/ops_alert.py`**, because three subsystems need exactly the same three
things (who to tell, how to tell them, how often) and none of them should grow its
own version: this monitor, the scheduled-task failure alerts, and the HTTP
watchdog. The names here remain as thin delegates, so the monitor's behaviour and
its test seams are unchanged.

The ledger stays **in-process on purpose**: it is a rate limiter, not a record. A
pod restart re-arming an alert for a problem that is still broken is the safe
direction to fail. A caller that needs restart-durable "already told them" state
keeps its own column — see `ScheduledTask.error_alerted_at`.

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
