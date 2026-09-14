# MCP Self-Detection + Self-Healing

Status: **Phase 1 + 2 + 3 + 4 SHIPPED** (Phase 3 item 3, the Plane-B kiosk verdict,
is still open).
Flags: `MCP_HEALTH_MONITOR_ENABLED` (default `false`) gates the whole monitor;
`MCP_HEALTH_SELF_HEAL_ENABLED` (default `true`) gates the Phase-2 probe+reconnect;
`MCP_HEALTH_RATE_LIMIT_SIGNAL_ENABLED` + `MCP_RATE_LIMIT_BACKOFF_ENABLED` (both
default `false`) gate the Phase-3 throttle handling.

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
| any client (A) | plugin bind failed / 0 tools | yes (`degraded`) | ✅ tick → alert — P3 fixed three alert-path losses (§3.0) + a 0-tools boot grace | — (config problem, human-gated; not probed since P3) |
| any client (A) | **connected but calls time out** | ✅ P2 `calls_failing` (rolling timeout window; app errors excluded) | ✅ tick → alert | ✅ P2 probe reconnects a wedged session |
| any client (A) | **upstream answers everything with an error** (HTTP 500) | ✅ P4 functional probe | ✅ tick → alert naming the probe reason | — (upstream problem; human-gated) |
| any client (A) | **nobody calls it, so there are no samples** | ✅ P4 functional probe | ✅ tick → alert | — |
| search (A) | reachable SearXNG, all scrapers CAPTCHA-blocked | ✅ purpose-built probe (#1162) | ✅ P4 wires its verdict into `_server_health` | — |
| paperless / news / carrier (A) | 429 / Retry-After throttle (upstream, in an error result) | ✅ P3 `rate_limited` (dark, windowed) | ✅ tick → alert naming the count | ✅ P3 per-tool Retry-After fail-fast (dark); no transparent retry (§3.2) |
| any client (A) | 429 from the MCP endpoint itself | as timeout / `down` (SDK raises it in a background task) | ✅ via `calls_failing` / `down` | ✅ existing reconnect |
| backend pod | DB unreachable / process wedged | ✅ P3 readiness `/health/ready`, liveness `/health/live` | k8s endpoints + peer watchdog | k8s (liveness never on a dependency) |
| dedicated MCP pods | pod crash-loop / not-ready | `tcpSocket` probes (dlna, samsung) | ✅ via Plane-A `down` | k8s restart |

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

## Phase 3 — the 0-tools gap, rate-limit, probes (SHIPPED, 2026-09-14)

### 3.0 Why a tool-less server could go unreported

The catalog above said "0 tools → ✅ tick → alert", and the single happy path does
alert. Every monitor test faked `get_status()` with dicts, so nobody had run a REAL
`MCPManager` with a 0-tool server through the real self-heal and alert path. Doing
that (reproduction on .159, 2026-09-14) found three defects in the ALERT path, not in
the verdict:

1. **A failed hand-off was never retried.** `ops_alert.should_alert` stamps the
   ledger *before* delivery, and `_notify` threw away `notify_admin`'s bool. An
   attempt while the pipeline was failing, or while `PROACTIVE_ENABLED` was still
   off, silenced the problem for the whole re-alert TTL (6 h), and again at every
   TTL boundary that hit a bad moment. Measured: tick 1 undelivered, tick 2 silent.
   The scheduled-task alerts already honoured the bool; the monitor did not.
   Fix (revised in review): "told" now means **a notification row exists** —
   `notify_admin` returns `True` for a failure AFTER `process_webhook` committed the
   row (the admin sees it in their list), checked on a fresh session. Only a missing
   row is retried, and only after `MCP_HEALTH_ALERT_RETRY_SECONDS` (600 s,
   `ops_alert.defer_alert`). The first cut cleared the key on `False`, which during a
   post-persist delivery failure stored a new row + push on every 120 s tick
   (~720/day per server — the 60 s pipeline dedup window is shorter than the tick).
2. **Reason changes (decided in review).** The first cut put the reason into the
   ledger key and cleared every key that was not a current problem, so a server
   flapping `rate_limited` ↔ `calls_failing` re-alerted on every switch and
   bypassed the 6 h limit. Decision: the re-alert TTL applies per **server + health**
   (`planea:{name}:{health}`); a reason change inside the TTL is the same outage and
   does not re-alert — the next due alert names the CURRENT reason, because the
   message is built at alert time. The recovery sweep forgets a server's keys only
   when that server has no problem at all. The MCP monitor sends no recovery notice;
   a real recovery only re-arms the ledger, so a re-failure alerts at once.
3. **The self-heal "recovered" a tool-less server.** `tools/list` answers fine with
   an empty list, so `probe_server` returned ok, the tick logged "1 recovered on
   reconnect", and the alert claimed "Selbstheilung versucht". A reconnect cannot
   create tools. Fix: `no_tools` (like `plugin_failed`, and now `rate_limited`) is in
   `_UNHEALABLE_CODES` and is not probed.

Plus two things the alert needed to be trustworthy rather than noisy:

- **No boot storm:** `MCPServerState.no_tools_since` starts when a discovery finds
  nothing and clears when tools appear; a reconnect that still finds nothing does
  NOT reset it (a flapping server must still age into an alert). `get_status()` adds
  `no_tools_for_seconds`; the alert waits `MCP_HEALTH_NO_TOOLS_GRACE_SECONDS` (300 s,
  above two refresh intervals) while the verdict shows at once. Unknown age alerts.
- **A reason a human can act on** — "stellt keine Werkzeuge bereit" instead of the
  raw `no_tools` code in the message.

What was NOT the cause: the verdict itself (`_server_health` flags a connected
server with an empty `all_discovered_tools` correctly), federation exemption, and the
`prompt_tools` filter (a server whose tools are all filtered out stays healthy, pinned
by a test). The optional external MCP server that once served 0 tools has no stanza
in either instance's live `mcp_servers.yaml` today (checked 2026-09-14, boolean
only), so this is a fix of the path, not of a currently-firing condition. Related
gap left open: a `PLUGIN_MCP_BINDINGS` entry naming a server that is not configured
can never surface `plugin_failed`, because `get_status()` only iterates configured
servers.

### 3.1 Upstream rate-limit as its own signal (dark)

A throttle is neither a dead server nor a failed functional check, so it gets its own
state and must not leak into the other two:

- **Where it is read:** only from results that are already errors (`isError` or the
  inner-error envelope) and from app-level exceptions (incl. an `httpx` response with
  status 429 + `Retry-After` header) — never from a success, whose payload may talk
  about rate limits. `_classify_rate_limit` recognises httpx's
  `Client error '429 Too Many Requests'`, JSON `status`/`code` 429, and "rate limit"
  prose; a bare "429" (an invoice or document number) does NOT count.
- **What it cannot see:** a transport-level 429 from the MCP endpoint itself. The
  SDK (mcp 1.27.1) raises it inside a task-group task, so it surfaces as a timeout or
  a dead session — already covered by `calls_failing` and `down`.
- **State:** `rate_limit_events` (timestamps) on `MCPServerState`, separate from
  `recent_outcomes` and from the probe verdict. **Windowed hysteresis:**
  `>= MCP_HEALTH_RATE_LIMIT_MIN_EVENTS` (5) within
  `MCP_HEALTH_RATE_LIMIT_WINDOW_SECONDS` (900) → `degraded/rate_limited`, folded after
  `probe_failed` (a dead service is worse news) and before `calls_failing` (direct
  evidence beats inference). Events age out, so a burst cannot pin a server red.
- **Probes:** with either Phase-3 flag on, a throttled probe records NO verdict (two
  of them would otherwise read `probe_failed` for a server that is merely busy), but
  its cadence advances — re-probing a throttled upstream every tick would deepen it.
  With the backoff gate on this is mandatory, not a preference: the probe's
  "failure" may be our OWN Retry-After refusal (caught in self-review). Both flags
  off → a throttled probe fails exactly as before.
- **Flag dark** (`MCP_HEALTH_RATE_LIMIT_SIGNAL_ENABLED=false`): our own batch jobs
  throttle themselves by design (the Paperless dedupe against a 60/min MCP), so a
  throttle becoming a kiosk colour and an alert is an operator decision. Flag off →
  nothing is recorded, byte-identical.
- The alert text names the count, never the upstream error text: throttle messages
  carry request URLs, and API URLs can carry keys.

### 3.2 Retry-After (dark) — a deliberate deviation from "backoff"

The roadmap said "honor Retry-After with backoff instead of surfacing a throttle as a
hard error". Built: with `MCP_RATE_LIMIT_BACKOFF_ENABLED`, a Retry-After the upstream
sent for a TOOL makes further calls to that tool return at once
("Upstream-Rate-Limit … erneut versuchen in N s") until it passes, capped by
`MCP_RATE_LIMIT_MAX_BACKOFF_SECONDS` (300). **Not built: a transparent
wait-and-retry.** A 429 inside a tool can follow side effects of that same call
(one tool, several upstream requests), and re-running a mutating tool is exactly the
double execution `_is_session_dead` is kept narrow to avoid. The gate is **per tool**,
not per server — one server can front several upstreams (tracking: one API per
carrier). Our own refusal is not recorded as a new throttle event, or the gate would
keep the server red by itself; a clean result lifts the horizon. Callers that already
back off (the dedupe tool) keep working — they just hit a fast refusal instead of
the upstream.

### 3.3 k8s probes

- **Backend readiness → `/health/ready`**, never `/health` (which answers "ok" with a
  dead DB). **Decided in review: readiness reflects DB REACHABILITY, not pool
  saturation.** The first cut ran `SELECT 1` through the app pool; an exhausted pool
  (the 2026-07-01 watch-folder backlog) would have made every replica's check wait,
  time out, and take ALL replicas out of the Service at once while the DB was healthy
  — slow turned into 503. The check now uses its own short-lived connection
  (`services/health_check.py`: a lazily created `NullPool` engine, asyncpg connect
  timeout + `command_timeout` + server `statement_timeout`, disposed at shutdown,
  `application_name=renfield-readiness`), bounded by
  `HEALTH_READY_DB_TIMEOUT_SECONDS` (3 s, below the probe's 5 s). Cost: one short DB
  connection per probe per replica. A DB blip under 30 s (period × threshold, e.g. a
  CNPG switchover) does not flip it.
- **Optional checks are bounded and never fail readiness** (review finding): the
  Redis ping (client with socket connect/read timeouts) and the device-summary hook
  each run under `HEALTH_READY_AUX_TIMEOUT_SECONDS` (1 s) and report `degraded` /
  `unknown` on a hang. Before, a Redis accepting TCP but never answering held the
  probe past its timeout and failed readiness on every replica simultaneously. All
  checks run concurrently, so the probe's worst case is the DB bound.
- **Backend liveness → `/health/live`**, deliberately dependency-free. A liveness
  probe that checked the DB would restart every replica during a DB outage — a
  restart storm that tears down MCP sessions and satellite connections and fixes
  nothing (the 2026-09-11 outage lasted 21.5 h). Liveness only asks whether the
  process is wedged.
- **Deploy consequence:** a rollout while the DB is down no longer "completes"; new
  pods stay NotReady while the old ones serve. Intended (deploy skill updated). The
  peer watchdog still sees the outage — no endpoints is as unreachable as a 503.
- **Dedicated MCP pods** (`k8s/dlna-mcp.yaml`, `k8s/samsung-mcp.yaml`): already had
  `tcpSocket` readiness + liveness; unchanged. Neither server exposes an HTTP health
  route (checked in both repos), and an `httpGet` on the MCP endpoint answers 4xx
  without MCP headers, so TCP is the honest maximum today. They run `hostNetwork` and
  the backend dials them directly, so readiness gates no traffic; "reflected in the
  kiosk verdict" already happens through Plane-A (`down`). The filesystem,
  email-ingest and xidra manifests live in their own repos / `x-ren`.

### 3.4 Still open

**Kiosk verdict for the Plane-B ingest MCPs** (a real health colour; they are
telemetry-excluded today) — not built.

### 3.5 Follow-ups closed (2026-09-14)

- **Streaming calls now count.** `execute_tool_streaming` had its own copy of the
  call handling and recorded neither timeouts nor throttles. Worse than the ticket
  said: it also shielded nothing from the refresh/self-heal reconnect
  (`inflight_deadlines`), ignored the Retry-After gate, marked the server
  *disconnected* on any app exception (a relayed 429 would read `down` and draw
  self-heal reconnects), and ran a `per_user_auth` server on the shared operator
  session. Fix: ONE accounting for both paths — `MCPServerState.track_call`
  (in-flight shield), `_retry_after_refusal`, `_call_timed_out` (fail sample),
  `_call_app_error` (throttle check, no disconnect), `_call_result` (ok sample,
  lifts the horizon, throttle check on error results). Streaming specifics: a
  timeout after partial progress is a fail sample; the caller's cancellation goes
  through none of the helpers (the server did nothing wrong); a dead session still
  disconnects but is not retried, because progress may already have reached the
  consumer. `per_user_auth` servers are routed to `execute_tool` (per-user session
  or fail-closed denial, no progress). Latent today: no configured server sets
  `streaming: true` and nothing in the backend calls `execute_tool_streaming`.
- **`refresh_tools` kept the configured tool shape.** Connect and refresh built the
  tool list separately; refresh never appended `tool_hints`, so every hint was gone
  after the first refresh (default 300 s). Both now go through
  `_install_discovered_tools` (hint → `all_discovered_tools` → no-tools clock →
  filter/index via `_apply_tool_filter`). That also fixed a second drift: a reconnect
  never removed a tool the server stopped offering from `_tool_index`. Checked and
  NOT lost on refresh: `prompt_tools` / DB override filter, `tool_permissions`,
  `call_timeout`, `health_probe` — all read from `state.config` at use time
  (pinned by tests against a real YAML-loaded `MCPManager`). Neither instance's
  live `mcp_servers.yaml` sets `tool_hints` today, so no hint was lost in prod.

## Rollout

Dark everywhere. To enable on an instance: set `MCP_HEALTH_MONITOR_ENABLED=true`
(needs `PROACTIVE_ENABLED=true` for delivery) and point each ingest MCP's
`*_NOTIFY_WEBHOOK_URL`/`_TOKEN` at `POST /api/mcp-health/report`
(see `docs/ENVIRONMENT_VARIABLES.md`).
