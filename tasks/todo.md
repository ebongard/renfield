# Scheduled Tasks subsystem — Phase 1 (#1137)

Design: `docs/design/scheduled-tasks.md`. Branch: `feat/scheduled-tasks-1137`.

## Phase 1 — Engine + model + registry + Paperless dedupe job (backend only)

- [x] Config: `croniter` dep; `paperless_dedupe_reconciler_enabled`/`_interval`/`_max_delete`; `scheduled_tasks_engine_tick_seconds`/`_max_concurrent`; `scheduled_tasks_enabled` (frontend flag)
- [x] Migration `pc20260827_scheduled_tasks` (down_revision `pc20260825_paperless_task_id`, re-verified head)
- [x] Model `ScheduledTask` (models/database.py) + constants
- [x] Registry `services/scheduled_tasks/registry.py` (handler_key → handler; optional param validator)
- [x] Engine `services/scheduled_tasks/engine.py` (tick + spawn-per-task + Semaphore(3) + advisory-lock 0x5354 dedicated-conn + boot pass + ensure_builtin_tasks ON CONFLICT + interval floor + unknown-handler_key skip+backoff + cron tz + status-transition log)
- [x] `api/lifecycle.py`: `_setup_task_engine(app)` + register in lifespan; migrated `federation_audit_cleanup` + `upload_cleanup` (removed their `_schedule_*`); updated stale advisory-lock comment in `services/database.py` (B2)
- [x] Built-ins: paperless-dedupe (self-gates on `paperless_dedupe_reconciler_enabled`) + `federation_audit_cleanup` + `upload_cleanup` handlers
- [x] MCP `dedupe_documents` (sibling repo `renfield-mcp-paperless` v1.12.0, 24 tests green) — NOT committed/pushed yet (awaits approval)
- [x] Tests: `tests/backend/test_scheduled_tasks_engine.py` (pure helpers, registry, seeding idempotency+no-clobber, boot-force, execution ok/error/unknown-skip/disabled, tick due-selection+bounds+spawn-independence, dedupe handler self-gate)
- [x] Run backend tests on .159 green — 22/22 pass (incl. cron, upload-cleanup regression, shutdown drain)
- [x] Code review of implementation — 2 findings fixed (upload_cleanup NameError; untracked spawned runs → drain)
- [ ] **FOLLOW-UP (this PR):** rewire interactive `internal.paperless_dedupe` as a thin caller of `mcp.paperless.dedupe_documents` (D9) + rewrite `test_paperless_dedupe_tool.py` to mock the MCP call. Deploy-coupled: requires the requirements.txt MCP pin bump to the v1.12.0 commit (ships in the same backend image). Autonomous scheduled path already uses the MCP tool — no fork in the new code.
- [ ] **DEPLOY STEP:** after the MCP PR merges, bump `renfield-mcp-paperless` archive pin in `src/backend/requirements.txt` to the v1.12.0 SHA.

## Review / outcome

Phase 1 COMPLETE + committed (not pushed):
- Renfield `feat/scheduled-tasks-1137`: `bf4ac680` (design doc) + `7b36d131` (feat: engine + dedupe job + D9 rewire).
- MCP `renfield-mcp-paperless` `feat/dedupe-documents`: `8d0ffc7` (dedupe_documents v1.12.0).
- Tests: 22 engine + 14 dedupe-thin-caller green on .159; MCP 24 tests green.
- Three design reviews + one implementation code review; all findings fixed (upload_cleanup NameError; untracked spawned runs → drain).

### Remaining before "Phase 1 shippable"
- Push + PR both repos (awaiting approval).
- **DEPLOY:** after the MCP PR merges, bump the `renfield-mcp-paperless` archive pin in `src/backend/requirements.txt` to the v1.12.0 SHA (ships the tool in the backend image; the interactive thin caller + scheduled job both need it).
- Activate on xidra: `PAPERLESS_DEDUPE_RECONCILER_ENABLED=true` (runtime self-gate) to drain the duplicate backlog, then it idles.

### Phase 2 (next): `/api/scheduled-tasks` CRUD + admin UI "Geplante Aufgaben".
### Phase 3: migrate the remaining ~20 schedulers (per-job gate-location audit).
