---
paths:
  - "src/backend/services/conversation_memory_service.py"
  - "src/backend/services/turn_extraction.py"
  - "src/backend/services/memory_*.py"
---
# Memory extraction, Memory→KG bridge, subsume, spoken turns

Loaded only when a memory service file is read. Entity resolution/merge/reconciler lives in `kg-memory.md`.
Long form: `docs/design/structured-memory.md`, `docs/CIRCLES.md`.

## Subject attribution (D9)
Each extracted fact is bound to a `subject_name` (`conversation_memories.subject_entity_id` + `subject_name`);
retrieval carries it and the injected context is subject-tagged (`- [FACT · <name>] …`) so facts about different
people cannot conflate. Do not drop the tag from the context builder. `subject_name`/`subject_entity_id` ride on
`MemoryResponse`.

## Memory→KG bridge (`MEMORY_KG_BRIDGE_ENABLED`, dark; off = byte-identical)
- `ConversationMemoryService._bridge_subject_entity` runs in the **BACKGROUND** extraction path, never the sync turn.
  Only `fact`/`preference` subjects are bridged.
- It calls `resolve_entity` with `create_tier` = the memory's `circle_tier` (never the hardcoded tier 0) and
  `match_entity_type` (a person-fact must not link a same-named place).
- Backfill `services/memory_bridge_backfill.py` / `bin/backfill_subject_entity_ids.py` (`--dry-run`/`--commit`):
  resolve-or-create at `memory.tier`, per-row atomic create+link, idempotent.
- **Subject-union retrieval** (`memory_retrieval.retrieve`): query-named entities (exact word-token + surface-form, no
  LLM) → their `subject_entity_id` memories are unioned into the embedding hits **through the SAME `circle_sql`
  filter** — no second unfiltered path. Chase `canonical_id` tombstones; own cap
  `MEMORY_RETRIEVAL_SUBJECT_UNION_LIMIT`. `GET /api/memory/by-subject/{entity_id}` is circle-filtered too.

## Subsume (`MEMORY_SUBSUME_TO_KG`, dark, aggressive)
Decomposable `fact` memories with a subject are NOT stored flat; preferences/instructions/context stay flat.
- Coordination: ONE ordered background coroutine `services/turn_extraction.extract_structured_background` (after the
  `done` frame). The `post_message` hooks run **FIRST**; `kg_post_message_hook` fills `captured_subjects` with the
  subjects of relations it actually saved; KG extraction runs **exactly once**; then memory extraction gets the set
  as `captured_kg_subjects`.
- `_should_subsume_fact`: the per-turn `captured_kg_subjects` set is the **PRIMARY** gate. `_subject_is_kg_representable`
  (`MEMORY_SUBSUME_REQUIRE_KG_RELATION`, default on) is ONLY a fallback for uncoordinated callers
  (`captured_kg_subjects is None`).
- **NOT truly per-fact** — the signal is subject NAMES. Two facts about one subject in one turn (one relation saved,
  one state/attribute) still lose the state fact: the documented residual (`mixed-same-subject-*` case in
  `bin/run_subsume_recall_loss_eval.py --perfact`). Do not claim a per-fact guarantee.
- **Still single-user only.** Subsume off → coordination off, two independent tasks (legacy).

## Spoken turns (`services/turn_extraction.py`) — deliberately UNFLAGGED, no kill-switch
- Both producers (browser `chat_handler`, `satellite_handler._spawn_satellite_extraction`) go through the same seam:
  `spawn_memory_extraction` / `spawn_post_message_hooks`. **ONE skip policy:** memory flags off / empty answer /
  `action_success is False` → nothing scheduled. Do not fork a second policy per producer.
- `TurnExtractionSpawn.owns_post_message` keeps KG extraction at exactly once per turn under subsume coordination.
- **Privacy boundary:** a spoken turn is extracted ONLY when the speaker was recognized (`sat_user_id`). `None` ⇒ no
  memory extraction AND no `post_message` hooks — an unattributed voice must not become somebody's memory.
- **Never extract an unpersisted turn** (no conversation `session_id`, same gate as `ollama.save_message`).
- Fire-and-forget after the turn is persisted: TTS is never delayed, every failure is swallowed.
- `chat_handler` re-exports `extract_memories_background` / `extract_structured_background` under their historical
  private names — keep the re-exports when moving code.

## Writes are ownership-gated (long form in `circles.md`)
UPDATE/DELETE targets come from an LLM-supplied `target_id`; `validate_against_candidates` checks MEMBERSHIP only.
The apply layer scopes by `user_id == asker_id` (`_extraction_target_owned`) and REFUSES a turn with no identity
while auth is on (`_identity_scoped_write_denied`). Test: `tests/backend/test_memory_ownership_guard.py`.
