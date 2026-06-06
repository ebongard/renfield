# Obligation → Calendar auto-push (Calendar MCP) — plan (2026-06-06)

Branch: `feat/obligation-calendar-sync`. Decisions (user): **per-user calendar
preference** (each user picks their obligations calendar; no pref = no sync) ·
**delete event when handled** (confirmed / out-of-window / fact gone) · **full
reconciler** (create / update / delete, idempotent + restart-safe).

## MCP facts (from exploration)
- `mcp.calendar.create_event(calendar,title,start,end,description,location,user_id)` →
  `{"success":true,"event":{"id":...}}` inside `execute_tool` result `message` (JSON string). Parse it.
- `update_event(calendar,event_id,...)` → same shape. `delete_event(calendar,event_id,user_id)` → no id (ledger stores it).
- `list_calendars(user_id)` → `{"calendars":[{name,label,type}]}` = the user's WRITABLE calendars.
- All-day NOT supported on create → events are timed at `obligation_calendar_event_hour` (default 09:00, 30 min). Note limitation.
- user_id passed as `execute_tool(..., user_id=)` kwarg; MCP enforces per-calendar access by user_id.

## Backend
- [ ] B1 config: `obligation_calendar_sync_enabled=False`, `obligation_calendar_sync_interval=86400`, `obligation_calendar_event_hour=9`, `obligation_calendar_horizon_days=90`, `obligation_calendar_retain_past_days=30`
- [ ] B2 models: `ObligationCalendarPref(user_id unique, calendar_name)` + `ObligationCalendarEvent(document_fact_id FK SET NULL, user_id FK CASCADE, calendar, event_id, synced_obligation_date, synced_summary, ts)` UNIQUE(document_fact_id,user_id)
- [ ] B3 migration pc20260609 (down_revision = pc20260608_fact_tier_ov)
- [ ] B4 service `services/obligation_calendar_sync.py` — per-user advisory lock (ns 0x4F43), reconcile desired-set (open, owner, [today-retain, today+horizon]) ↔ ledger: create/update/delete via mcp execute_tool, parse event id, graceful failure; `reconcile_all_users(mcp_manager)`
- [ ] B5 scheduler `_schedule_obligation_calendar_sync(app)` (run_at_boot, daily) reading app.state.mcp_manager
- [ ] B6 routes: `GET/PUT /api/atoms/obligations/calendar-pref` (current + writable-calendar list via list_calendars; validate chosen name is writable)

## Frontend
- [ ] F1 hooks `useObligationCalendarPref` (GET) + `useSetObligationCalendarPref` (PUT)
- [ ] F2 ObligationsPage: a small "Kalender-Sync: [calendar ▼ / aus]" selector (writable calendars + Off)
- [ ] F3 i18n de+en

## Tests
- [ ] T1 backend PG: reconciler create (new obligation→event+ledger), update (date change→update_event), delete (confirmed→delete_event+row gone), orphan cleanup (fact gone→delete), no-pref→skip, out-of-window→delete, idempotent re-run no-op, advisory-lock no-op. Mock mcp_manager.execute_tool.
- [ ] T2 routes: get pref + available, put validates writable, clear
- [ ] T3 frontend RTL: selector lists calendars, set/clear calls PUT

## Notes / accepted
- Timed events (all-day unsupported by MCP) at the configured hour.
- Gated on `obligation_calendar_sync_enabled` (+ runtime calendar MCP availability — graceful skip if down).
- Per-calendar authz enforced by the MCP via user_id; sync passes manage perm acting for the owner.
- Re-extraction recreates facts → SET NULL orphans the ledger row → next reconcile deletes the stale event + recreates for the new fact.
