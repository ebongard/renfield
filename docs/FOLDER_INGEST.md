# Folder Auto-Ingest

Drop a file into a watched folder → it is ingested into the knowledge base **and**
Paperless automatically, for local, SMB, and NFS shares.

> **Status.** The **backend** side (this document) is complete: the push endpoint,
> the health/token routes, the interactive `internal.ingest_file` agent tool, the
> completion+Paperless-aware dedup, owner/tier filing, and the Paperless leg. The
> **dedicated `renfield-mcp-filesystem` server** that watches the folders and pushes
> files is a separate deployment that is **not built yet** — until it exists, files
> reach the backend only via the interactive `internal.ingest_file` tool (the agent
> pulling a file through any filesystem MCP that exposes `read_file`) or a manual
> `POST` to the endpoint below.

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
  enqueue on the Redis doc stream → Paperless leg → respond 4-state
        ▼
  document worker (async): OCR / chunk / embed + KG / Schicht-A hooks
```

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
| `ingested`  | new row created + enqueued                                     | move → `processed/`      |
| `duplicate` | row exists, completed, Paperless leg settled                   | move → `processed/`      |
| `retry`     | worker down, or row pending/processing, or Paperless unsettled | **leave in inbox**, re-push |
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

## Interactive path (`internal.ingest_file`)

Besides the auto push, the agent can ingest a file the user points at:
`internal.ingest_file({path})` pulls the bytes through the filesystem MCP
(`mcp.files.read_file`, `truncate=False`) and runs them through the **same** bridge —
dedup / owner+tier / Paperless filing are identical. The asking user owns what they
ingest (falling back to `FOLDER_INGEST_TARGET_USER` in single-user mode).

## Behavior notes

- **Dedup (completion + Paperless aware).** A re-pushed file is only a `duplicate` when
  the row is `completed` **and** the Paperless leg is settled. A previously `failed`
  document is re-ingested; a `completed`-but-Paperless-missing document re-runs **only**
  the Paperless leg.
- **Owner / tier.** Auto-filed documents are owned by `FOLDER_INGEST_TARGET_USER` at
  `FOLDER_INGEST_DEFAULT_TIER` (default 0 = self/private), regardless of the KB.
- **Paperless leg.** Uploads non-blocking, then awaits the consume verdict via the
  Paperless MCP. A Paperless **duplicate** counts as terminal success (the document is
  already there). Paperless filing never fails the KB ingest. The filed Paperless
  document id is persisted on `documents.paperless_document_id` (migration `pc20260613`).
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
- **Backfill.** `bin/backfill_paperless_metadata.py` (`--dry-run`/`--commit`) gap-fills
  the correspondent on already-filed folder-ingest docs that lack one (the Docling-outage
  + new-sender cohorts): it re-extracts, runs the same resolve-or-create path, and
  PATCHes via `update_document`. Correspondent-only (never touches title/type/tags),
  locates the Paperless doc by the stored id else a filename match over recently-added
  docs, and skips any doc that already has a correspondent.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| MCP logs `401`/`403` on every push | token missing / wrong | re-mint via `POST /api/folder-ingest/token`, update the MCP secret |
| Nothing ingests, push gets `503 feature_disabled` | `FOLDER_INGEST_ENABLED=false` | enable the flag + restart the backend |
| Push gets `503 worker_unavailable` | document worker pod down | check the worker pod; it self-heals when back (the file stays in the inbox) |
| File lands in `failed/` | bad extension / empty / oversize / malformed metadata | check `ALLOWED_EXTENSIONS` + `MAX_FILE_SIZE_MB`; inspect the file |
| Document is in the KB but **not** in Paperless | a transient Paperless outage during the first ingest (known gap, P2) | the file already moved to `processed/`, so it is not auto-retried — re-push it, or it surfaces in Paperless's own failed-task log; a future reconciler will re-file `paperless_state != done` docs |
| Document re-fails on every worker restart | poison document (terminal pipeline error) | the worker marks it `status=failed` + acks (it stops looping); fix or remove the file, then re-push |

## Where it lives

- Bridge: `services/folder_ingest.py` (dedup, 4-state, owner/tier, token helpers, resolvers)
- Paperless leg: `services/folder_ingest_paperless.py`
- Routes: `api/routes/folder_ingest.py` (`POST /document`, `GET /health`, `POST /token`)
- Interactive tool: `services/folder_ingest_tool.py` (+ dispatch in `services/action_executor.py`)
- Worker terminal-failure handling: `workers/document_processor_worker.py`
- Correspondent backfill: `bin/backfill_paperless_metadata.py`
- Paperless MCP consume-poll: `renfield-mcp-paperless` `await_consume_result` (v1.8.0+)
