# Obligation-Deadline Notifier — plan (2026-06-06)

Branch: `feat/obligation-deadline-notifier`. Scope: **A + B** (notifier core +
Bestätigt server-migration on the shared ledger). Digest (C) = fast-follow.

## Design authority
Cross-model learning `schicht-a-obligations-source-of-truth` (overrides the
earlier `schicht-a-reminder-durability`):
- **Do NOT** reuse `check_due_reminders` / pre-materialize `Reminder` rows
  (back-fire storm on restart, household broadcast = privacy breach, no lock).
- Obligations (`document_facts`) ARE the scheduling source of truth.
- **One daily idempotent scan** → due lead-time milestones + a
  `(obligation_id, milestone)` notified-ledger, **owner-targeted**.
- Survives pod restarts (safety component — missed-deadline scar).
- **Legal-gate kinds always human-gated** → notify but flag for `/brain/review`,
  never auto-act (per user decision: notify-but-human-gated).

## Architecture
- **Ledger table** `obligation_acknowledgements`: `(document_fact_id, user_id,
  milestone)` UNIQUE. `milestone` ∈ {`14d`,`7d`,`3d`,`1d`,`due`,`overdue`} for
  notifier rows (user_id=owner) OR `confirmed` for the agenda's Bestätigt
  (user_id=acker). Serves BOTH the notified-ledger and the Bestätigt state.
- **Milestone bucket** (no storm): `current_milestone(days_until)` returns the
  single current bucket; fire only if `(fact, owner, bucket)` not in ledger.
  First-enable at d=2 fires only `3d`, never 14d/7d at once.
- **Delivery:** reuse `NotificationService.process_webhook(target_user_id=owner,
  privacy="personal", source="obligation_notifier")`. Degrades gracefully if
  `proactive_enabled` off / no ha_glue hooks (persisted, not broadcast).
- **Scheduler:** `_spawn_periodic_task(run_at_boot=True)` daily, per-user session
  + advisory lock (mirror `_schedule_kg_reconciler`).
- **Confirmed suppresses:** scan skips a fact entirely if a `confirmed` ack
  exists for the owner.

## Tasks
### Backend — notifier core (A)
- [x] B1 config: `obligation_notifier_enabled=False`, `obligation_notifier_interval=86400`, `obligation_notifier_overdue_grace_days=30`
- [x] B2 model `ObligationAcknowledgement` + `OBLIGATION_MILESTONE_CONFIRMED`
- [x] B3 alembic migration `pc20260606_oblig_acks` (down_revision = verified single head `pc20260604b_kgmp`)
- [x] B4 service `services/obligation_deadline_notifier.py` — `current_milestone` (no-storm), `run_for_user` (advisory lock), owner-scoped scan, ledger dedup, legal-gate flag, delivery via NotificationService, `scan_all_users`
- [x] B5 scheduler `_schedule_obligation_deadline_notifier()` (run_at_boot) + registered in lifespan

### Backend — Bestätigt migration (B)
- [x] B6 endpoints `POST/DELETE /api/atoms/obligations/{fact_id}/confirm` (circle-gated 404)
- [x] B7 obligations response carries `confirmed: bool` + `is_visible` helper

### Frontend — Bestätigt rewire (B)
- [x] F1 `useConfirmObligation` / `useReopenObligation` mutations
- [x] F2 `useBestaetigt` → server (optimistic override + 5s undo→DELETE); one-time localStorage→server migration
- [x] F3 `confirmed` on DocumentFact; ObligationsPage passes `f.confirmed`; i18n `obligations.confirmError` (de+en)

### Tests
- [x] T1 backend PG (8): bucket logic, no-storm, no re-fire, progression, owner-targeting, confirmed suppresses, legal_gate flagged, too-far, advisory-lock no-op, list_owner_user_ids — GREEN on .159
- [x] T2 endpoint PG (3): confirm/reopen roundtrip, circle 404, per-user — GREEN on .159
- [~] T3 migration: down_revision verified as single repo head + schema validated by create_all in the 24 PG tests. Full-chain `alembic upgrade head` on .159 blocked by env friction (root-owned __pycache__ + stale tree); migration is pure create_table (no data ops). Real run happens via the staging alembic-upgrade-job at deploy.
- [x] T4 frontend (9 hook + 6 page + 7 row = 22): confirm→POST, undo/reopen→DELETE, server-flag layering, one-time migration — GREEN

### Ship
- [ ] /review → update docs (CLAUDE.md, TODOS.md, docs/SECOND_BRAIN.md/schicht-a-gui-plan) → wait → merge
- [x] Config defaults dark (opt-in), mirror proactive_*; co-author trailer; no push without approval

## Deferred (fast-follow)
- C: weekly catch-all digest (safety floor for never-extracted/missed)
- `.ics` export; per-fact TierPicker; agenda `Fakten` filter chip
