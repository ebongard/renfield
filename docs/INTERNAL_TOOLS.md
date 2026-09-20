# Platform-owned internal agent tools

The agent loop sees a mix of MCP tools (`mcp.<server>.<tool>`) and `internal.*` tools. Internal tools are
platform-level wrappers that bundle multi-step workflows or chain MCP calls with real server-side state.

This page is the reference for the platform-core tools (moved out of `CLAUDE.md` on 2026-09-20); further
`internal.*` tools live in `ha_glue` (`src/backend/ha_glue/services/internal_tools.py`). The editing rules for this
code are in `.claude/rules/internal-tools.md`.

Related docs: `docs/FEATURES.md` (user-facing description, incl. "Duplikate selbst finden und löschen
(`internal.paperless_dedupe`)" and "Duplikat-Erkennung"), `docs/FOLDER_INGEST.md` (`internal.ingest_file`),
`docs/design/scheduled-tasks.md` (the autonomous dedupe built-ins), `docs/MESSAGE_RELAY.md` (the announce tools),
`docs/ENVIRONMENT_VARIABLES.md` (all flags named here).

## Index

Source paths are relative to `src/backend/`.

| Tool | One-line purpose | Source | Gate (permission / flag) |
|---|---|---|---|
| `internal.knowledge_search` | RAG chunk search PLUS circle-filtered Schicht A facts; returns provenance `data.sources` | `services/knowledge_tool.py` | circle filter (asker `user_id`) |
| `internal.list_my_memories` | Enumerate the asker's own memories without the vector threshold | `services/memory_list_tool.py` | own memories only |
| `internal.create_reminder` | Create a time-based reminder | `services/reminder_tool.py` | re-asserts `settings.proactive_reminders_enabled` (H4) |
| `internal.forward_attachment_to_paperless` | Forward a chat attachment to Paperless from server-stored bytes | `services/chat_upload_tool.py` | — |
| `internal.ingest_file` | Interactive folder-ingest of a file on a watched share | `services/folder_ingest_tool.py` | `FOLDER_INGEST_ENABLED` |
| `internal.render_table` / `internal.render_list` | Gen-UI: render an answer as a typed `table`/`list` widget | `services/widget_tools.py` | `ARTIFACTS_TYPED_ENABLED` (widget emit) |
| `internal.weather_widget` | Gen-UI: weather MCP → `weather` artifact | `services/widget_tools.py` | `WEATHER_ENABLED` |
| `internal.device_controls` | Gen-UI INTERACTIVE: `device_control` widget from fresh HA states | `ha_glue/services/internal_tools.py` | interactions gated on `HA_CONTROL` |
| `internal.presence_map` | Gen-UI read-only: present users by room | `ha_glue/services/internal_tools.py` | — |
| `internal.ingest_status` | Read-only ingest pipeline status | `services/kb_maintenance_tool.py` | — |
| `internal.reindex_documents` | Re-enqueue completed docs with no searchable chunk | `services/kb_maintenance_tool.py` | `Permission.RAG_MANAGE` |
| `internal.list_chunkless_documents` | List chunkless docs by name, REPAIRABLE vs UNINDEXABLE | `services/kb_maintenance_tool.py` | — |
| `internal.list_unfiled_documents` | Paperless filing status by name | `services/kb_maintenance_tool.py` | owner-scoped when auth is on |
| `internal.refile_to_paperless` | Re-file `failed` documents to Paperless | `services/kb_maintenance_tool.py` | `Permission.RAG_MANAGE` |
| `internal.paperless_dedupe` | Destructive: find + delete duplicate Paperless documents | `services/paperless_dedupe_tool.py` | `Permission.ADMIN` |
| `internal.find_duplicate_documents` | KB near-duplicate detection, propose-only | `services/document_dedupe_tool.py` | `DOCUMENT_DEDUPE_ENABLED` (dark) |

`AUTH_ENABLED=false` skips every permission gate, and with auth on an authenticated low-privilege user is always
refused. An UNIDENTIFIED turn (`user_permissions=None` — a device/satellite token or an unrecognized voice) is handled
per tool: the `RAG_MANAGE` maintenance tools and the `HA_CONTROL` announce tools ALLOW it (so spoken commands keep
working), while `internal.paperless_dedupe` (`ADMIN`, bulk archive delete) and the `device_action` frame DENY it.
(The former CLAUDE.md claimed "`None` allowed" for `paperless_dedupe` too; the code — `paperless_dedupe_tool.py`,
"an unidentified turn is DENIED" — says otherwise, and the code is right.)

## Tools

### `internal.knowledge_search`

Source: `services/knowledge_tool.py`

- Semantic RAG search over the user's knowledge base (chunks) **PLUS circle-filtered Schicht A document facts**.
- When `schicht_a_extraction_enabled`, `DocumentFactRetrieval` runs alongside `rag.search` and folds precise
  extracted values (Steuernummer/IBAN/issuer/obligation Fristen) into a `FAKTEN` block in `data.context` plus a
  `data.facts` list, so "what's my Steuernummer" answers from the normalized fact, not the chunk.
- Fact-source-document titles are **separately** circle-filtered (`_visible_document_meta`), so a
  tier-overridden-public fact on a private document never leaks that document's title/filename; a non-visible source
  falls back to a generic `Dokument {id}` label with no chip.
- Flag-off = chunk-only (byte-identical context/message).
- Also returns structured `data.sources` (deduped per document) that `chat_handler._extract_agent_sources` surfaces
  as chat **provenance chips**.

### `internal.list_my_memories`

Source: `services/memory_list_tool.py`

- Enumerates the asker's own conversation memories (preferences/facts/instructions) WITHOUT the per-turn
  `{memory_context}` vector threshold.
- Backs broad self-knowledge queries ("Was weißt du über mich?") the small auto-injected snapshot can't answer.
- Reads only the authenticated user's own memories.

### `internal.create_reminder`

Source: `services/reminder_tool.py`

- Creates a time-based reminder ("erinnere mich in 30 Minuten daran …", "remind me at 18:00") that the reminder
  checker fires as a proactive notification.
- Thin wrapper over `ReminderService.create_reminder`; the authenticated `user_id`/`session_id` are injected (never
  from LLM params).
- **Re-asserts `settings.proactive_reminders_enabled` in-handler** (H4 — refuses rather than persist a row the
  checker will never fire).
- Closes the #1146 gap where the chat agent had no create path and hallucinated a confirmation (the only prior caller
  was `POST /api/reminders`).
- Routed via the `general` role.

### `internal.forward_attachment_to_paperless`

Source: `services/chat_upload_tool.py`

- Forwards a chat-attached file to Paperless using real server-stored bytes — prevents the LLM from handling base64
  payloads it can't actually see.

### `internal.ingest_file`

Source: `services/folder_ingest_tool.py`

- Interactive folder-ingest: the agent points at a file `path` on a watched share.
- Pulls the bytes through the filesystem MCP (`mcp.files.read_file`, `truncate=False` — no 128 KB cap corruption) and
  runs them through the same `folder_ingest.ingest_document` bridge as the REST push (dedup / owner+tier / Paperless
  filing identical — the async reconciler files it).
- Gated by `FOLDER_INGEST_ENABLED`. See `docs/FOLDER_INGEST.md`.

### `internal.render_table` / `internal.render_list`

Source: `services/widget_tools.py`

- **Gen-UI** (item 10): the agent renders any answer (incl. RAG-backed) as a typed Lane-A `table`/`list` widget.
- It passes the structured `columns`/`rows` or `items` it computed; the tool validates them via
  `artifact_service.validate_artifact` (typed JSON from the agent, NEVER free-text parsing) and returns
  `data={"artifacts":[…]}`.

### `internal.weather_widget`

Source: `services/widget_tools.py`

- **Gen-UI**: wraps `mcp.weather.get_weather` (Open-Meteo MCP, `WEATHER_ENABLED`) and maps it to the `weather`
  artifact (current conditions + a daily forecast).
- Returns the raw data too so the agent can add a one-line spoken summary.

### `internal.device_controls`

Source: `ha_glue/services/internal_tools.py`

- **Gen-UI INTERACTIVE**: reads fresh HA `get_states()`, filters to controllable light/switch/scene/climate (optional
  `room`), emits a `device_control` widget of toggles + brightness slider + scene-buttons + thermostat stepper.
- Interactions are NOT agent-mediated — a `device_action` WS frame gated on `HA_CONTROL` → `internal.device_action`
  (in `ha_glue`, `_HANDLERS`-only / not agent-advertised; allowlist incl. set_brightness/set_temperature with value
  clamping).

### `internal.presence_map`

Source: `ha_glue/services/internal_tools.py`

- **Gen-UI read-only**: groups currently-present users by room (presence service) → a `presence_map` widget.

### `internal.ingest_status`

Source: `services/kb_maintenance_tool.py`

- **Read-only pipeline status**: documents by status (pending/processing/completed/failed).
- Count of completed docs with **no searchable (embedded) chunk** — zero chunks OR only unembedded `parent` chunks
  (`_searchable_chunk_subquery`) — **split into `chunkless_reindexable` vs `chunkless_unindexable`**.
- Worker liveness + Redis queue depth, and the `paperless_state` filing breakdown (done/pending/failed/unfiled).
- Backs "wie ist der Verarbeitungsstatus?" / "sind alle Dokumente in Paperless?".

### `internal.reindex_documents`

Source: `services/kb_maintenance_tool.py`

- **Write/maintenance**: finds `completed` documents with **no searchable (embedded) chunk** (zero chunks OR only
  unembedded `parent` chunks — the 2026-07 parent-only blind spot) and enqueues a `user_reindex` worker task (purge +
  rebuild) for each.
- Same path as `POST /api/knowledge/documents/{id}/reindex`, batch-capped (default 200 / max 500), skips in-flight
  docs.
- **Skips genuinely-unindexable docs by default**, classified via `_unindexable_exists()` over
  `document_processing_history`: a completed run that produced 0 usable chunks AND either dropped everything at the
  quality gate or was an already-retried re-derivation. The `chunks_produced=0` guard keeps a doc that once produced
  chunks but lost them out-of-band REPAIRABLE.
- Reports `skipped_unindexable`; `force=true` reindexes them anyway.
- Filters unindexable in SQL so the cap applies to repairable docs (no cap-starvation).
- **Gated on `Permission.RAG_MANAGE`** when auth is on (auth-off / `user_permissions=None` allowed; an authenticated
  low-privilege user is refused).
- Backs "Dokumente ohne Chunks neu indexieren".

### `internal.list_chunkless_documents`

Source: `services/kb_maintenance_tool.py`

- **Read-only**: lists the `completed` documents with **no searchable (embedded) chunk** (zero-chunk OR parent-only)
  **by name** (`generated_title → title → filename`), newest first, capped (default 50 / max 200).
- Each is **labelled REPAIRABLE vs UNINDEXABLE** (grouped in the output, same `_unindexable_exists()`
  classification).
- Backs "welche Dokumente haben keine Chunks?" / "nenne mir die Titel der leeren Dokumente" — the by-name complement
  to `ingest_status` (count) and `reindex_documents` (fix).

### `internal.list_unfiled_documents`

Source: `services/kb_maintenance_tool.py`

- **Read-only Paperless filing status BY NAME**, two modes:
  - (no query) lists the `completed` documents NOT successfully filed (`paperless_state` failed/pending) by name;
  - (with a `query`) reports a specific document's filing status — filed (+Paperless id) /
    in-Paperless-as-duplicate (done but unlinked) / failed / pending / not-intended (NULL, e.g. interactive uploads).
- **Owner-scoped** when auth is on: admin/single-user sees all; a non-admin only their OWN docs via the atom owner,
  INNER-joined fail-closed — the query mode is a corpus-wide name search that must not leak other users' document
  names.
- LIKE wildcards escaped.
- Backs "welche Dokumente sind nicht in Paperless?" / "ist die Rechnung X abgelegt?".

### `internal.refile_to_paperless`

Source: `services/kb_maintenance_tool.py`

- **Write/maintenance**: re-files `failed` documents to Paperless — resets `failed→pending` + clears the terminal
  `paperless_task_id`, then enqueues a `paperless_refile` worker task.
- The backend NEVER OCRs — Docling stays in the worker. Flip-then-enqueue is crash-safe via the periodic reconciler's
  `pending` backstop; a Redis lease (same key as `paperless_reconciler`) prevents double-enqueue.
- Targets ONLY `failed`: pending are already reconciled, and clearing a live `pending` task_id would risk the
  duplicate-upload loop.
- Optional `query` to re-file one specific failed doc.
- **Gated on `Permission.RAG_MANAGE`**.
- Backs "lege die fehlgeschlagenen Dokumente in Paperless ab".

### `internal.paperless_dedupe`

Source: `services/paperless_dedupe_tool.py`. User-facing description: `docs/FEATURES.md`, "Duplikate selbst finden
und löschen (`internal.paperless_dedupe`)".

**Destructive maintenance**: finds duplicate Paperless documents and deletes the extras itself (keeps the
**lowest-id / oldest** copy).

What counts as a duplicate:

- PASS 1 — its file **checksum is identical** (byte-identical original — the exact primary signal, catches
  re-ingest-loop copies even when their title/date/page_count drifted), OR
- PASS 2 — **ALL its metadata is identical** (correspondent, document_type, creation date, title, **page_count**) OR
  its OCR is byte-identical. PASS 2 is for **re-scans** (differing OCR bytes, same metadata), which byte-identical
  matching never did.
- `paperless_dedupe_metadata_match_enabled` (default on) gates the metadata pass; OFF = byte-identical-only.
- The checksum + metadata identity come straight from `mcp.paperless.list_all_documents` (checksum + page_count, no
  per-document fetch); only the byte-identical OCR fallback calls `get_document`.
- Metadata delete requires a **non-empty title AND a present page_count** on every group member; if either is missing
  (empty title / older MCP with no page_count) the group falls back to byte-identical. An absent field isn't
  "identical" — never delete on a weak signal.

Enumeration:

- **Enumerates the FULL archive INDEX-INDEPENDENTLY** via `mcp.paperless.list_all_documents` (DB-ordered by id, fully
  paginated, carries checksum — renfield-mcp-paperless ≥1.11.0).
- A stale/partial Paperless search index can no longer hide copies, and a duplicate group whose >500 copies share ONE
  creation date is now reachable. The old `created_before` day-window sweep stalled there.
- That sweep is kept only as a fallback when the enumeration response lacks the `total_count` contract marker (e.g.
  an older MCP whose fuzzy-fallback returns a search result); the guard never accepts that as a complete sweep →
  never false-cleans.

Deletion:

- **Rate-limit-safe batched delete**: the Paperless MCP is 60/min rate-limited, so each call deletes up to
  `paperless_dedupe_delete_batch` (default 50) extras with **retry/backoff** on a rate-limit rejection, then reports
  how many **remain** so the caller re-runs to continue.
- It NEVER claims the archive is clean while copies remain. The 2026-07-27 bug: an unthrottled bulk delete had ~226
  of 317 rate-limit-rejected, yet the agent reported "316 deleted, clean".
- Deletion goes via `mcp.paperless.delete_document` (Paperless 2.x → recoverable **trash**).
- `dry_run=true` reports the full duplicate scope without deleting.

Gate: **`Permission.ADMIN`**, fail-closed: with auth on an unidentified turn (`user_permissions=None`) is DENIED — a bulk archive delete has a larger blast radius than the reversible tools. Auth off (single-user household) skips the gate.

Backs "finde und lösche die Duplikate in Paperless" / "räum die doppelten Dokumente auf".

### `internal.find_duplicate_documents`

Source: `services/document_dedupe_tool.py`. User-facing description: `docs/FEATURES.md`, "Duplikat-Erkennung".

**KB near-duplicate detection** (#1170, `DOCUMENT_DEDUPE_ENABLED`, dark).

Identifier pass:

- Finds two KNOWLEDGE-BASE documents that byte-hash ingest dedup misses (different `file_hash` — a re-scan /
  re-export / same invoice from two sources) yet share a **document-unique identifier**: a `document_facts`
  `category='identifier'` row with the same `(kind, normalized_value)`, e.g. the same invoice number.
- Gated by a **recurring-identifier frequency cap** so a Steuernummer/IBAN/Kundennummer on many docs never emits N²
  pairs.
- **Propose-only — never deletes**: each pair lands in `document_duplicate_proposals` for owner review (mirrors the
  KG-merge / PDF-split queues). The detector's idempotency guard skips a pair proposed under ANY status (durable
  reject).
- Per-user advisory lock `0x4444` on a dedicated connection; survivor = Paperless-linked > more facts > lower id.
- Distinct SCOPE from `paperless_dedupe` (which only dedupes Paperless) — the tool description disambiguates so
  "Duplikate in der Wissensbasis" routes here.

**Phase 2 SHIPPED** — owner review:

- `/brain/review` `DocumentDuplicatesSection`/`DocumentDuplicateCard` (survivor radio + per-pair
  **supersede-vs-delete** choice + 5s undo, mirrors `MergeProposalsSection`).
- Backed by owner-gated `POST /api/document-duplicates/{id}/{approve,reject}` →
  `DocumentDedupeService.resolve_proposal`/`reject_proposal`.
- **supersede** = recoverable: sets `documents.superseded_by_document_id` on the loser, which is then EXCLUDED from
  retrieval (`superseded_by_document_id IS NULL` added to `rag_service.list_documents`, `document_search` final
  fetch, `rag_retrieval` dense+bm25, `document_fact_retrieval` search/sqlite/obligations/is_visible).
- **delete** routes the loser through `RAGService.delete_document` (CASCADE-removes the proposal).
- Both claim the proposal `pending→approved` conditionally (double-resolve → no-op).

**Phase 3 SHIPPED**:

- (a) A **text-similarity signal** (`DOCUMENT_DEDUPE_TEXT_SIMILARITY_ENABLED`, separately dark):
  - a per-user **per-anchor HNSW top-k** neighbour search — a
    `CROSS JOIN LATERAL … ORDER BY content_embedding <=> anchor LIMIT k` that the
    `idx_documents_content_embedding_hnsw` index ACCELERATES: O(N·k·log N), NOT the O(N²) all-pairs cross-join a
    naive self-join would be. kNN is asymmetric so the caller dedups min/max.
  - It runs on `documents.content_embedding` (= mean of the doc's chunk embeddings, populated best-effort at ingest
    via `np.mean`, migration `pc20260901` + HNSW index, backfill `bin/backfill_document_content_embeddings.py`),
    catching re-scans/re-exports that share NO identifier.
  - High threshold `document_dedupe_text_threshold` (0.97) + propose-only keeps distinct-but-similar docs (two
    invoices from one vendor) apart.
  - Runs AFTER the identifier pass so it skips already-proposed pairs (`find_text_similar_pairs`, signal
    `text_similarity`).
- (b) An **autonomous scan** — the `document_dedupe` **Scheduled-Task built-in**
  (`services/scheduled_tasks/builtins.py`, mirrors `kg_reconciler`: per-user `run_for_user`, self-gates on
  `document_dedupe_enabled`, `list_owner_ids` enumerates `kb_document` atom owners), so detection runs daily without
  a chat prompt.

`_Pair` carries `signal`/`shared_key`/`similarity` for both passes. Migrations `pc20260830` + `pc20260901`, model
`DocumentDuplicateProposal` + `documents.superseded_by_document_id` + `documents.content_embedding`.

## Dispatch and dependency injection

Dispatch for these tools is a special case in `services/action_executor.py` that injects dependencies the generic
`intent.startswith("internal.")` hook path cannot provide:

- `mcp_manager`, `session_id`, and the authenticated `user_id` for `list_my_memories` / `ingest_file` /
  `ingest_status` / `reindex_documents` / `list_chunkless_documents` / `list_unfiled_documents` /
  `refile_to_paperless` / `find_duplicate_documents` / `create_reminder`;
- `session_id` for `create_reminder`;
- `user_permissions` for `reindex_documents`'s + `refile_to_paperless`'s `RAG_MANAGE` gate,
  `list_unfiled_documents`'s owner-scope admin check, and `paperless_dedupe`'s `ADMIN` gate;
- `mcp_manager` for `weather_widget` / `paperless_dedupe`.

The render/weather tools' `data["artifacts"]` is collected by `chat_handler._collect_tool_artifacts` from
`agent_tool_results` and fed to `_emit_turn_artifacts` (both the single-agent and orchestration paths), the same
validate→emit→persist path as the sub-intent producers.

## Registering a NEW `internal.*` tool is two steps, not one

1. Add it to `InternalToolService.TOOLS` + `_HANDLERS` (it auto-registers in the tool registry).
2. Add the tool name to the relevant role's `internal_tools` list in **`config/agent_roles.yaml`**. This file is
   **ConfigMap-served** (`renfield-mcp-config`), NOT baked into the image, so prod needs the live ConfigMap patched
   too.

Skip step 2 and the agent reports "no tool available" even though the tool is registered — the router picks a role
and the role filters tools (e.g. `internal.bluetooth_scan` → `smart_home`, `internal.presence_history` →
`presence`). The `general` role has `internal_tools: null` = all tools.

A tool that is in `_HANDLERS` only (NOT `TOOLS`), such as `internal.device_action`, is frame-dispatched and never
agent-advertised.
