---
paths:
  - "src/backend/services/conversation_service.py"
  - "src/backend/services/ollama_service.py"
  - "src/backend/api/routes/chat.py"
  - "tests/backend/test_chat_branching.py"
---
# Chat message branching (edit-and-fork)

Loaded only when the conversation service, its `ollama_service` delegate, the chat routes or the branching tests are
read. Long form: `docs/design/chat-branching.md`. UI affordances/artifacts live in `chat-ui.md`.

## Model
- Conversation **tree**: `messages.parent_message_id` (self-FK) + `conversations.active_leaf_message_id`; migration
  `pc20260618_message_branching` incl. an idempotent backfill (legacy conversation = one linear branch).
- Active branch = recursive CTE walking parents up from the leaf (`ConversationService.active_path_message_ids`).
- **Every recursive CTE is conversation-scoped ON THE RECURSIVE STEP** (`p.conversation_id = b.conversation_id`), not
  only on the seed row — otherwise a stray parent pointer walks into another user's conversation (cross-conversation
  IDOR). The fork-target lookup is additionally scoped to the caller's session; a foreign id is dropped → normal append.
- `CHAT_BRANCHING_ENABLED` (dark) gates the fork affordances + UI only. **The CTE is always on → flag-off must stay
  byte-identical**; never add a flat-select branch for flag-off.
- A conversation delete must not trip the message self-FK / leaf FK (leaf FK is `ON DELETE SET NULL`).

## The four branch-aware seams (touch one → check all)
1. History load: active-path CTE, exposes `message.id`.
2. `conv_context`: self-heals off that history (no dead-branch error markers).
3. Memory: `recompute_memory_activation` — `is_active = (source_message_id ∈ active_path)` for every memory of the
   conversation, **symmetric**, re-derived on every fork AND every switch. Background extraction also recomputes at its
   commit (flag-gated) so whichever of fork/extraction commits last wins. Do not reintroduce a one-way deactivate.
4. Message search: filtered to the active path, `message_index` recomputed within the branch (jump-to-message).

## Fork / switch / delete
- `ConversationService.save_message` ALWAYS maintains the tree (`ollama_service.save_message` only delegates): normal
  turn chains onto the leaf and advances it; `parent_message_id` inserts a sibling.
- WS `fork_from_message_id` is honoured only when the flag is on; edit vs regenerate is decided by the target's role;
  the `done` frame carries the new message ids.
- `PUT /api/chat/{session_id}/active-leaf` (ownership-gated) resolves the target to its subtree's deepest leaf
  (`_deepest_leaf_message_id`), then recomputes memory activation.
- `DELETE /api/chat/{session_id}/branch/{message_id}`: ownership-gated 404; **refuses an active-path message with 409**;
  subtree delete. Branch-local memories are **soft-deleted + detached** (`is_active=False`, `source_message_id=NULL`) —
  a hard delete hits the `memory_history` RESTRICT FK and orphans the `atoms` row. KG provenance is detached, not deleted.
