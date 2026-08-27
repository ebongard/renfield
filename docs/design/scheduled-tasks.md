# Scheduled Tasks — DB-defined, UI-managed recurring jobs

**Status:** **All three phases implemented.** Phase 1 (#1138) — engine + model +
migration + registry + built-ins, plus the MCP `dedupe_documents` tool (sibling
repo v1.12.0). Phase 2 (#1139) — the ADMIN-gated `/api/scheduled-tasks` CRUD +
the "Geplante Aufgaben" admin page (gated on `scheduled_tasks_enabled`); DEPLOYED
+ browser-verified both instances. Phase 3 — **16 of the ~23 `_schedule_*`
schedulers migrated onto the engine** (21 built-in seed rows total); the 7
keep-legacy jobs stay on `_spawn_periodic_task` (see below). Reviewed (three
design rounds + implementation code reviews per phase); all findings folded in.
Dark/inert by default — each migrated built-in re-asserts its runtime gate
in-handler (H4), so a flag-off job is a seeded-enabled no-op; the admin UI is
absent until `scheduled_tasks_enabled` is on.

**Phase 3 kept-legacy (NOT migrated, by design):** `whisper_preload` (one-shot),
`notification_poller` (persistent connection), `reminder_checker` (15s, sub-tick),
and the three kiosk WS-push refreshers (`kiosk_internal_health` 30s sub-tick,
`kiosk_weather` 600s, `kiosk_peer_status` 60s — WS-delta loops coupled to the
in-process `_kiosk_clients` hub, no operator value in the UI).

**Phase-1 follow-up still open:** rewire the interactive `internal.paperless_dedupe`
as a thin caller of `mcp.paperless.dedupe_documents` (Review D9) — the autonomous
scheduled job already calls the MCP tool, so the new code has no fork; the
interactive rewire is deploy-coupled to bumping the `renfield-mcp-paperless`
requirements pin to the v1.12.0 commit (they ship in the same backend image).

## Problem

Renfield has ~25 hand-coded background schedulers in
`api/lifecycle.py::_schedule_*`, all built on the same `_spawn_periodic_task`
primitive: a naive **fixed-interval-from-boot** loop
(`while True: await sleep(interval); await work()`), gated by scattered
`settings.*_enabled` flags. Consequences:

1. **Invisible at runtime** — no way to see what is scheduled, when it last ran,
   whether it succeeded, or when it runs next.
2. **Un-editable at runtime** — changing an interval, disabling a job, or
   bounding it to a date window means a code change + redeploy.
3. **No cron, no date bounds** — every job is a bare interval; "every Monday
   08:00" or "run daily until 2026-12-31" is inexpressible.
4. **Boot-drift (#678)** — a daily job on a pod that recycles faster than its
   interval can starve: the loop sleeps the full interval before its first tick,
   so a pod that restarts every few hours may never reach a 24h job's body.

The immediate forcing function: draining xidra's ~3000 Paperless duplicates
requires the owner to type "räum die Duplikate auf" ~60 times (the MCP is
rate-limited 60/min, so each pass deletes a batch and reports the remainder).
The user asked for this to run **autonomously on a schedule** — which is the
first concrete consumer of a general scheduled-tasks platform.

## Requirements (user-fixed)

1. A **Scheduled Tasks** platform: DB-defined recurring jobs with **interval OR
   cron**, **start/end dates**, and an **enable toggle**.
2. A **management UI** showing each task, its repeatability, start/end, next/last
   run, and status.
3. **All ~25 existing jobs migrated** onto the new format.
4. An autonomous **Paperless dedupe** job as the first built-in — and the
   Paperless-specific logic lives in the **Paperless MCP** (`dedupe_documents`
   tool), not tied to the Renfield core, so an instance without a Paperless
   integration carries no dead Paperless code. The scheduled job's handler is a
   thin caller of the MCP tool.

## Naming (collision-avoidance)

The core already uses `Task` / `tasks` / `/api/tasks` / `/tasks` (the agent
task-queue) and `Reminder` / `reminders` (chat reminders). To avoid every
collision:

| Concern | Name |
|---|---|
| Model | `ScheduledTask` |
| Table | `scheduled_tasks` |
| REST | `/api/scheduled-tasks` |
| Route | `/admin/scheduled-tasks` |
| Nav key | `nav.scheduledTasks` → "Geplante Aufgaben" |

## Architecture

A DB table of task **definitions** (admin-editable) + a `handler_key`→code
**registry** (1:1, mirroring `services/domain_contract.py`) + a single **engine
loop** that each tick selects due tasks and **spawns each as its own
`asyncio.Task`**.

The alternative considered was a per-row `asyncio` loop (one long-lived task per
DB row, re-spawned on edit). Rejected in favor of the single engine: it is
simpler for UI edits (edit a row → the next tick honors it, no task lifecycle to
reconcile) and expresses cron + date-bounds naturally. This is sound **only**
with the spawn-not-await and boot-force fixes below (see Review findings).

### Data model

`scheduled_tasks`:

| column | purpose |
|---|---|
| `id` | PK |
| `name` | UNIQUE — the seed/idempotency key |
| `handler_key` | registry lookup → the code that runs |
| `schedule_kind` | `interval` \| `cron` |
| `interval_seconds` | for `schedule_kind=interval` |
| `cron_expr` | for `schedule_kind=cron` (evaluated by `croniter` against a **tz-aware** `now()` — see below) |
| `params` | JSONB — handler arguments (e.g. dedupe `max_delete`) |
| `enabled` | admin toggle |
| `run_at_boot` | force a run at startup regardless of `next_run_at` |
| `start_at` / `end_at` | optional active window |
| `next_run_at` | engine-computed |
| `last_run_at` / `last_status` / `last_error` / `last_duration_ms` | run history (last only) |
| `is_builtin` | built-ins are edit-not-delete; custom tasks are deletable |
| `created_at` / `updated_at` | audit |

### Engine tick

`run_engine_tick(app)` runs on ONE `_spawn_periodic_task` with
`engine_tick=10s` (`scheduled_tasks_engine_tick_seconds`). Each tick:

1. Select `enabled` tasks with `next_run_at <= now()` and within
   `[start_at, end_at]`.
2. For each, `asyncio.create_task(_run_one(app, task))` under a **`Semaphore(3)`**.
   **Spawn, not inline `await`** — a slow or looping handler must never stall the
   other due tasks (Review C1). The bound is deliberately low: see the
   connection-budget note below.

`_run_one(app, task)`:

1. Acquire the per-task advisory lock (**skip if held** — no cross-tick overlap
   of the same task).
2. Resolve `handler_key` → handler. **On an unknown `handler_key`** (a built-in
   whose handler was removed in code, or a Phase-3 seed row selected by an old
   pod mid-rollout before the new handler registers): record `last_status=error`
   **once**, **advance `next_run_at`** (or auto-disable), and skip — do NOT leave
   the row perpetually due, or the engine error-spams it every tick forever
   (Review D3/D4).
3. Run the handler with `task.params`.
4. Write `last_run_at` / `last_status` / `last_error` / `last_duration_ms`.
   Emit a **log/metric on status transition** (ok→error, error→ok) so an
   intermittently-failing task is visible without opening the UI — the `last_*`
   columns keep only the most recent run, so a transient failure would otherwise
   be overwritten and invisible (Review D5). Consider routing an `error`
   transition through the existing proactive/MCP-health alert path.

### Connection budget (Review D1)

`_run_one` holds a **dedicated lock connection** (`0x5354`) for the entire
handler duration, and a migrated **per-user looper** (e.g. the KG reconciler)
opens its **own** dedicated lock connection per user (`0x4B47`) plus per-user
work sessions — so one such task peaks at ~4 concurrent connections, not 2-3.
The SQLAlchemy pool is `pool_size=10 + max_overflow=20 = 30`
(`utils/config.py`, `services/database.py`), and **that same pool serves every
FastAPI request + WS handler**. The failure mode is request handlers blocking on
connection checkout, so the engine must leave headroom: `Semaphore(3)` caps the
concurrent-task fan-out well under the pool while reserving the bulk for request
traffic. A handler that itself consumes multiple connections is the reason the
bound is 3, not 10.

### Cron timezone (Review D2)

`croniter` on a naive/UTC datetime evaluates in UTC, which makes "every Monday
08:00" meaningless to the operator. Cron is evaluated against a **tz-aware
`now()`** in a configured zone — reuse the existing `daypart_timezone`
(`utils/config.py`; empty ⇒ UTC), consistent with the day/night watcher and
presence analytics. The assumed timezone is stored/shown in the UI so a cron
schedule is unambiguous. Interval jobs are tz-independent.

### Boot pass (the #678 fix — Review C2)

At startup, for every **enabled `run_at_boot`** task, force
`next_run_at = now()` regardless of the persisted value. A daily task whose
stored `next_run_at` is 20h out must still fire on a pod that recycles faster
than its interval — the persisted `next_run_at` alone would defeat `run_at_boot`
exactly as the legacy loop's leading sleep did.

### Single-flight (Review H5)

ONE fixed advisory-lock namespace **`0x5354`** ("ST"), documented alongside the
existing scheduler namespaces (`0x4B47` KG reconciler, `0x4F42` obligation
notifier, `0x4F43` calendar sync, `0x4F44` digest, `0x5341` fact-override
reindex). `key = task.id`. Held on a **dedicated connection** for the handler's
duration — the `kg_reconciler_service._resolve_lock_engine` pattern. A lock
taken on the work session would be dropped by the `pg_advisory_unlock_all()`
pool-checkin hook in `services/database.py`, so the dedicated connection is
mandatory, not cosmetic.

### Gates: seed vs runtime (Review H4)

`settings.*_enabled` flags **seed** a task's `enabled` at first-create; the UI
owns `enabled` thereafter. BUT some jobs carry a **compound/runtime gate that
does not live in the service function** — e.g. `obligation_deadline_notifier`
and `obligation_digest`: their `scan_all_users()` has **no** internal
`proactive_enabled` check; the gate lives only in the `_schedule_*` wrapper. If
such a handler is migrated naively, the ledger is consumed (milestones marked
sent) without delivery when proactive is off.

**Rule:** every migrated handler must **re-assert its full runtime gate inside
the handler**. Migration is preceded by a per-job **audit of gate LOCATION**
(wrapper vs service fn).

### Interval floor (Review M6)

Reject `interval_seconds < engine_tick`. Sub-tick jobs — `reminder_checker`
(15s), the kiosk internal-health refresher (30s) — are **NOT migrated**; they
stay on legacy `_spawn_periodic_task` to avoid a latency regression from the 10s
engine cadence.

### Seeding (Review M8)

`ensure_builtin_tasks()` = `INSERT ... ON CONFLICT (name) DO NOTHING` —
create-if-missing, **never clobber admin edits**, and race-safe across a rolling
deploy's two pods both running the boot pass.

## Phases

### Phase 1 — Engine + model + registry + the Paperless dedupe job (backend only)

1. **MCP (`renfield-mcp-paperless`, v1.12.0)** — new
   `dedupe_documents(dry_run=True, max_delete=200, metadata_match=True)`: the
   **FULL** dedupe (Review C3 — NOT checksum-only, or re-scan duplicates never
   drain). Ports the proven logic from `services/paperless_dedupe_tool.py` INTO
   the MCP: checksum grouping (PASS 1) + metadata-identity/OCR grouping
   (PASS 2, via the MCP's own `get_document`) + keep-lowest-id + delete up to
   `max_delete` to trash + circuit-breaker (abort if an already-deleted id
   reappears) → `{scanned, groups, deleted, remaining, complete}`. Honors
   "Paperless logic in the MCP" AND avoids a weaker fork. Permission-map
   `dedupe_documents: mcp.paperless.write`. Cross-repo: commit → repin
   `requirements.txt` → backend image (ships in the same image → no skew).
   - **Avoid the two-codebase drift (Review D9):** the interactive
     `internal.paperless_dedupe` (`services/paperless_dedupe_tool.py`, ~470 lines
     of the same **destructive** delete op) becomes a **thin caller of the MCP
     tool IN Phase 1** — not "left as-is short-term," which is exactly the drift
     of one dangerous op living in two places.
   - The port must externalize the three Renfield-side knobs the MCP won't have:
     `paperless_dedupe_metadata_match_enabled` → the `metadata_match` param,
     `paperless_dedupe_delete_batch` (the rate-limit retry cap, **distinct from
     `max_delete`**) → a `delete_batch` param, and the `Permission.ADMIN` gate →
     stays enforced on the Renfield side (the scheduled job runs as the owner
     admin; the interactive tool keeps its ADMIN gate).
2. **Migration** `pcXXXX_scheduled_tasks` (down_revision
   `pc20260825_paperless_task_id`; idempotent create-table per
   `pc20260714_meetings.py`). **Re-verify `alembic heads` against `main` at
   implementation time** — migrations land frequently, so the down_revision may
   have moved past `pc20260825` by then (Review B3).
3. **Model** `ScheduledTask` (mirror `Meeting`), **registry**
   `services/scheduled_tasks/registry.py`
   (`Handler = Callable[[FastAPI, dict], Awaitable[TaskRunResult]]`; each
   registered handler may declare an optional **param schema** so `params` JSONB
   is validated on `PATCH`/`POST` rather than failing at handler runtime —
   Review D8), **engine** `services/scheduled_tasks/engine.py` (tick + boot pass
   + `ensure_builtin_tasks` + advisory-lock). Also **update the now-stale comment
   at `services/database.py`** that claims advisory locks are used "for exactly
   one purpose" — this is the 6th lock user (Review B2).
4. **`api/lifecycle.py`** — `_schedule_task_engine(app)` (engine loop + boot
   pass), registered in the lifespan block. No existing `_schedule_*` removed
   yet.
5. **Built-ins (Phase-1 subset):**
   - **paperless-dedupe** — handler calls `mcp.paperless.dedupe_documents` via
     `app.state.mcp_manager`; seeded interval 300s, `is_builtin=True`;
     **self-gates on `settings.paperless_dedupe_reconciler_enabled` at runtime**
     (Review M7 — so a ConfigMap env-flip activates it in Phase 1, before any UI
     toggle exists; the row is seeded present+enabled and the runtime flag
     controls it).
   - 1-2 trivial jobs (`federation_audit_cleanup`, `upload_cleanup`) migrated to
     prove the engine end-to-end; their `_schedule_*` removed once verified.
6. **Config:** `croniter` dep;
   `paperless_dedupe_reconciler_enabled` / `_interval` / `_max_delete`;
   `scheduled_tasks_engine_tick_seconds` (default 10); a `scheduled_tasks_enabled`
   frontend flag (false-safe, for the Phase-2 UI).
7. **Tests:** engine (spawn-not-await independence, due-selection, interval+cron
   next-run, start/end bounds, boot-force `run_at_boot`, advisory-lock skip,
   handler error → `last_status`, interval-floor reject); `ensure_builtin_tasks`
   ON CONFLICT idempotency + no-clobber; registry; MCP `dedupe_documents`
   (checksum+metadata+OCR+circuit-breaker+dry_run); the paperless-dedupe handler
   self-gate.

### Phase 2 — REST API + admin UI ("Geplante Aufgaben")

`api/routes/scheduled_tasks.py` (`/api/scheduled-tasks`,
`require_permission(Permission.ADMIN)`, registered in `main.py`):

- `GET` list / `GET {id}`
- `PATCH {id}` — enable, schedule, start/end, params
- `POST {id}/run-now` — sets `next_run_at = now()`; the advisory lock prevents a
  double-run against an in-flight tick. **UI honesty (Review D6):** run-now is
  *schedule-soonest*, not *execute-immediately* — there's up to `engine_tick`
  (10s) latency, and if the task is already running the click is a silent no-op
  (the lock skips it). The UI shows "running / will run within ~Ns", not an
  implied instant execution.
- `POST` create / `DELETE {id}` — custom tasks only; built-ins are edit-not-delete

**Disable-mid-run (Review D7):** `PATCH enabled=false` stops *future* spawns but
does **not** cancel an in-flight `asyncio.Task` — it finishes. The UI surfaces
"running" distinctly from "enabled" so this is not surprising.

Frontend (mirror `RolesPage` / `MaintenancePage`; `/add-frontend-page`):
`pages/ScheduledTasksPage.tsx` (table: name, human-readable schedule, next/last
run, status badge, start/end, enable toggle, edit, run-now), `App.tsx`
`<AdminRoute>` `/admin/scheduled-tasks`, `Layout.adminNavigationConfig`
`nav.scheduledTasks` (CalendarClock icon, gated on the feature flag),
`api/resources/scheduledTasks.ts` + `api/keys.ts` + i18n (de/en/it + `.pro`).

Once the UI ships, the paperless-dedupe env self-gate can be dropped in favor of
the DB `enabled` toggle.

### Phase 3 — Migrate the remaining schedulers

Incrementally, per job: register its `_schedule_*` tick body as a handler + a
built-in seed row, **re-assert its full runtime gate inside the handler**
(Review H4 — audit gate location first), then delete the `_schedule_*` fn + its
lifespan call. Preserve `run_at_boot`, intervals (weekly 604800s is fine),
per-user loopers (the handler calls the service fn; the per-user advisory locks
stay inside the service — distinct namespaces, no collision with `0x5354`). Each
migrated job is verified against its old behavior before the legacy code is
removed.

**Kept legacy (not migrated):**
- `_schedule_whisper_preload` — one-shot, not periodic.
- `_schedule_notification_poller` — holds a persistent connection.
- The sub-`engine_tick` jobs (`reminder_checker`, kiosk internal-health) — see
  the interval floor.

## Deploy & activation

- Per phase. Phase 1 also repins the MCP (same backend image → no skew).
- Phase-1 migration via the alembic job (xidra: strip the hardcoded
  `namespace: renfield` line in `k8s/alembic-upgrade-job.yaml`).
- **Default inert:** the engine runs; built-ins are seeded with their existing
  enabled-defaults; paperless-dedupe is self-gated off.
- **Activate paperless-dedupe (Phase 1):**
  `PAPERLESS_DEDUPE_RECONCILER_ENABLED=true` on xidra (runtime self-gate) →
  drains the backlog, then idles.
- **Phase 2+:** toggle in the UI.

## Verification

- `.159`: engine/registry/handler + MCP tests green; existing scheduler tests
  unaffected (Phases 1/2 are add-only).
- Phase 3: per migrated job, assert the seed row + a tick invokes the same
  service fn + the gate re-check; no behavior change.
- Post-deploy: `/admin/scheduled-tasks` lists schedules and last-run; enabling
  paperless-dedupe on xidra drains "deleted N, ~M remaining" → 0 (verify as the
  DB checksum-extras aggregate → ~0; **counts only**, never document
  titles/content, especially on xidra).

## Risks / decisions

- **Engine-with-spawn** (not inline-await, not per-row-loop): task independence
  via per-tick spawned tasks. The per-row-loop was the reviewer's alternative —
  equivalent once spawn + boot-force are in, and the single engine wins on
  UI-edit + cron/dates.
- **Migrating 25 jobs** is the main risk → phased, per-job gate-location audit,
  behavior-preserving, verified; two non-periodic + the sub-tick jobs stay
  legacy.
- **Autonomous metadata/OCR dedup** deletes on a heuristic, unsupervised —
  mitigated by recoverable trash (Paperless-ngx 2.x, ~30-day window),
  keep-lowest-id, the circuit-breaker, opt-in, and the admin can set the task's
  `metadata_match=false` for checksum-only caution.
- **Settings-flag semantics change** (seed default vs runtime gate) —
  documented; compound gates re-asserted in-handler so a precondition-off still
  no-ops.
- New `croniter` dep — small, standard; interval jobs don't use it.

## Review history

**Rounds 1–2** (folded in):

- **C1** — a single inline engine loop serializes all tasks → **spawn per-task**
  (`asyncio.create_task` under a Semaphore).
- **C2** — a persisted `next_run_at` defeats `run_at_boot` (#678) → **boot-force**
  `next_run_at = now()` for enabled `run_at_boot` tasks.
- **C3** — checksum-only dedupe won't drain re-scans → **full metadata+OCR
  heuristic** in the MCP tool.
- **H4** — compound gates not in the service fns → **re-assert the runtime gate
  in each handler**; audit gate location per job.
- **H5** — **fixed advisory-lock namespace `0x5354` on a dedicated connection**.
- **M6** — exclude sub-`engine_tick` jobs (interval floor).
- **M7** — Phase-1 runtime-flag activation for paperless-dedupe (before the UI
  exists).
- **M8** — `INSERT ... ON CONFLICT (name) DO NOTHING` seeding.

**Round 3** (complete code-grounded review — all C1/C2/H4/H5/M8 claims verified
accurate against the code; verdict "sound to build with the fixes below"):

- **D1 (HIGH)** — the Semaphore was never given a value and the budget ignored
  `max_overflow` + request traffic → **`Semaphore(3)`**, budgeted against the
  real `10 + 20` pool minus request headroom, noting a handler may consume
  multiple connections.
- **D3/D4 (MEDIUM)** — an unknown `handler_key` (removed handler / rolling-deploy
  ordering) would error-spam every tick forever → **skip + advance/auto-disable,
  record error once**.
- **D2 (MEDIUM)** — cron timezone unspecified → **evaluate against a tz-aware
  `now()`** (reuse `daypart_timezone`), shown in the UI.
- **D9 (MEDIUM)** — dedupe forked across two codebases → make
  `internal.paperless_dedupe` a **thin caller in Phase 1**; pass `delete_batch`
  through as a param.
- **D5 (MEDIUM)** — `last_*`-only hides intermittent failures → **log/metric on
  status transition**, optionally route `error` to the proactive alert path.
- **D6/D7/D8 (LOW)** — run-now is schedule-soonest not instant; disable doesn't
  cancel an in-flight run; `params` needs a per-handler schema — all now stated.
- **B1/B2/B3** — pool ceiling is `10+20=30` not 10; update the stale
  "exactly one purpose" advisory-lock comment in `database.py`; re-verify the
  alembic head against `main` at build time.
