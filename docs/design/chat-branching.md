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
- **`messages.conversation_id`** — `ON DELETE CASCADE` (`pc20261107_chat_fk_rules`).
  Before that only the ORM cascade removed the messages, so the atom purge (a
  Core `DELETE FROM atoms` that cascades into `conversations`) failed on every
  conversation with messages: an Art. 17 erasure of a chat was refused. The
  provenance pointers onto a message (`conversation_memories.source_message_id`,
  `kg_relations.source_message_id`) are `ON DELETE SET NULL`: the memory and the
  relation are atoms of their own and outlive the chat they were drawn from.
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

## Deleted conversations stay deleted (#1485)

The browser mints the session id and keeps it in `localStorage`. A conversation
deleted elsewhere — another tab, another device, `DELETE /conversations/cleanup`,
an Art.-17 atom purge — was recreated under the same id by the next message of a
browser that still held it; everything that outlives a delete and keys on the id
(KG provenance in `source_session_id`, logs) was then attributed to new content.

- **Record:** `deleted_conversation_sessions(session_id, deleted_at)` — the id and a
  UTC timestamp, no owner, no content. Filled by the trigger
  `trg_conversations_record_deleted_session` (AFTER DELETE, per row), because the
  atom purge removes conversations through an `ON DELETE CASCADE` no Python code
  sees. DDL once in `services/conversation_tombstones.py`, used by the migration
  `pc20261106_conv_tombstones` and the test harness. No backfill: ids deleted
  before the migration are recorded nowhere.
- **Retention (#1529):** `CONVERSATION_TOMBSTONE_RETENTION_DAYS`, default 0 = keep
  forever (no prune, no query; the table then only grows, one short row per
  delete). With N > 0 the daily built-in task `conversation_tombstone_cleanup`
  (`prune_expired_tombstones`) deletes records whose `deleted_at` is MORE than N
  days past — strict `<`, a record exactly N days old stays; a non-positive value
  never prunes; the cut-off uses the database clock that wrote `deleted_at`. Index
  `ix_deleted_conversation_sessions_deleted_at` (`pc20261111_tombstone_idx`, also
  declared on the model). A window is safe because a record is needed only until
  the client holding the stale id has spoken once (it is then swapped to a fresh
  id); what it gives up is the client that stays silent longer than N days — its
  stale id is accepted again, the pre-#1485 behaviour, without any deleted content
  returning. Reasons to bound it: growth is tied to deletes (a job that creates
  and purges sessions adds rows forever), and a plugin transport may use an
  external identifier as the session id, which would otherwise outlive the delete
  indefinitely. A satellite room id ages from its LAST delete (the trigger
  refreshes `deleted_at`).
- **Boundary:** an id with NO conversation row that is recorded is refused for
  every caller — auth on or off, JWT or device token — at the WS `register` frame
  and first message (`session_replaced` with a fresh id) and in `POST /api/chat/send`
  (answers under the fresh id). The fresh id is not recorded, so there is no loop.
  An existing row under a recorded id (a satellite recreating its fixed room id)
  is judged by reach as before.
- **Writer:** the boundary alone is not enough — a turn already past it (the probe
  runs at turn start, the save after the LLM round), a scan outcome and the
  Paperless announcement write after a delete without crossing it again. So
  `ConversationService.save_message` refuses to CREATE a row under a recorded id
  (`ConversationDeletedError`, a subclass of `ConversationNotOwnedError`): the WS
  turn falls into its replacement path (under auth-off too, where the turn has no
  identity), the scanner settles `refused`, the Paperless announcement is skipped.
  Only the satellite opts in (`allow_deleted_session=True`): its room id is fixed
  per day. The Paperless announcement writes in its own session after the filing
  committed — a refused `save_message` rolls its session back, and a shared one
  undid the tracking row and the finalize stamp.
- **Live socket:** its history is loaded, so only the per-turn freshness probe can
  notice. `_probe_session_freshness` asks the record when the newest message is
  gone; `history_needs_reload(session_deleted=True)` forces the reload where the
  boundary check runs. Every switch to another conversation — the `register`
  frame the browser sends on each session change, a message naming another id, a
  replacement — goes through `ConversationSessionState.bind_session`, which drops
  the in-memory history, its load flag and its watermark. Before, the `register`
  frame only moved the id: the next message saw the same id, skipped the reload,
  and the previous conversation's turns went into the new one's prompt.
- **Uploads:** a file attached under the deleted id AFTER the delete belongs to
  the turn the tab is about to send; it moves to the fresh id (under auth only
  the caller's own). Older uploads stay — moving them would revive part of the
  deleted conversation.
- **Frontend:** on `session_replaced` the tab keeps only the turn in flight (last
  user message onward), or nothing when no turn is running
  (`ChatPage/context/sessionReplaced.ts`).

## Background moved from CLAUDE.md (2026-09-20)

- Two defects were caught in `/review` and fixed before merge: the cross-conversation IDOR (see Security) **and a
  conversation-delete FK violation** — deleting a conversation must not trip the new `parent_message_id` self-FK /
  `active_leaf_message_id` FK (hence `ON DELETE CASCADE` / `ON DELETE SET NULL`, regression-tested as "SET-NULL
  deletion").
- Roadmap position: item 1 of the chat-UI modernization roadmap, "the heaviest". Frontend Phase 1 =
  edit-latest-user / regenerate-latest-assistant; Phase 2 = fork-from-ANY + the per-message `‹ n/m ›` switcher +
  delete-branch + the symmetric memory recompute.
- The user-facing description lives in `docs/FEATURES.md`. The PWA cache-propagation rule that lets a new frontend
  reach the browser is in the deploy-production skill ("Frontend PWA cache propagation").
- Working rules for editing this code: `.claude/rules/chat-branching.md`.
