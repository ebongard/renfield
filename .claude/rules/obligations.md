---
paths:
  - "src/backend/services/obligation_*.py"
---
# Obligations — deadline notifier, weekly digest, calendar sync

Loaded only when an `obligation_*` service is read. Long form: `docs/OBLIGATION_CALENDAR_SYNC.md` (calendar runbook),
`docs/SECOND_BRAIN.md` (notifier + digest background), routes/pages in `docs/CIRCLES.md`.

## Invariants
- **Obligations ARE the scheduling source of truth**: dated `document_facts` rows with `category='obligation'`. No
  `Reminder` rows, no reuse of the chat-reminder loop.
- All three jobs are per-user, idempotent, restart-safe and hold a per-user advisory lock: notifier `0x4F42`, digest
  `0x4F44`, calendar `0x4F43`. They run as Scheduled-Task built-ins (`obligation_deadline_notifier`,
  `obligation_digest`, `obligation_calendar_sync`), `run_at_boot`, and **re-assert their gate in-handler**.
- Flags — all dark: `OBLIGATION_NOTIFIER_ENABLED` and `OBLIGATION_DIGEST_ENABLED` each ALSO need
  `PROACTIVE_ENABLED`; `OBLIGATION_CALENDAR_SYNC_ENABLED` needs the Calendar MCP.

## Deadline notifier — `services/obligation_deadline_notifier.py`
- `current_milestone(days_until)` returns the **SINGLE current** lead-time bucket
  (`14d`/`7d`/`3d`/`1d`/`due`/`overdue`), so first-enable cannot back-fire every milestone already crossed.
- Each milestone fires once and is recorded in the `obligation_acknowledgements` ledger —
  `(document_fact_id, user_id, milestone)` UNIQUE — so a pod restart never re-fires (the missed-deadline safety
  property).
- The ledger's `"confirmed"` milestone IS the per-user Bestätigt store; a confirmed ack also suppresses that owner's
  further milestones.
- Legal-gate kinds are notified but flagged human-gated (message → `/brain/review`) — **never auto-acted**.
- Scan window `[today − OBLIGATION_NOTIFIER_OVERDUE_GRACE_DAYS, today + 14d]`; owner-targeted.
- Delivery: `NotificationService.process_webhook(target_user_id, privacy="personal")`. `ha_deliver_notification`
  presence-gates **BOTH the WS push and TTS** for non-public notifications, fail-closed — a personal reminder never
  fans out to all household devices.
- `NotificationService._compute_dedup_key` includes `target_user_id`, so two members' identical per-user
  notifications are not cross-deduped.

## Weekly digest — `services/obligation_digest.py` (the safety floor UNDER the notifier)
- Once per ISO week ONE summary per owner of every OPEN obligation with **no lower date bound** — it catches a
  late-extracted / very-overdue deadline the notifier's grace window missed. It cannot catch *never-extracted*.
- Deduped by a `(user, period_key)` row in its own table `obligation_digest_log` — NOT the TTL-reaped `notifications`
  row. The ISO week is in the title so two weeks' digests stay content-distinct.

## Calendar auto-push — `services/obligation_calendar_sync.py`
- Per-user opt-in via `obligation_calendar_pref` (no pref → no sync; `GET/PUT /api/atoms/obligations/calendar-pref`;
  clearing tears the user's events down FIRST).
- Stateless reconciler: diffs open obligations against the `obligation_calendar_events` ledger (fact→event_id) and
  create/update/deletes via `mcp.calendar.{create,update,delete}_event`, owner-scoped, op-capped.
- Ledger FK is `ON DELETE SET NULL`: a purged fact orphans the row (event_id kept) so the next pass deletes the event.
- Not-found-on-delete counts as done; MCP failures retry. Events are timed at `obligation_calendar_event_hour`
  (the MCP has no all-day events; the `.ics` export is all-day — a different path).
- Known: an **at-least-once duplicate window** on a crash between create and ledger-commit (no MCP idempotency key;
  P2 in `TODOS.md`).
