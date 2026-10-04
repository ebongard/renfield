---
paths:
  - "src/backend/services/scanner_jobs.py"
  - "src/backend/api/routes/scanner*.py"
---
# Dokumentenscan — scan jobs

Loaded only when the scanner-job service or route is read. Long form: `docs/design/scanner-ingest.md`.
The scanner (`renfield-mcp-scanner`, separate repo) is one more producer in FRONT of the existing ingest seam —
dedup / tier / PDF-Split / Paperless are unchanged.

## Job model
- `scan_document` only **STARTS** a job and returns `{job_id}` at once. Never run the scan inside the tool call (the
  30 s MCP timeout reports a scan as FAILED that is filed moments later).
- Completion is an **EVENT, never a poll**: the scanner POSTs `/api/scanner/job-event` to the instance that asked
  (per-caller token → `SCANNER_CALLER_TARGET_<CALLER>` → that target's URL + folder-ingest token) and retries for 24 h.
- `action_executor` records the requester (user, session, title, voice room) from the **AUTHENTICATED** turn in Redis
  (`services/scanner_jobs.py`). The event carries **no user identity** — never trust one from it.
- Unknown job → **409** (a fast-failing scan retries until the requester is recorded), **never 404**. An ownership
  refusal is final.
- The route accepts ONLY `SCANNER_INGEST_CLIENT_IDS` (fail-closed, rate-limited).

## Delivery: exactly once into the conversation
1. `…:reported` marker (24 h) — a settled job answers from Redis.
2. `SET NX` claim (60 s) is only a **LEASE**, never the delivered marker: held → 409, so a pod that died mid-write is
   re-delivered by the scanner's retry. Its value is a per-delivery token released by Lua compare-and-delete, so a
   late delivery cannot free a newer lease.
3. The message itself (`message_metadata.scanner_job.job_id`), checked under
   `pg_advisory_xact_lock(0x534A, hashtext(session_id))` right before the insert. An advisory lock, NOT the row lock:
   a brand-new conversation's row does not exist until `chat_handler` saves at turn end. SQLite test harness:
   unlocked, deliberately.
- The ws event + room announcement stay **at-most-once** (only after a successful write).
- `save_message` locks the conversation row `FOR UPDATE` so concurrent appends cannot fork onto a hidden branch.

## Message content
- Built from **fixed localised templates + the scanner's `error_code`**. NO event free text reaches the chat — it
  would be a prompt-injection channel.
- 🛑 **Der Grund gehört AUCH ins Protokoll.** Die Zustellzeile schrieb nur `(failed)`; welcher der neun
  `error_code` es war, stand danach allein in der Konversation der anfragenden Person. Am 2026-10-04 war das die
  Sackgasse: kein SSH auf den Scanner-Rechner, kein Code im Log — die Ursache wäre nur durch einen Blick in einen
  fremden Chat zu holen gewesen. `describe_failure_for_log()` schreibt ihn jetzt mit, **validiert** gegen
  `_KNOWN_ERROR_CODES` (aus den Texten abgeleitet, keine zweite Liste): unbekannt → gekürzt und markiert, nie
  roh durchgereicht. 🛑 Nur FEHLSCHLÄGE tragen einen Code — `done`, `unrouted` und `interrupted` haben eigene
  Texte und nie einen; die Menge liegt in `_STATES_WITHOUT_ERROR_CODE`, **derselben**, über die
  `render_completion_message` verzweigt. Vorher waren es zwei Weichen auf eine Menge, und die zweite war
  veraltet: ein echter `unrouted`-Auftrag hätte `error_code=<fehlt>` gemeldet, wo nie einer erwartet war.
- The outcome = an assistant message in the requesting conversation + a content-free `scan_job_finished` `/ws/user`
  event carrying the conversation `session_id` (routing key), so only the tab that wrote into it toasts
  (`utils/tabConversations.ts`, `ScanJobToast`; chat reload deferred while a turn streams; in auth-off every tab
  receives it). A voice request is ALSO spoken in its satellite's room via the `announce_in_room` hook.
- Personal proactive notifications are NOT the channel (presence-gated).

## Routing invariant
A scan enters exactly **ONE** instance and only after its destination is settled — an unrouted scan never transits
any instance's DB (rules out a hub-ingests-everything design). Targets are config, never code; `n=1` short-circuits
the whole routing layer.

## Related MCP-client shapes
- `MCPServerState.inflight_deadlines` keeps `refresh_tools` / `probe_server` from reconnecting a session under a
  running call — bounded: only a call still inside its own timeout shields the session; a skipped probe reports
  `ok: None`, never healthy.
- `call_timeout:` in `mcp_servers.yaml` takes a number or `{tool: s, default: s}` (scanner: `route_scan` /
  `retry_pending_scans` 600 s; `scan_document` needs none).
