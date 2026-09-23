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

## A conversation is a SHARED artifact (auth-on §8.1)
`conversations` carry `circle_tier` + `atom_id` and read through
`conversations_circles_filter` — the same four-branch filter as every other atom. A room history (satellite) belongs
to the DEVICE account at tier 2, so every member whose tier reaches it reads and CONTINUES the one thread; browser
chats stay at tier 0. `ConversationService.reaches()` runs that filter for one row (never a second ownership rule in
Python), `may_alter()` keeps deleting, re-leafing and branching with the owner plus `chat.all` — reach is for reading,
not for wiping the kitchen's thread. The message search follows conversation reach (decided): whoever may read the
thread finds the line in it.
`Meeting` follows the same rule now; its tier said "shared" from the start while its routes filtered on owner
equality. Writing routes pass `for_write=True` and stay owner-bound.
An atom needs an owner (`atoms.owner_user_id` NOT NULL), so an OWNERLESS conversation gets none — `atom_id` is
nullable here, unlike on notes, and the P2 backfill fills it in when the row gets an owner.
**The migration's downgrade drops the COLUMNS before deleting the atoms**: `atom_id` carries ON DELETE CASCADE, so
the other order deletes every conversation and meeting. That same CASCADE widens `AtomPurgeService.purge`: an Art.-17
erasure now takes the whole conversation (with its messages) and the whole meeting, which is the point, not an accident.

**A room history is owned by the DEVICE account** (`satellite_handler.room_history_owner_id`), never by the recognised
speaker — §8.1 rules that out by name, because `may_alter` would then let that one person delete the household's
thread. No device account ⇒ no owner ⇒ tier falls back to 0: an ownerless row at tier 2 reaches nobody, since every
branch of the filter keys on the owner. The handoff copies the source tier for the same reason — it runs BEFORE
`save_message`, which never revisits the tier of a row that already exists.
**Every place that hands a row an owner registers its atom** — creation, the auth-off adoption branch,
`associate_speaker`, the handoff, `POST /api/chat/send`, and the meeting upload. `ConversationService.ensure_atom` is
the one entry point and runs in a SAVEPOINT (`begin_nested`): `create_with_source` flushes, and a failed flush aborts
the turn's whole transaction, so "best-effort" is only true inside one.

## Access to a conversation (`enforce_ownership`, auth-on only)
The rule is REACH, on both sides, and its negative half is unchanged: a conversation out of the caller's reach is
refused — foreign AND **ownerless** (every reach branch keys on the owner, so an ownerless row reaches nobody). The
session id is minted by the CLIENT and never validated, so such a row would otherwise be readable by anyone holding
the id. Reads return `[]`, writes raise `PermissionError`, push-registration says no; only a session with **no row at
all** is free to take. Adoption ("first writer becomes the owner") exists only under auth-off.
**`enforce_ownership` is an auth-on rule — derive it from the flag, never hardcode `True`**: `reaches()` fails closed
without a caller identity, so a hardcoded `True` refuses every write under auth-off (`scanner_jobs` did, and every
scan outcome into a voice-started conversation was refused).
The decision is taken at the BOUNDARY — `_session_registerable_by` on the `register` frame, `_replacement_session_for`
on the first message, `api/routes/chat.py::conversation_is_callers` for the REST fallback — not at persistence: the
scan return path, the paperless-confirm lookup and the push registration all key on the id the turn STARTED with.
**Those boundary checks ask `reaches()` too**; on owner equality they would swap a member onto a fresh id before the
service layer ever ran, and the shared thread would be readable but never continuable from a browser. A refused id is
swapped and announced as `{"type": "session_replaced", "session_id": …}` — the id and nothing else, or the frame
becomes an oracle. The late `ConversationNotOwnedError` catch in the save block is a backstop for clients that send no
register frame; it rebinds `session_state.db_session_id` too. Never catch a bare `PermissionError` there — it is a
builtin `OSError` descendant. No identity (device token) → no replacement: it would mint a new ownerless row per turn.

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
