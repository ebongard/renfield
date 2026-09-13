# Obligation → Calendar Sync

Mirrors each user's **open obligations** (Schicht-A bills + Behörde deadlines with
a printed Frist) into their chosen calendar as events, and keeps them in step:
create on first sight, update when the date/summary changes, delete when the
obligation is confirmed/handled, drops out of the window, or its fact is purged.

**Status:** implemented, tested and deployed — but **dark on every instance**
(`OBLIGATION_CALENDAR_SYNC_ENABLED` and `CALENDAR_ENABLED` both unset in the
`renfield-env` of the household and of xidra, checked 2026-09-13). Turning it on needs **no code
change**, but it is more than a flag flip: the Calendar MCP is not wired into the
pod today (see the checklist).

## What it is

- **Per-user opt-in.** A user picks a calendar in the Fristen agenda
  (`/wissen/fristen` or legacy `/brain/fristen`). No pref row → that user is never
  synced. The picker only appears when the Calendar MCP offers them a *writable*
  calendar.
- **Stateless reconciler, run by the Scheduled-Tasks engine.** The built-in task
  **„Fristen-Kalender-Sync"** (`handler_key=obligation_calendar_sync`, daily,
  `run_at_boot`) calls `services/obligation_calendar_sync.reconcile_all_users`,
  per-user advisory-locked (pg ns `0x4F43`). The task is seeded *enabled* and
  **self-gates** on `OBLIGATION_CALENDAR_SYNC_ENABLED`: while the flag is off every
  run records `skipped: obligation_calendar_sync_enabled is off`. Obligations are
  the source of truth; the `obligation_calendar_events` ledger (fact → calendar
  `event_id`) is the only state, so it is idempotent and restart-safe.
- **Owner-scoped.** Events are written to the obligation OWNER's calendar via the
  Calendar MCP (`mcp.calendar.{create,update,delete}_event`); the MCP enforces
  per-calendar write access by `user_id`.
- **Timed events.** All-day events are unsupported by the MCP, so each event is a
  30-min slot at `OBLIGATION_CALENDAR_EVENT_HOUR` (default 09:00, interpreted in
  the calendar backend's timezone — Europe/Berlin for the Google backend).

## Moving parts

| Piece | Location |
|---|---|
| Reconciler service | `src/backend/services/obligation_calendar_sync.py` |
| Scheduled task | `src/backend/services/scheduled_tasks/builtins.py` — `_obligation_calendar_sync_handler` + seed „Fristen-Kalender-Sync"; admin view `/admin/scheduled-tasks` |
| Pref + teardown routes | `src/backend/api/routes/atoms.py` — `GET/PUT /api/atoms/obligations/calendar-pref` |
| DB tables | migration `pc20260609_oblig_cal`: `obligation_calendar_pref`, `obligation_calendar_events` (FK `ON DELETE SET NULL`) |
| Config flags | `src/backend/utils/config.py` — `calendar_enabled`, `obligation_calendar_*` |
| Calendar MCP | `renfield-mcp-calendar` (stdio, installed in the backend image via `requirements.txt`); stanza `config/mcp_servers.yaml` → `- name: calendar` |
| Calendar accounts template | `config/calendar_accounts.yaml` (`work`=EWS, `family`=Google, `verein`=CalDAV commented out) |
| Frontend opt-in UI | `src/frontend/src/pages/ObligationsPage.tsx` (calendar `<select>`) + `useObligationCalendarPref` / `useSetObligationCalendarPref` in `api/resources/brain.ts` |
| Tests | `tests/backend/test_obligation_calendar_sync_pg.py` (14, real PG) · `tests/frontend/react/pages/ObligationsPage.test.tsx` (calendar-sync selector) |

## Config flags

```bash
CALENDAR_ENABLED=false                   # Calendar MCP stanza gate
OBLIGATION_CALENDAR_SYNC_ENABLED=false   # the sync gate — flip to true to enable
OBLIGATION_CALENDAR_SYNC_INTERVAL=86400  # daily (seconds); seeds the task interval
OBLIGATION_CALENDAR_EVENT_HOUR=9         # local hour for the (timed) event
OBLIGATION_CALENDAR_HORIZON_DAYS=90      # sync obligations due within N days
OBLIGATION_CALENDAR_RETAIN_PAST_DAYS=30  # keep past-due events this long
OBLIGATION_CALENDAR_MAX_OPS_PER_RUN=100  # cap create/update MCP calls per user per pass
```

`OBLIGATION_CALENDAR_SYNC_INTERVAL` only **seeds** the task (`INSERT … ON CONFLICT
(name) DO NOTHING`); once the row exists, change the interval in
`/admin/scheduled-tasks`, not the env var.

With the sync flag on but the MCP unavailable, every calendar call is logged as a
`calendar sync: … failed` warning and skipped — **the run itself does not fail**.
Consequence: the Scheduled-Task failure-streak alert will **not** flag a
misconfigured Calendar MCP. After enabling, check the backend log for
`calendar sync:` warnings rather than relying on the task status.

## Prod-enablement checklist

No migration is needed: `pc20260609_oblig_cal` has been in the chain since
2026-06-09. Replace `<ns>` with the instance namespace (household `renfield`;
xidra's config lives in the `x-ren` repository).

1. **Put the account config into the pod.** The `renfield-mcp-config` ConfigMap is
   mounted whole (no `items`), but each file needs its own `subPath` mount, and
   `calendar_accounts.yaml` has none today. Mirror the existing
   `mail_accounts.yaml` pattern:
   - add a `calendar_accounts.yaml` key to `renfield-mcp-config` containing the
     real accounts (the repo file is a template);
   - add a `subPath` mount at `/app/config/calendar_accounts.yaml` in
     `k8s/backend.yaml`;
   - set `CALENDAR_CONFIG=/app/config/calendar_accounts.yaml` in `renfield-env`.
     **The stanza's default `/config/calendar_accounts.yaml` does not exist in
     the pod** — the same trap `MAIL_ACCOUNTS_CONFIG` already works around.
2. **Calendar backend credentials** (only for the calendars users will sync into).
   Secrets belong in a Secret, never in `renfield-env`'s ConfigMap or in git.
   - **`work` (Exchange EWS):** `CALENDAR_WORK_USERNAME` / `CALENDAR_WORK_PASSWORD`
     (referenced by name in the accounts file).
   - **`family` (Google):** a Google Cloud "Desktop app" OAuth client — **the
     operator provides it; never invent credentials.** Two pod-path corrections
     against the template, both mandatory:
     - `credentials_file`: mount the client secret from a Secret and point the
       account at that pod path (the template's `/config/…` does not exist).
     - `token_file`: must be on a **PVC-backed** path, e.g. under
       `/app/data/cache-home/`. The template's `/data/calendar/…` is not a mount
       in the pod, so the token would be lost on every restart.

     Then mint the token once:
     ```
     kubectl -n <ns> exec -it deploy/renfield-backend -- \
       python -m renfield_mcp_calendar --auth google --calendar family
     ```
     Not yet exercised inside a pod: the flow is a desktop OAuth flow. If it
     cannot complete from an `exec` session, run the same command on a
     workstation with the same client secret and copy the resulting token file
     onto the PVC path. After bootstrap the token refreshes automatically.
3. **Flip the flags** in the committed `renfield-env` ConfigMap (flags live in
   git, never a `kubectl patch`): `CALENDAR_ENABLED=true` and
   `OBLIGATION_CALENDAR_SYNC_ENABLED=true`.
4. **Restart the backend** — settings are read at startup. The deployment is
   `Recreate` and gated on the LLM endpoint, so pre-check it is healthy. The task
   is `run_at_boot`, so the first real pass runs immediately.
5. **Verify:**
   - `calendar` appears in `GET /api/mcp/servers` with its tools;
   - „Fristen-Kalender-Sync" in `/admin/scheduled-tasks` shows a run that is no
     longer `skipped: … is off`;
   - a user opens `/wissen/fristen`, the „Kalender-Sync" selector lists their
     writable calendar(s), picking one persists the pref, and their open
     obligations appear as events (run the task from the admin page instead of
     waiting a day);
   - clearing the selector tears their events back down synchronously.

### Rollback

- **Stop syncing without a restart:** disable „Fristen-Kalender-Sync" in
  `/admin/scheduled-tasks`.
- **Full rollback:** set `OBLIGATION_CALENDAR_SYNC_ENABLED=false` and restart.

Ledger and pref rows remain in both cases, so re-enabling resumes cleanly. To
remove a user's events first, have them clear the calendar selector (teardown
runs synchronously), or leave the rows — they are inert while the sync is off.

## Known caveats (decide before enabling)

- **At-least-once duplicate window (P2).** The Calendar MCP exposes no idempotency
  key, so a crash in the narrow window between a successful `create_event` and the
  ledger commit can leave a duplicate event. Same class as the notifier. Closing
  it needs a pre-create marker-scan or MCP idempotency support — tracked in
  `TODOS.md`. For a daily, low-volume obligation sync the exposure is small but
  non-zero; acceptable for first enable.
- **Timed, not all-day.** Events land at `OBLIGATION_CALENDAR_EVENT_HOUR`; the MCP
  has no all-day support. (The read-only `.ics` export at
  `/api/atoms/obligations/export.ics` IS all-day — different path.)
- **A pref pointing at a now-unwritable calendar errors every pass** (logged); it
  never self-heals until the user changes the pref.

## User decisions required

- **Which calendar backend(s)** to expose for sync — `work` (Exchange), `family`
  (Google), or add `verein` (Nextcloud CalDAV, commented out in
  `calendar_accounts.yaml`).
- **Google OAuth client secret + one-time `--auth` bootstrap** if the `family`
  Google calendar is a sync target (the credentials file is user-supplied).
- **Accept the at-least-once duplicate window** for first enable, or wait for the
  P2 idempotency follow-up.
