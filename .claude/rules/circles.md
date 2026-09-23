---
paths:
  - "src/backend/services/circle_*.py"
  - "src/backend/services/atom_service.py"
  - "src/backend/services/*_retrieval.py"
  - "src/backend/services/polymorphic_atom_store.py"
  - "src/backend/services/kb_shares_service.py"
  - "src/backend/api/routes/atoms.py"
  - "src/backend/api/routes/circles.py"
  - "src/backend/services/rag_service.py"
---
# Circles v1 — access tiers on every retrievable row

Loaded only when a circle / atom / retrieval service is read. Long form: `docs/CIRCLES.md` (model, data tables, full
route + frontend-page inventory), `docs/SECOND_BRAIN.md` (the four subsystems circles protect). Sibling rules:
`documents-facts.md`, `obligations.md`, `notes-wissen.md`.

| tier | name | meaning |
|---|---|---|
| 0 | self | owner-only |
| 1 | trusted | 1-3 closest people |
| 2 | household | family / housemates |
| 3 | extended | named outsiders |
| 4 | public | anyone |

## The access rule — one filter, in SQL
- Access to a source row = **OWNER** OR **tier == public** OR **explicit grant** (`atom_explicit_grants`) OR
  **tier-reach through circle membership** (`circle_memberships`). `rag_retrieval`, `kg_retrieval`,
  `memory_retrieval` push this 4-branch filter into SQL via `services/circle_sql.py` — never add a second, unfiltered
  read path next to it.
- `AUTH_ENABLED=false` short-circuits the filter (single-user mode sees everything).
- **Callers MUST pass `user_id=asker_id`** — to memory retrieval and to every `rag.search()` call. `None` reduces to
  public-tier-only when auth is on (silently: no error, just missing results).
- The document owner-branch in `circle_sql` has an atom-owner fallback so null-KB / global-RAG docs reach their owner.
- Behavioural change vs pre-circles: `ConversationMemoryService.retrieve()` respects circle reach — tier-2 household
  peers see each other's household-tier memories (it used to filter strictly `user_id == asker_id`).

## Writing source rows
- Tables: `atoms` (polymorphic registry), `circles` (per-user dimension config), `circle_memberships`,
  `atom_explicit_grants`. Denormalized `circle_tier` + `atom_id` live on `document_chunks`, `kg_entities`,
  `kg_relations`, `conversation_memories`, `notes`, and — since auth-on §8.1 — `conversations` and `meetings`
  (both `atom_id` NULLABLE there: a row nobody owns has no owner to hang an atom on; see `chat-branching.md`).
- Create a source row via `AtomService.create_with_source`, **never a direct INSERT** (the denormalized columns and
  `atoms.policy` drift apart otherwise). Change a tier only through `AtomService` — it owns the **tier cascade**
  (entity → incident relations; `kb_document` → its facts `WHERE NOT tier_overridden`).
- `services/kb_shares_service.py`: a KB-level share is exploded into per-chunk explicit grants.
- `services/circle_resolver.py` = `PolicyEvaluator` + cache, for access checks outside the SQL paths.
- `services/polymorphic_atom_store.py` = cross-source RRF; it emits per-entity `kg_node` + per-relation `kg_edge`
  atoms (`KGRetrieval.get_relevant_atoms`), while the agent's string context (`get_relevant_context`) is unchanged.

## Extraction WRITES are ownership-gated separately from retrieval
`_apply_update_v2` / `_apply_delete_v2` and the v1 contradiction path take the row to mutate from an LLM-supplied
`target_id`, and `services/memory_ops.validate_against_candidates` checks **membership in the candidate set only —
never ownership**. So the apply layer enforces it:
- an identified turn is scoped by `user_id == asker_id` (WHERE clause in v2, `_extraction_target_owned` re-check in v1);
- a turn with **no identity is refused outright while auth is on** (`_identity_scoped_write_denied`) — `user_id` is
  `None` for every device / satellite / unidentified-voice turn, and the candidate filter then degrades to public-tier,
  i.e. other users' rows.
- `AUTH_ENABLED=false` is one trust domain (filter bypassed, every row carries the same `_resolve_owner_user_id`
  fallback owner) and keeps its unscoped behaviour. Test: `tests/backend/test_memory_ownership_guard.py`.
