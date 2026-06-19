# Chat message branching (edit-and-fork)

Roadmap item 1 of `docs/design/chat-ui-modernization.md` — the heaviest Tier-1
item. Ships **dark behind `CHAT_BRANCHING_ENABLED`** (default false): the flag
gates the fork affordances + UI, NOT the schema or the active-path query (those
are always on; the one-time backfill makes flag-off byte-identical).

## Data model
- **`messages.parent_message_id`** — nullable self-FK (`ON DELETE CASCADE`),
  indexed. The conversation tree's source of truth. A *fork* = a new message
  sharing a parent with an existing one (siblings = branches). NULL = root.
- **`conversations.active_leaf_message_id`** — nullable FK → `messages.id`
  (`ON DELETE SET NULL`, `use_alter=True` to break the conversations↔messages
  FK cycle for `metadata.create_all`). The tip of the active branch.
- **Active branch = the root→leaf path**, resolved by a **recursive CTE walking
  `parent_message_id` upward from `active_leaf_message_id`**, ordered
  `timestamp ASC, id ASC` (reproduces the exact pre-branching history order for
  a linear conversation). NULL leaf → empty path. **Every recursive CTE scopes
  its recursive step to the same `conversation_id`** (`AND p.conversation_id =
  b.conversation_id`) so a stray cross-conversation parent pointer can never
  walk out of the conversation — defense in depth behind the request-level
  session scoping (see Security).
- **Migration `pc20260618_message_branching`** (down_revision
  `pc20260618_doc_quality_ignored`): adds the columns + index + FKs, then a
  one-time idempotent **backfill** — per conversation, chain messages by
  `(timestamp, id)` (`LAG`), set `active_leaf_message_id` to the last
  (`DISTINCT ON`). After backfill all reads use the walk uniformly; legacy
  conversations are a single linear branch. PG-only DML; sqlite test harness
  seeds its own trees.

## The four branch-aware seams
1. **History load** (`/api/chat/history/{session_id}`) — the active-path CTE
   replaces the flat select; also exposes `message.id` per row.
2. **conv_context replay** — self-heals: the agent reads the in-memory history
   the handler loaded via seam 1, so dead-branch
   `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` markers can't leak.
3. **Memory deactivate-at-fork** — on a fork that abandons a branch, the
   abandoned subtree's memories are flipped `is_active=False` (a downward
   recursive CTE, conversation-scoped) so retrieval can't conflate the old and
   new branches' facts. Extraction stays per-active-turn (no double-extract).
4. **Message search** — the FTS query is filtered to the active-path ids and
   `message_index` is recomputed as the ordinal *within* the active branch (so
   jump-to-message scrolls correctly).

## Fork mechanics
- `conversation_service.save_message(..., parent_message_id=None)` always
  maintains the tree: normal turns chain onto the current leaf and advance it; a
  passed `parent_message_id` inserts a **sibling** under that parent.
- **WS turn** accepts an optional `fork_from_message_id` (honored only when
  `chat_branching_enabled`). Edit-vs-regenerate is disambiguated by the **role**
  of the target: a `user` target → regenerate (no duplicate user row); else
  edit-and-resubmit. The abandoned subtree's memories are deactivated before
  generation. The `done` frame carries the new `user_message_id` +
  `assistant_message_id`.
- **`PUT /api/chat/{session_id}/active-leaf` `{message_id}`** — ownership-gated;
  moves the active leaf to an existing message (no generation).

## Frontend (Phase 1)
- `id` on `ChatUiMessage`, carried through `historyToUiMessage` + the done frame.
- `ChatMessages.tsx`: keyboard-reachable **edit** (latest user message) +
  **regenerate** (latest assistant turn), behind the `chat_branching_enabled`
  feature flag. Dark + i18n (de/en).

## Security
The request-level fork-target lookup is scoped to the caller's conversation
(`Conversation.session_id == msg_session_id`); a foreign/nonexistent
`fork_from_message_id` is dropped and the turn becomes a normal append. The
recursive-CTE conversation scoping is the second layer. (A cross-conversation
IDOR — unscoped fork target + an unscoped recursive step — was caught in review
and fixed before merge; regression-tested on Postgres.)

## Phase 2 (SHIPPED) — fork-from-any + switcher + delete + symmetric memory

- **fork-from-ANY message** — edit any user turn / regenerate any finished
  assistant turn (the Phase-1 latest-only gate is removed). The backend already
  accepted any in-session `fork_from_message_id`.
- **Per-message `‹ n/m ›` branch switcher** (NOT the `ChatHeader.tsx` global one
  the survey suggested — per-message at the fork point is the standard, more
  usable pattern and handles multiple fork points). Rendered on any message with
  siblings; ◂/▸ call `PUT …/active-leaf` with the chosen sibling; history reloads.
  Role-aware contrast (holds on the user bubble), keyboard-reachable, 44px targets.
- **Symmetric memory recompute** (`recompute_memory_activation`) replaces
  Phase-1's one-way `deactivate_memories_for_abandoned_subtree`:
  `is_active = (source_message_id ∈ active_path)` for every memory in the
  conversation, re-derived on every fork AND switch. This adds the missing
  **reactivation** half (revisiting a branch restores its memories) and **closes
  the deactivate-at-fork race** — truth is re-derived from the current leaf each
  time, and the background extraction additionally recomputes at its commit
  (flag-gated), so whichever of fork/extraction commits last wins.
- **`set_active_leaf`** resolves the target to its subtree's **deepest leaf**
  (`_deepest_leaf_message_id`) so switching activates the whole continuation of
  the chosen branch, then recomputes memory activation.
- **Delete-branch** — `DELETE /api/chat/{session_id}/branch/{message_id}`:
  ownership-gated (404), refuses a message on the active path (409 — the frontend
  switches to a sibling first), deletes the subtree. Branch-local memories are
  **soft-deleted + detached** (`is_active=False`, `source_message_id=NULL` —
  mirrors `_apply_delete_v2`; a hard delete would hit the `memory_history`
  RESTRICT FK and orphan the `atoms` row), KG-relation provenance is detached.

**Tests:** `tests/backend/test_chat_branching.py` adds the Phase-2 PG/sqlite
cases (symmetric recompute deactivate+reactivate, deepest-leaf, branch metadata,
delete-branch guards + FK-safe soft-delete with a `memory_history` row), 26 pass.
Frontend `ChatMessages.branching.test.tsx`: fork-from-any, switcher nav, delete.

## Tests
`tests/backend/test_chat_branching.py` (sqlite tree-maintenance + deletion FK
metadata; Postgres recursive CTEs, active-path order, fork sibling + leaf
advance, memory deactivate, search filter+reindex, **cross-conversation IDOR
isolation**, SET-NULL deletion) — 16 pass on real PG. Frontend:
`ChatMessages.branching.test.tsx` + `historyToUiMessage` id carry.
