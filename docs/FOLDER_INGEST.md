# Folder Auto-Ingest

Drop a file into a watched folder → it is ingested into the knowledge base **and**
Paperless automatically, for local, SMB, and NFS shares.

> **Status.** SHIPPED + DEPLOYED. Backend push/health/token routes, the interactive
> `internal.ingest_file` agent tool, completion-aware dedup, owner/tier filing, and
> the **async Paperless reconciler** are live; the dedicated `renfield-mcp-filesystem`
> server that watches the shares and pushes files runs as its own deployment
> (`filesystem-mcp` image). Files also still reach the backend via the interactive
> `internal.ingest_file` tool or a manual `POST`.

## Architecture

```
renfield-mcp-filesystem (dedicated, owns share access — NOT the backend)
  • watches roots, acts on a settled NEW file (create-only)
  • on a settled file → HTTP multipart PUSH to the backend (Bearer)
  • moves the file by the 4-state response: ingested|duplicate → processed/ ;
                                            retry → leave in inbox ; failed → failed/
        │  POST /api/folder-ingest/document   [multipart: file + metadata json]
        ▼
backend  services/folder_ingest.py  (shared bridge)
  persist a recovery byte copy → dedup vs the Document row → race-safe create +
  stamp paperless_state='pending' → enqueue on the Redis doc stream → respond 4-state
        ▼
  document worker (async): OCR / chunk / embed + KG / Schicht-A hooks
        ▼
  paperless_reconciler (async, periodic): files pending+completed docs into Paperless
```

**Paperless is decoupled from the request (Design Z).** The push never performs the
Paperless upload/consume round-trip — it only stamps `paperless_state='pending'` and
returns. A periodic backend reconciler (`services/paperless_reconciler.py`, mirrors
`obligation_calendar_sync`) files pending+completed docs out of band via the
already-connected MCP manager, bounded concurrency, its own short session. This
avoids holding a pooled DB connection across a multi-second external wait — the
inline leg did, and a watch-folder backlog exhausted the pool and stalled the API
(the 2026-07-01 outage). `'pending'` doubles as the provenance marker: interactive
KB uploads stay `NULL` and are never filed.

**Hard constraints:** the network shares are **never mounted into the backend** (the
MCP is the sole access boundary), and there is **no polling and no WebSocket** — the
MCP pushes over REST the instant it detects a settled new file.

## Enable it

1. Set the config (see `docs/ENVIRONMENT_VARIABLES.md` → *Folder Auto-Ingest*):

   ```bash
   FOLDER_INGEST_ENABLED=true
   FOLDER_INGEST_KB_NAME=Eingang          # target KB (auto-created)
   FOLDER_INGEST_TARGET_USER=             # owner of auto-filed docs (empty → admin/first user)
   FOLDER_INGEST_DEFAULT_TIER=0           # circle tier at create (0=self … 4=public)
   FOLDER_INGEST_TO_PAPERLESS=true
   ```

2. Mint the Bearer token (admin, `settings.manage`). The token lives in `SystemSetting`,
   not `.env`, so it is revocable without a redeploy:

   ```bash
   curl -X POST https://<host>/api/folder-ingest/token \
        -H "Authorization: Bearer <your-admin-jwt>"
   # → {"token": "…"}   # store this in the filesystem MCP's secret
   ```

3. Point the filesystem MCP at the backend (`RENFIELD_URL` + the token) and at the
   shares to watch. (MCP setup ships with that server.)

## The 4-state response contract

The push response body's `status` is load-bearing — the MCP moves the source file by
it. **`ingested` means *enqueued*, not OCR'd.** All four are HTTP 200.

| `status`    | meaning                                                        | MCP action               |
|-------------|----------------------------------------------------------------|--------------------------|
| `ingested`  | new row created + enqueued (+ stamped `paperless_state='pending'`) | move → `processed/`      |
| `duplicate` | row exists + completed (Paperless filed out of band by the reconciler) | move → `processed/`      |
| `retry`     | worker down, or row pending/processing                        | **leave in inbox**, re-push |
| `failed`    | terminal reject (bad ext, empty, oversize, malformed metadata) | move → `failed/`         |

Transport-level outcomes use status codes the MCP maps separately:

| code      | meaning                                              | MCP action                       |
|-----------|------------------------------------------------------|----------------------------------|
| `401`/`403` | missing / wrong token                              | **fatal config error** — stop, don't move |
| `503`     | feature disabled (`reason: feature_disabled`) or worker down (`reason: worker_unavailable`) | retry |

Every response (and 4-state body) carries `contract_version`; the MCP sends its own
version in the `X-Folder-Ingest-Contract` request header. A mismatch is logged as a
skew WARNING but processed leniently (the request shape is backward-compatible).

## Health handshake

The MCP pings `GET /api/folder-ingest/health` (same Bearer token) on startup and
periodically to catch config drift before it silently misroutes files:

```json
{ "enabled": true, "kb_name": "Eingang", "kb_resolved": true, "token_ok": true,
  "max_file_size_mb": 50, "allowed_extensions": ["pdf", "docx", …],
  "contract_version": "1" }
```

A wrong token → `401`/`403` (the MCP knows its token is bad = fatal). When the feature
is **disabled** health still returns `200` with `enabled: false` (definitive "feature
off" — distinct from the push route's transient `503`).

### Failure reporting (MCP self-detection)

The MCP already fires an `OPERATOR-NOTIFY` on its own failures (SMB-auth, share
down, retry-exhausted). Point `FILES_NOTIFY_WEBHOOK_URL` at
`POST /api/mcp-health/report` (same folder-ingest Bearer token via
`FILES_NOTIFY_WEBHOOK_TOKEN`) so those surface as a proactive admin alert +
`internal.system_health` entry instead of dead-ending in container logs. Backend
side is gated `MCP_HEALTH_MONITOR_ENABLED`; unset URL = legacy log-only.
See `docs/design/mcp-self-detection.md`.

## Interactive path (`internal.ingest_file`)

Besides the auto push, the agent can ingest a file the user points at:
`internal.ingest_file({path})` pulls the bytes through the filesystem MCP
(`mcp.files.read_file`, `truncate=False`) and runs them through the **same** bridge —
dedup / owner+tier / Paperless filing are identical. The asking user owns what they
ingest (falling back to `FOLDER_INGEST_TARGET_USER` in single-user mode).

## Chat maintenance tools (`internal.ingest_status` / `internal.reindex_documents`)

Two platform-owned agent tools let the household admin operate the pipeline from
the Renfield chat (both live on the `documents` + `general` roles;
`services/kb_maintenance_tool.py`):

- **`internal.ingest_status`** (read-only): "wie ist der Verarbeitungsstatus?",
  "sind alle Dokumente in Paperless?" → documents by status, count of completed
  docs with **no chunks**, worker liveness + queue depth, and the `paperless_state`
  filing breakdown (done / pending / failed / unfiled).
- **`internal.reindex_documents`** (write): "Dokumente ohne Chunks neu indexieren"
  → finds `completed` docs with 0 chunks and enqueues a `user_reindex` worker task
  (purge + rebuild) for each (batch-capped 200 / max 500; skips in-flight docs).
  **Gated on `Permission.RAG_MANAGE`** when auth is on — an authenticated
  low-privilege user is refused; auth-off / unidentified-voice turns are allowed.
- **`internal.list_chunkless_documents`** (read-only): "welche Dokumente haben keine
  Chunks?" / "nenne mir die Titel der leeren Dokumente" → lists the chunkless
  `completed` docs by name (`generated_title → title → filename`), newest first,
  capped (default 50 / max 200). The by-name complement to the count + reindex.

Router note: these processing-status/reindex questions are routed to the
`documents` agent role (not `knowledge`, which is a no-agent-loop RAG path) — see
the role descriptions in `config/agent_roles.yaml`.

## Behavior notes

- **Dedup (completion-aware).** A re-pushed file is a `duplicate` once the row is
  `completed` — Paperless filing is decoupled (the async reconciler owns it), so the
  file moves to `processed/` without waiting on filing. A previously `failed` document
  is re-ingested (`REINGEST`, with the fresh bytes). Self-heal: a re-push re-stamps
  `paperless_state='pending'` if a filing-wanted doc reached `completed` with a NULL
  state (e.g. a stamp commit lost to a crash) so the reconciler still picks it up.
- **Owner / tier.** Auto-filed documents are owned by `FOLDER_INGEST_TARGET_USER` at
  `FOLDER_INGEST_DEFAULT_TIER` (default 0 = self/private), regardless of the KB.
- **Paperless filing (async, Design Z).** The push stamps `paperless_state='pending'`
  and returns; `services/paperless_reconciler.py` (periodic, `run_at_boot`, bounded
  `PAPERLESS_RECONCILER_CONCURRENCY`) files pending+completed docs via the Paperless
  MCP — upload non-blocking, then await the consume verdict. A Paperless **duplicate**
  counts as terminal success. Filing never fails the KB ingest; a doc whose recovery
  bytes are gone is marked `paperless_state='failed'` (terminal, so it can't poison the
  batch). The filed Paperless id is persisted on `documents.paperless_document_id`
  (migration `pc20260613`). The push itself never performs the external round-trip on a
  pooled DB connection — that inline leg was the 2026-07-01 pool-exhaustion outage.
- **Paperless-id link via checksum (#1166).** The consume task frequently settles
  `success`/`duplicate` with **no `related_document`** even though Paperless created/holds
  the document, so the id came back NULL corpus-wide. `_resolve_paperless_id_by_checksum`
  falls back to a read-only Paperless `?checksum__iexact=<file_hash>` lookup — renfield's
  `documents.file_hash` equals Paperless's `checksum` (SHA256) — so the KB row still links
  to the real Paperless doc. The deferred created_date/OCR PATCH stays guarded to the
  TASK-reported id (never a checksum-resolved one, which may be a pre-existing doc).
  `internal.ingest_status` reports `paperless_done_linked`/`paperless_done_unlinked` (the
  *verified* filing count, not just `state='done'`); `bin/backfill_paperless_document_ids.py`
  (`--dry-run`/`--commit`) links pre-fix docs by checksum. Needs `PAPERLESS_API_URL` +
  `PAPERLESS_API_TOKEN` in the backend env (already provisioned for the MCP).
- **Idempotent refile (no re-upload loop).** The leg persists the Paperless consume
  `task_id` on `documents.paperless_task_id` (migration `pc20260825`) BEFORE awaiting the
  verdict; on a retry it RE-POLLS that task (`await_consume_result`) instead of
  re-uploading. So a consume that outlives the await window (a slow Paperless) settles
  from the same task next cycle rather than creating a fresh copy — the fix for a 2026-08
  re-ingest loop that reached 2289 identical copies of one file on xidra. Only
  success/duplicate/failure are terminal; a pending/transport-error re-poll keeps the
  task_id and never re-uploads. The initial (fire-and-forget) filing hook awaits the full
  `paperless_consume_timeout_s` (raised to 300s; it yields the loop, so long is free);
  the retry runs in the sequential document worker and uses a short
  `paperless_refile_poll_timeout_s` (30s) so it can't head-of-line-block ingest —
  relying on the cheap re-poll. Docs that settle via the re-poll skip the post-consume
  `created_date`/OCR patch — the date is fixable via
  `bin/backfill_paperless_metadata.py --mode created-date` (see *Backfill* below); the
  OCR content transport is not backfilled (renfield keeps no full text outside the
  chunks, and re-deriving it needs Docling in the worker).
- **Search-index self-heal (Fix B, `PAPERLESS_INDEX_CHECK_ENABLED` /
  `PAPERLESS_INDEX_HEAL_ENABLED`, both dark).** The re-ingest loop also left a Paperless
  whose full-text index held far fewer documents than its database — documents existed
  but full-text search could not find them. Paperless has **no REST reindex**: a full
  rebuild is only the `document_index reindex` management command on the Paperless
  host, `POST /api/tasks/run/` accepts only `train_classifier`/`sanity_check`, and
  `/api/status/` `index_status` only says whether the index can be *opened*. A document
  PATCH, however, re-indexes that one document. The built-in scheduled task
  **"Paperless-Suchindex prüfen"** (`paperless_index_health`, hourly) calls
  `mcp.paperless.search_index_health` (**requires renfield-mcp-paperless ≥ 1.13.0**; an
  older MCP makes the task raise "nicht verfügbar"): one page of document ids from the
  **database** (index-independent, newest first) is probed id by id against the index
  (`query=id:<n>`), skipping docs added in the last `PAPERLESS_INDEX_CHECK_MIN_AGE_SECONDS`.
  A miss only counts once the probe is **proven** to work on this Paperless — a document
  on the same page was found, OR a **positive control** (a recent document from page 1)
  was found in the same call, OR an earlier run proved it (remembered in Redis for 7
  days). The control and the remembered proof are what make check mode see an index that
  lost its OLD documents: there, every page below the boundary is entirely missing and
  contains no same-page hit. Verdicts:
  - `degraded` — proven misses. With healing on, each missing doc gets an **empty partial
    update** and is re-probed, at most `PAPERLESS_INDEX_HEAL_MAX_TOUCH` per run.
    paperless-ngx re-indexes on every document update, so no field is sent: nothing can be
    overwritten (not even a title edited meanwhile), nothing is deleted or re-OCR'd.
  - `inconclusive` — misses with an unproven probe, a probe Paperless rejects, or a sample
    cut short. Only logged; with healing on, **one** canary doc is re-saved, and only if it
    then appears is the verdict upgraded to `degraded`.
  - `index_error` — Paperless reports the index cannot be opened; a re-save cannot fix
    that, the operator must run `document_index reindex`.

  **A heal is not free of side effects.** Every document update — including the empty one
  — bumps `modified` and fires Paperless's `document_updated` signal, which runs **every
  enabled workflow with a "Document Updated" trigger** once per healed document. Such a
  workflow may assign tags, owner or permissions, send e-mail or call a webhook. So the
  MCP reads `/api/workflows/` before touching anything and **refuses to heal** while such
  workflows are enabled (or when the list cannot be read — fail closed); the task then
  raises "Selbstheilung blockiert". Set `PAPERLESS_INDEX_HEAL_ALLOW_WORKFLOWS=true` only
  after checking that those workflows are harmless when run for already-filed documents.

  **Documents that cannot be healed.** A document that is still missing after its re-save
  counts one failed attempt (Redis ledger, 30-day TTL). Below
  `PAPERLESS_INDEX_HEAL_MAX_ATTEMPTS` the task raises "wirkungslos" and re-checks the page
  next run. At the limit the document is **given up**: it is never re-saved again, it is
  reported in a direct, rate-limited `ops_alert` ("nicht heilbar", listing the Paperless
  ids), and the walk moves on — one unindexable document can no longer freeze the walk or
  make its neighbours be re-saved every hour. A document that is later found in the index
  leaves the ledger again. A document deleted between listing and re-save is **skipped**,
  not counted as a failed heal.

  Alerting otherwise follows the watchdog pattern: the task **raises** on a proven
  degradation with healing off, on a blocked heal, on an ineffective heal with attempts
  left, and on `index_error`, and in those cases keeps its Redis page cursor on the same
  page — so the next run re-checks it and the scheduled-task engine's failure streak turns
  it into one `ops_alert` to the owner admin. A handled page, an `inconclusive` or
  `healthy` page advance the cursor (wrapping at the end of the archive); a page with
  unfinished work (touch or time budget) is kept without raising. Rollout: enable the
  check first (detect + alert), review Paperless workflows, then heal.
- **Correspondent auto-create (Option A + guardrail).** Metadata extraction (`services/
  paperless_metadata_extractor.py`) only matches a correspondent against the *recency-
  pruned* taxonomy window, so a new sender would otherwise be filed blank. The leg now
  resolves-or-creates via `resolve_correspondent_from_metadata` →
  `resolve_or_create_correspondent`: it re-checks the extracted sender against the
  **full** correspondent list — a strong fuzzy match reuses the existing entry (recovers
  a pruned-window miss, never duplicates); a loose fuzzy-near match is left unset
  (the "no fuzzy-near existing match" guardrail); a genuinely-new sender is **created**
  and assigned. Document-type and tags stay existing-match-only (no auto-create). Note:
  the Paperless MCP's name→id resolver does bidirectional *substring* matching, so a
  containment match (e.g. "Telekom" ⊂ "Telekom Deutschland GmbH") is reused rather than
  duplicated — intended, and correct for recurring senders.
- **Document date (Ausstellungsdatum).** The extracted `created_date` is submitted on
  the (non-blocking) upload **and reapplied post-consume** via `update_document` once the
  consume task yields a document id — Paperless can't set `created` before the doc exists,
  so with `wait_for_consume=False` the MCP hands it back in `deferred_patch` for the caller
  to apply (mirrors the chat-upload `_finalize_paperless_commit`; the reapply is merged into
  the same post-consume PATCH that transports Renfield's OCR content). Without the reapply
  Paperless kept the consume-time date while the OCR-derived **title** showed the correct one
  (the pre-2026-07 Jet-receipt date drift, fixed). No extracted date → left unset (Paperless
  default).
- **Backfill.** `bin/backfill_paperless_metadata.py --mode {created-date,correspondent}`
  — every run is a dry run unless `--commit` is given; `--mode` is required.
  - `--mode created-date` repairs the Paperless `created` date of filed docs whose
    post-consume PATCH never ran (the task_id re-poll / checksum-resolved settles). No
    column records which path settled a doc, so it targets the *symptom*: source is
    `documents.document_date`; only `paperless_state='done'` docs with a linked
    `paperless_document_id`; PATCHes **only** where Paperless `created` still equals its
    `added` date (the consume-date fallback — any other value may be a human edit and is
    left alone); a Paperless doc linked from KB rows that disagree on the date is skipped.
    Idempotent, paced below the 60/min MCP limit with backoff on a rejection, capped
    (`--limit`, default 200, max 1000) and resumable (`--after-pid` = the printed
    `last_pid`); prints counts and Paperless ids only. Core:
    `services/paperless_metadata_backfill.py`.
  - `--mode correspondent` gap-fills the correspondent on already-filed folder-ingest
    docs that lack one (the Docling-outage + new-sender cohorts): it re-extracts, runs
    the same resolve-or-create path, and PATCHes via `update_document`.
    Correspondent-only (never touches title/type/tags), locates the Paperless doc by the
    stored id else a filename match over recently-added docs, and skips any doc that
    already has a correspondent.

  Neither is in the backend image (the build context is `src/backend`): copy the script
  into the backend pod (any path, e.g. `kubectl cp bin/backfill_paperless_metadata.py
  <ns>/<backend-pod>:/tmp/`) and run `python /tmp/backfill_paperless_metadata.py --mode …`
  — no `PYTHONPATH`, any cwd. Every `bin/backfill_*.py` finds the backend itself:
  `$RENFIELD_BACKEND_DIR` if set (exclusive), else the repo layout `bin/../src/backend`,
  else the image layout `/app`; if none holds the backend it exits 2 with the paths it
  tried. The metadata backfill starts only the `paperless` MCP server, bounds every
  teardown step (MCP shutdown, leftover transport tasks, executor) so the process ends
  after the summary, and exits 0 on success / 1 on an error — including a Paperless MCP
  that is not configured or does not connect (previously every document silently showed
  as `unreachable`) and any failed PATCH in created-date mode.

## Processed-file rename (#881, `FOLDER_INGEST_RENAME_PROCESSED_ENABLED`, dark)

The MCP moves the source file to `processed/` under its **original** filename at
push time — but the human-readable `documents.generated_title` (issuer + type +
date) does not exist yet; it's synthesized later in the worker, after the
Schicht-A facts commit. So a **decoupled post-ingest step** renames the already-moved
file once the title is known.

- After the worker sets `generated_title` (`schicht_a_extractor.post_document_ingest`),
  `services/folder_ingest_rename.py::rename_processed_to_title` — gated on
  `FOLDER_INGEST_RENAME_PROCESSED_ENABLED` **+** `source == 'folder_ingest'` **+** a
  non-empty title — calls the filesystem MCP `mcp.files.rename_processed(original_name,
  new_base)`. The worker uses a lazy single-server (`files`) client
  (`services/files_worker_client.py`), mirroring the Paperless worker client.
- The MCP tool renames `processed/<original_name>` → `processed/<sanitized title><ext>`:
  **idempotent** (missing source → success no-op), **collision-safe** (` (2)`, ` (3)`
  suffix), **traversal-safe** (`original_name` must be a bare filename; separators are
  scrubbed from `new_base`), keeping the original extension.
- **Best-effort:** a rename failure (MCP down, tool absent, file gone) is swallowed +
  logged — the ingest never breaks. It does NOT touch the 4-state move contract or the
  `paperless_state` dedup (a purely post-move step). **Rollout:** deploy the MCP tool
  first, then the backend, then flip the flag (dark until both are live).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| MCP logs `401`/`403` on every push | token missing / wrong | re-mint via `POST /api/folder-ingest/token`, update the MCP secret |
| Nothing ingests, push gets `503 feature_disabled` | `FOLDER_INGEST_ENABLED=false` | enable the flag + restart the backend |
| Push gets `503 worker_unavailable` | document worker pod down | check the worker pod; it self-heals when back (the file stays in the inbox) |
| File lands in `failed/` | bad extension / empty / oversize / malformed metadata | check `ALLOWED_EXTENSIONS` + `MAX_FILE_SIZE_MB`; inspect the file |
| Document is in the KB but **not** in Paperless | a transient Paperless outage during the first ingest (known gap, P2) | the file already moved to `processed/`, so it is not auto-retried — re-push it, or it surfaces in Paperless's own failed-task log; a future reconciler will re-file `paperless_state != done` docs |
| Paperless `created` (Ausstellungsdatum) is the ingest date, not the document's | pre-fix: the extracted date was submitted on upload but never reapplied post-consume | fixed 2026-07 (submit + post-consume `deferred_patch` reapply); correct already-filed docs via the ADMIN Paperless audit flow (`/api/admin/paperless-audit`, it PATCHes `created`) |
| Document exists in Paperless but its full-text search does not find it | the Paperless search index is incomplete (re-ingest loop aftermath) | enable `PAPERLESS_INDEX_CHECK_ENABLED` (detect + alert), then `PAPERLESS_INDEX_HEAL_ENABLED` (per-document re-save); an `index_error` alert needs `document_index reindex` on the Paperless host |
| Document re-fails on every worker restart | poison document (terminal pipeline error) | the worker marks it `status=failed` + acks (it stops looping); fix or remove the file, then re-push |

## Where it lives

- Bridge: `services/folder_ingest.py` (dedup, 4-state, owner/tier, token helpers, resolvers)
- Paperless leg: `services/folder_ingest_paperless.py`
- Processed-file rename (#881): `services/folder_ingest_rename.py` + `services/files_worker_client.py` + the `rename_processed` tool in `renfield-mcp-filesystem`
- Routes: `api/routes/folder_ingest.py` (`POST /document`, `GET /health`, `POST /token`)
- Interactive tool: `services/folder_ingest_tool.py` (+ dispatch in `services/action_executor.py`)
- Worker terminal-failure handling: `workers/document_processor_worker.py`
- Metadata backfill (created-date / correspondent): `bin/backfill_paperless_metadata.py` + `services/paperless_metadata_backfill.py`
- Search-index self-heal: `services/paperless_index_health.py` (built-in `paperless_index_health`) + `search_index_health` in `renfield-mcp-paperless`
- Paperless MCP consume-poll: `renfield-mcp-paperless` `await_consume_result` (v1.8.0+)
