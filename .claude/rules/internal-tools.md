---
paths:
  - "src/backend/services/*_tool.py"
  - "src/backend/services/action_executor.py"
  - "config/agent_roles.yaml"
  - "src/backend/ha_glue/services/internal_tools.py"
  - "src/backend/services/widget_tools.py"
---
# Internal agent tools (`internal.*`)

Loaded only when an internal-tool file, `action_executor.py` or `config/agent_roles.yaml` is read.
Full table (every tool, purpose, source, gate): `docs/INTERNAL_TOOLS.md`.

## Registering a NEW tool is TWO steps, not one
1. Add it to `InternalToolService.TOOLS` + `_HANDLERS` (it auto-registers in the tool registry).
2. Add the tool name to the relevant role's `internal_tools` list in `config/agent_roles.yaml`. That file is
   **ConfigMap-served** (`renfield-mcp-config`), NOT baked into the image — prod needs the live ConfigMap patched too.
- Skip step 2 → the agent reports "no tool available" although the tool is registered: the router picks a role and
  the role filters tools (e.g. `internal.bluetooth_scan` → `smart_home`, `internal.presence_history` → `presence`).
- The `general` role has `internal_tools: null` = all tools.
- `_HANDLERS`-only (NOT in `TOOLS`) = frame-dispatched, never agent-advertised (`internal.device_action`, reached
  only via the `device_action` WS frame). Do not add such a tool to `TOOLS`.

## Identity is injected, never taken from LLM params
- `services/action_executor.py` special-cases these tools because the generic `intent.startswith("internal.")` hook
  path cannot provide `mcp_manager`, `session_id`, the authenticated `user_id` or `user_permissions`.
- A handler must never read `user_id` / `session_id` / permissions from the tool arguments.
- A new tool that needs one of these must be wired into that special case, or it silently gets none.
- Widget tools return `data["artifacts"]`; `chat_handler._collect_tool_artifacts` → `_emit_turn_artifacts` validates,
  emits and persists. Typed JSON from the agent via `artifact_service.validate_artifact`, NEVER free-text parsing.

## Permission gates (fail-closed)
Rule for all: auth-off / `user_permissions=None` is allowed; an authenticated low-privilege user is refused.
- `Permission.RAG_MANAGE`: `internal.reindex_documents`, `internal.refile_to_paperless`.
- `Permission.ADMIN`: `internal.paperless_dedupe`.
- `HA_CONTROL`: `internal.announce_in_room` + `internal.broadcast_announcement` (`_HA_CONTROL_GATED_TOOLS` in
  `ha_glue_execute_tool`) and `internal.device_action`, which also re-validates domain, action and entity.
- `internal.list_unfiled_documents` is owner-scoped: admin/single-user sees all, a non-admin only their OWN docs via
  the atom owner, INNER-joined fail-closed (its `query` mode is a corpus-wide name search); LIKE wildcards escaped.
- `internal.list_my_memories` reads only the authenticated user's own memories.
- `internal.knowledge_search`: fact-source titles are circle-filtered separately (`_visible_document_meta`); a
  non-visible source becomes a generic `Dokument {id}` label with no chip.

## H4 — handlers re-assert their runtime flag
A tool re-checks its feature flag in-handler and refuses rather than persist something nothing will ever act on:
`internal.create_reminder` → `settings.proactive_reminders_enabled`; `internal.ingest_file` → `FOLDER_INGEST_ENABLED`;
`internal.find_duplicate_documents` → `DOCUMENT_DEDUPE_ENABLED` (dark).

## Destructive / write tools — safety properties
- `internal.paperless_dedupe`: keeps the lowest-id (oldest) copy; deletes via `mcp.paperless.delete_document`
  (recoverable trash); batched (`paperless_dedupe_delete_batch`, 50) with retry/backoff against the 60/min MCP limit;
  NEVER claims clean while copies remain; never deletes on a weak signal (metadata match needs a non-empty title AND
  a `page_count` on every member, else byte-identical only); an enumeration without `total_count` is never a full sweep.
- `internal.find_duplicate_documents`: propose-only, never deletes (`document_duplicate_proposals`, advisory lock
  `0x4444`); a pair proposed under ANY status is skipped (durable reject).
- `internal.refile_to_paperless`: targets ONLY `failed` — clearing a live `pending` `paperless_task_id` risks the
  duplicate-upload loop; the backend never OCRs (enqueue a `paperless_refile` worker task).
- `internal.reindex_documents`: skips unindexable docs unless `force=true`; the filter is in SQL so the cap
  (200 / max 500) applies to repairable docs.
