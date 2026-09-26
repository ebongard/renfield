---
paths:
  - "src/backend/services/scheduled_tasks/**"
  - "src/backend/services/paperless_index_health.py"
  - "src/backend/services/watchdog.py"
  - "src/backend/services/ops_alert.py"
  - "src/backend/api/routes/scheduled_tasks.py"
---
# Scheduled tasks, failure-streak alerting, watchdog

Loaded only when the engine, a watchdog-pattern built-in or `ops_alert.py` is read.
Long form: `docs/design/scheduled-tasks.md` (#1137); index-health verdicts in `docs/FOLDER_INGEST.md`.

## Engine (`services/scheduled_tasks/{engine,registry,builtins}.py`, model `ScheduledTask`/`scheduled_tasks`, `pc20260827`)
- ONE engine loop SPAWNS each due task as its own `asyncio.Task` (never inline in the tick); drain on shutdown.
- Single-flight = per-task advisory lock ns `0x5354` on a DEDICATED connection, held for the whole handler. Per-user
  locks stay inside the services (distinct namespaces).
- Seeding is `INSERT … ON CONFLICT (name) DO NOTHING`. `run_at_boot` boot-forces `next_run_at` (#678). Interval
  floor = the engine tick. Unknown `handler_key` = skip + backoff, not an error. Cron is evaluated in the local tz.
- The engine always runs but is dark/inert until a task is activated (UI gated on `scheduled_tasks_enabled`).
- **Registering a built-in is engine-registry-only — NOT `agent_roles.yaml`.**
- **Every handler RE-ASSERTS its full runtime gate in-handler (H4).** Seed rows are enabled, so the flag check is the
  only thing keeping a flag-off job a no-op. The obligation notifier/digest gates were
  `settings.*_enabled AND proactive_enabled` only in the old wrapper, and `scan_all_users` consumes the reminder
  ledger — a dropped gate silently drops reminders.
- Kept legacy on `_schedule_*`, do not migrate: `whisper_preload` (one-shot), `notification_poller` (persistent
  connection), `reminder_checker` (15s, below the tick floor), the 3 kiosk WS-push refreshers.

## Failure-streak alerting (`SCHEDULED_TASK_FAILURE_ALERT_ENABLED`, default ON = kill-switch; needs `PROACTIVE_ENABLED`)
- `scheduled_tasks.consecutive_error_count` + `error_alerted_at` (`pc20260912_taskalert`): at `_THRESHOLD` (3) ONE
  alert via `services/ops_alert.py`, re-alert after `_REALERT_SECONDS` (6h), ONE recovery notice — only if an alert
  was sent. Any non-error run (incl. a `skipped` unknown-`handler_key`) resets the streak.
- Counting is flag-INDEPENDENT (the admin-list badge stays honest with delivery off).
- `error_alerted_at` is in the DB on purpose: the in-process `ops_alert` ledger is a rate limiter a restart may
  re-arm; a crash-looping pod would otherwise alert on every boot.
- Alerting is wrapped: a broken notification pipeline must never cost the run-state commit.
- `ops_alert.py` is the ONE alert path (tasks, watchdog, MCP health). No second channel.
- `notify_admin` is also the ONE place that vouches a notification as technical (`llm_eligible=True`, `enrich=True`,
  `urgency=None → "auto"` with fallback `critical`): only its alerts may pass the LLM steps, and only for an
  `event_type` in `PROACTIVE_LLM_EVENT_TYPES`. Never route personal text through `notify_admin`; keep free-text
  error fields bounded (the engine caps `last_error` at 300 chars) because that text can reach the model.

## Watchdog pattern: raise, never alert
- `services/watchdog.py` (task `watchdog`, 120s, `WATCHDOG_ENABLED` + `WATCHDOG_TARGETS`, empty ⇒ inert) has NO
  alerting of its own: it RAISES on an unreachable target; the streak supplies threshold/ledger/TTL/recovery, and the
  task lock means one replica probes per tick (counters cannot fragment).
- Probes `/health/ready`, NEVER `/health` — the latter answers "ok" with a dead DB.
- Deliberately NOT `run_at_boot` (would alert on our own startup / a peer restarting in a coordinated deploy).
- A second target failing mid-streak raises no second alert — the task's error text must name ALL failing targets.

## `paperless_index_health` (`PAPERLESS_INDEX_CHECK_ENABLED` + separately `PAPERLESS_INDEX_HEAL_ENABLED`, both dark)
- Paperless has NO REST reindex; heal = an EMPTY partial PATCH per document (`DocumentViewSet.update` re-indexes
  unconditionally; no field sent). It ALWAYS fires `document_updated` → the MCP refuses to heal (`heal_blocked`) while
  enabled "Document Updated" workflows exist or cannot be read, unless `PAPERLESS_INDEX_HEAL_ALLOW_WORKFLOWS`.
- Misses count as `degraded` only once the probe is PROVEN; unproven/rejected/cut-short = `inconclusive`, NEVER
  raised (heal re-saves ONE canary at most). 404 = skipped, un-probed-by-deadline = unverified — not failed heals.
- RAISES on proven-degraded-with-heal-off, heal blocked, heal-ineffective-with-attempts-left, `index_error` — and
  KEEPS its Redis page cursor so the streak can build. After `PAPERLESS_INDEX_HEAL_MAX_ATTEMPTS` a doc is given up
  (excluded, direct rate-limited `ops_alert`, walk continues). Needs renfield-mcp-paperless ≥1.13.0.

## `vector_index_threshold` — ein Ausloeser gegen eine Ausnahme mit Verfallsdatum

Taeglich, schweigt im Normalfall. Die Aufgabe ZAEHLT NICHT — sie fragt je beobachteter Tabelle
(`document_chunks`, `conversation_memories`) per `EXPLAIN (FORMAT JSON)`, ob der Planer den HNSW-Index
NAEHME, wenn die Abfrage auf `halfvec` castete. Nur dann meldet sie. Kein `ANALYZE`, also keine
Ausfuehrung und keine Last.

🛑 Die erste Fassung meldete ab 4 000 Zeilen und wurde am Tag ihrer Einfuehrung widerlegt: xidra nutzt den
Index auf `kg_entities` schon bei 1 953 Zeilen, auf `document_chunks` bei 3 165 nicht. Heap-Seiten und
Indexform entscheiden, nicht die Zeilenzahl (416 Seiten bei 2 847 Zeilen gegen 150 bei 4 813, geschaetzter
HNSW-Einstieg Faktor acht auseinander). Eine Zahl, die das Falsche misst, ist kein Ausloeser, sondern eine
zweite Falle. Details: `services/vector_index_threshold.py`, Regel in `.claude/rules/migrations.md`.

## `satellite_fleet_watchdog` — weil ein toter Raum sonst still bleibt

Stuendlich. Vergleicht die eingebuchten Satelliten (`satellites`, nicht widerrufen,
aktiviert) gegen die LAUFENDE Registratur des `SatelliteManager` und meldet die Fehlenden
per `ops_alert.notify_admin`. Der `dedup_key` folgt der LAGE, nicht dem Lauf: solange
dieselben Raeume fehlen, wird nicht stuendlich erneut gemeldet.

🛑 **NICHT `last_authenticated_at` als Lebenszeichen.** Die Spalte wird beim
VERBINDUNGSAUFBAU gesetzt; ihr Alter misst „Zeit seit dem letzten Neuverbinden", nicht
„seit dem letzten Lebenszeichen". Am 2026-09-26 gemessen: direkt nach einem
Backend-Neustart zeigten alle drei GESUNDEN Satelliten vier Minuten, die drei toten Tage
bis Wochen — ein Waechter auf dieser Spalte haette also die Gesunden gemeldet und die
Toten verschwiegen. Sie liefert nur noch das SEIT WANN fuer den Meldetext.

🛑 **Karenz nach dem Start** (`SETTLE_SECONDS`, 300 s): die Registratur lebt im
Arbeitsspeicher und ist beim Start leer. Ohne Karenz meldete jeder Rollout die ganze
Flotte als tot, und ein Fehlalarm dieser Groesse macht jede echte Meldung wertlos.
Deshalb auch `run_at_boot=False`.

**Warum es das ueberhaupt gibt:** am 2026-09-26 war die halbe Flotte dunkel — Esszimmer
1 Tag (k8s-Knoten `NotReady`), Arbeitszimmer 2 Tage, BensZimmer **30 Tage** — und in
36 Stunden gab es keine einzige Satelliten-Benachrichtigung. MCP-Server hatten
Gesundheitsproben, Aufgaben eine Fehlerserien-Erkennung, die Satelliten nichts.

## Other self-gating built-ins
- Paperless dedupe: gates on `PAPERLESS_DEDUPE_RECONCILER_ENABLED`, calls `mcp.paperless.dedupe_documents` (MCP
  ≥1.12.0) — the logic lives in the MCP; `internal.paperless_dedupe` calls the same tool. No fork.
- `low_coverage_reindex` (`LOW_COVERAGE_REINDEX_ENABLED`, dark): re-enqueues with `force_ocr=False`; a doc already
  re-derived and still low-coverage is `attempted` and skipped — never build a re-OCR loop.
