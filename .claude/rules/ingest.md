---
paths:
  - "src/backend/services/folder_ingest*.py"
  - "src/backend/services/paperless_reconciler.py"
  - "src/backend/services/folder_ingest_paperless.py"
  - "src/backend/services/email_ingest*"
  - "src/backend/api/routes/email_ingest*.py"
  - "src/backend/api/routes/folder_ingest*.py"
---
# Folder + email auto-ingest, Paperless filing

Loaded only when a folder/email-ingest or Paperless-filing file is read.
Long form: `docs/FOLDER_INGEST.md`, `docs/EMAIL_INGEST.md`.

## Shape (both producers)
- The MCP **PUSHES** over REST: filesystem MCP → `POST /api/folder-ingest/document` (`FOLDER_INGEST_ENABLED`),
  `renfield-mcp-email-ingest` → `POST /api/email-ingest/document` (`EMAIL_INGEST_ENABLED`). **No backend mounts, no
  polling** (mail is detected via IMAP IDLE). SMB/local and IMAP credentials live in the dedicated MCP deployments,
  never in the backend.
- Both routes converge on the `folder_ingest.ingest_document` bridge (dedup / owner+tier / Paperless / enqueue). The
  scanner, PDF-split children, meeting transcripts and `internal.ingest_file` re-enter through the same seam — do not
  add a second ingest path.
- The push route uses the higher `API_RATE_LIMIT_INGEST`. The filesystem MCP bounds its own fan-out
  (`FILES_MAX_CONCURRENT_PUSHES`) and re-reconciles on backend recovery (`FILES_HEALTH_POLL_SECONDS`).

## Paperless filing is decoupled (Design Z)
- The push only stamps `documents.paperless_state='pending'` and returns; `services/paperless_reconciler.py` files
  pending+completed docs out of band. **The push NEVER awaits the Paperless round-trip on a pooled DB connection** —
  a watch-folder backlog exhausts the pool that way.
- **The filing leg is idempotent.** The Paperless consume `task_id` is persisted on `documents.paperless_task_id`
  (migration `pc20260825`) **BEFORE** the await; a retry **RE-POLLS** that task (`await_consume_result`) and never
  re-uploads — a consume that outlives the await window must not create a duplicate copy.
- Only success / duplicate / failure are terminal. A pending/error re-poll keeps the `task_id` and never re-uploads.
  Never clear a live `pending` task_id.
- Two timeouts, not interchangeable: the initial fire-and-forget hook keeps the full `paperless_consume_timeout_s`
  (300 s); the sequential-worker refile uses the short `paperless_refile_poll_timeout_s` (30 s) so it cannot
  head-of-line-block ingest.
- Re-polled docs skip the post-consume `created_date` PATCH → repaired by
  `bin/backfill_paperless_metadata.py --mode created-date` (dry run unless `--commit`; source
  `documents.document_date`; patches ONLY where Paperless `created` still equals `added`; paced below the MCP's
  60/min, capped, resumable via `--after-pid`; prints counts/ids only).

## Email sphere routing is SERVER-authoritative
- The backend maps `mailbox_id` → owner/tier/kb. Never accept owner, tier or KB from the push payload — a leaked push
  token must not be able to escalate tier.
- Watcher (separate repo): two IMAP connections (IDLE + command); on a disconnect reset **BOTH** (`v0.1.1`+) —
  resetting only the IDLE one wedges it after a server `BYE timeout` and mail detection stops silently.
