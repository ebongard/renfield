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
  `created_date`/OCR patch (fixable via `bin/backfill_paperless_metadata.py`).
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
- **Backfill.** `bin/backfill_paperless_metadata.py` (`--dry-run`/`--commit`) gap-fills
  the correspondent on already-filed folder-ingest docs that lack one (the Docling-outage
  + new-sender cohorts): it re-extracts, runs the same resolve-or-create path, and
  PATCHes via `update_document`. Correspondent-only (never touches title/type/tags),
  locates the Paperless doc by the stored id else a filename match over recently-added
  docs, and skips any doc that already has a correspondent.

## Simba review queue (xidra-only, `FOLDER_INGEST_SIMBA_ENABLED`, dark by default)

On xidra a watch-folder PDF should also reach the **Simba tax portal** — but the
upload to the tax accountant is **irreversible** (the portal forbids withdrawal),
so it is **NEVER auto-uploaded**. Instead the document-worker's post-ingest hook
files a **review proposal** the owner confirms by hand.

- **Hook** (`services/simba_ingest_review.py::simba_ingest_post_hook`, a
  `post_document_ingest` consumer registered alongside knowledge_graph / schicht_a
  / paperless_filing): gated on `FOLDER_INGEST_SIMBA_ENABLED` **+** `source ==
  'folder_ingest'` **+** a `.pdf` filename (the `documents.source` tag is set by
  the folder-ingest push, so only watch-folder PDFs qualify — chat uploads,
  meeting transcripts, PDF-split children etc. are excluded). It classifies the
  content against a stable built-in taxonomy (`KNOWN_SIMBA_TAXONOMY`, so the
  worker needs no simba MCP client) and inserts a **PENDING** `simba_ingest_proposals`
  row carrying a category/type **suggestion**. Best-effort: it never affects the
  KB/Paperless legs, and a benign concurrent-pending race (partial-unique
  `uq_simba_ingest_proposals_pending_doc`) is swallowed at INFO while any other
  insert failure is logged WARNING (a silent drop = the PDF never surfaces for
  review).
- **Review** on `/brain/review` (`SimbaIngestReviewSection`, gated on the
  `simba_ingest_review_enabled` feature flag = `FOLDER_INGEST_SIMBA_ENABLED`):
  the owner sees each pending proposal with the category/type **and Bezeichnung
  (description)** prefilled, can **edit** them, then **Confirm** (→ the real
  upload) or **Reject**.
- **Bezeichnung (description).** The Simba per-file `description` was empty when a
  document was pushed via /brain/review (the review flow had no description). It
  is now derived from the document title (`generated_title` → `title` → filename
  stem), **sanitized** to the portal's allowed charset (mirrors the simba MCP
  `DEFAULT_TEXT_PATTERN`: letters+digits+umlauts+space `. _ -`, cap 100) and shown
  as an **editable** field prefilled with that suggestion. The MCP *validates and
  throws* on a bad description (it does not sanitize), so an un-sanitized title
  with an em-dash/comma/slash would break the upload — hence the renfield-side
  sanitize. A user-edited value wins (also sanitized); blank or all-disallowed
  falls back to the derived title (`_bezeichnung`/`_sanitize_desc`).
- **Routes** (`api/routes/simba_ingest.py`, all **required-auth** when auth is on —
  the actions can trigger an irreversible upload, so never reachable anonymously):
  `GET /api/simba-ingest` (pending, owner-scoped — a proposal is visible only to
  its owner, or to an admin for an ownerless one; each carries a
  `suggested_description`), `POST …/{id}/confirm` `{category,type,description?}`,
  `POST …/{id}/reject`.
- **No double-upload.** `confirm()` is **claim-before-act**: a conditional
  `PENDING → UPLOADING` UPDATE claims the row *before* the irreversible
  `mcp.simba.upload_documents` (`dry_run:false, confirm:true`), so two concurrent
  confirms (double-click / retry / two tabs) can't both upload — the loser 409s.
  The upload uses `truncate=False` (a truncated MCP envelope would misread a
  landed upload as failed → a retry that double-uploads); the proposal is marked
  `UPLOADED` only when the document genuinely landed (`uebertragen>0`, no
  failures), reverts to `PENDING` on any non-landed outcome (retryable), and a
  row stuck in `UPLOADING` (process died mid-upload) is the fail-safe direction —
  it never auto-re-uploads.
- **Related:** the interactive chat path uses the two-tool human-gated bridge
  `internal.forward_attachment_to_simba` + `internal.simba_commit_upload` (see
  `CLAUDE.md`); this review queue is the folder-ingest analogue — same irreversible-
  upload discipline, owner-confirmed.

Model + migration: `SimbaIngestProposal` / `simba_ingest_proposals`
(`pc20260828b_simba_ingest`).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| MCP logs `401`/`403` on every push | token missing / wrong | re-mint via `POST /api/folder-ingest/token`, update the MCP secret |
| Simba review section empty / absent on `/brain/review` | `FOLDER_INGEST_SIMBA_ENABLED=false`, or the simba MCP isn't configured | enable the flag (xidra) + restart backend+worker; PDFs ingested while off produce no proposal |
| A watch-folder PDF never appears as a Simba proposal | not a `.pdf`, or `source != 'folder_ingest'` (chat/meeting/split docs are excluded by design), or the worker's insert failed | check the worker log for `simba-ingest: proposal insert FAILED` (WARNING); re-push the file |
| A proposal is stuck in `uploading` | the backend died mid-upload (fail-safe: never auto-re-uploads) | verify in Simba whether it landed; resolve the row by hand — do NOT blindly re-confirm |
| Nothing ingests, push gets `503 feature_disabled` | `FOLDER_INGEST_ENABLED=false` | enable the flag + restart the backend |
| Push gets `503 worker_unavailable` | document worker pod down | check the worker pod; it self-heals when back (the file stays in the inbox) |
| File lands in `failed/` | bad extension / empty / oversize / malformed metadata | check `ALLOWED_EXTENSIONS` + `MAX_FILE_SIZE_MB`; inspect the file |
| Document is in the KB but **not** in Paperless | a transient Paperless outage during the first ingest (known gap, P2) | the file already moved to `processed/`, so it is not auto-retried — re-push it, or it surfaces in Paperless's own failed-task log; a future reconciler will re-file `paperless_state != done` docs |
| Paperless `created` (Ausstellungsdatum) is the ingest date, not the document's | pre-fix: the extracted date was submitted on upload but never reapplied post-consume | fixed 2026-07 (submit + post-consume `deferred_patch` reapply); correct already-filed docs via the ADMIN Paperless audit flow (`/api/admin/paperless-audit`, it PATCHes `created`) |
| Document re-fails on every worker restart | poison document (terminal pipeline error) | the worker marks it `status=failed` + acks (it stops looping); fix or remove the file, then re-push |

## Where it lives

- Bridge: `services/folder_ingest.py` (dedup, 4-state, owner/tier, token helpers, resolvers)
- Paperless leg: `services/folder_ingest_paperless.py`
- Simba review (xidra): `services/simba_ingest_review.py` (hook + list/reject/confirm) + `api/routes/simba_ingest.py` + frontend `SimbaIngestReviewSection`
- Routes: `api/routes/folder_ingest.py` (`POST /document`, `GET /health`, `POST /token`)
- Interactive tool: `services/folder_ingest_tool.py` (+ dispatch in `services/action_executor.py`)
- Worker terminal-failure handling: `workers/document_processor_worker.py`
- Correspondent backfill: `bin/backfill_paperless_metadata.py`
- Paperless MCP consume-poll: `renfield-mcp-paperless` `await_consume_result` (v1.8.0+)
