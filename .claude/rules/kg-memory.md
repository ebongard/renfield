---
paths:
  - "src/backend/services/kg_*.py"
  - "src/backend/services/knowledge_graph_service.py"
  - "src/backend/services/merge_guard.py"
  - "src/backend/services/graph_expansion.py"
---
# Knowledge graph: canonicalization, merge, reconciler, graph expansion

Loaded only when a KG service file is read. Memory extraction/bridge/subsume lives in `memory-extraction.md`.
Long form: `docs/design/structured-memory.md`, `docs/design/kg-cross-user-canonicalization.md`, `docs/CIRCLES.md`.

## Schema (migrations `pc20260604_struct_mem` + `pc20260604b_kgmp`)
- `kg_entities.canonical_id` self-FK: **NULL = canonical/live; non-NULL = merge tombstone → survivor**
  (mirrors `procedural_skills.merged_into_id`). `surface_forms` JSONB = absorbed aliases (GIN `jsonb_path_ops`);
  `entity_types` JSONB = multi-type superset, scalar `entity_type` stays the closed-enum primary.
- `kg_relations.stated_by_user_id` = who asserted the fact, NOT the owner.

## `KnowledgeGraphService.resolve_entity` — cascade order is the contract
exact name → surface-form (jsonb `@>`) → embedding (**SAME-TIER only** + high threshold, `::halfvec`) → create new.
- Never fold across tiers or on a weak signal inline — that is a reconciler proposal.
- **PERSON entities SKIP the inline embedding match.** The gate is keyed on the multi-type `seed_types`, so a person
  carried as a SECONDARY type counts. Reason: a generic meta-description turns a row into a generic-person centroid
  that any bare name folds into (magnet hub). The embedding is still computed + stored (retrieval, reconciler).
- Generic person descriptions are stripped (`is_generic_person_description`, whole-string match) BEFORE embed/store.
  `prompts/knowledge_graph.yaml` must ask for an entity-specific description or empty. Repair: `services/kg_demagnetize.py`.
- Bridge params `create_tier` (tier for create + same-tier search) and `match_entity_type` are additive; notes resolve
  with `match_entity_type=True`, `use_embedding=False`.

## `merge_entities(loser, winner)`
- **A merge never raises visibility: survivor tier = MIN.** Relations recompute `circle_tier=LEAST(subject,object)`.
- FOR UPDATE + `services/merge_guard.is_already_merged`; reparent `kg_relations`, re-dedup, follow
  `conversation_memories.subject_entity_id`, tombstone the loser. Cross-user pairs are refused.

## Reconciler (`KG_RECONCILER_ENABLED`, dark)
- Same-tier high-confidence → auto-merge. **Cross-tier / gray-zone → `kg_merge_proposals`, never a silent merge.**
- **Person-guard:** a person-involving pair (primary OR `entity_types`) with UNRELATED names is dropped entirely — no
  merge, no proposal (`_names_related` = equal or whitespace-token-subset). The auto-merge gate RE-CHECKS it.
  ONE exception (#876 field data): a **typo pair** — same tokens except one, that one differing by a single in-token
  edit (`_names_near_typo`, both spellings ≥ 4 chars) — survives as a **review proposal** (`reason=name_typo`), never
  an auto-merge. Short tokens stay excluded on purpose (numbered test accounts "…01"/"…02" are distinct people).
- Same-name gate: same normalized name + empty/identical descriptions never auto-merges → review.
- Per-user non-blocking advisory lock `_RECONCILER_LOCK_NS`; an overlapping run is a no-op. Each pass first re-embeds
  up to `KG_RECONCILER_EMBED_BACKFILL_PER_RUN` null-embedding entities (else invisible to the self-join).
- Approving a proposal whose counterpart was already merged closes it as `superseded`, not `approved`.
- Routes are `KG_VIEW`, own graph only, per-proposal ownership 404. Scheduler `_schedule_kg_reconciler`.

## Conflation tripwire (`KG_CONFLATION_MONITOR_ENABLED`, dark, read-only)
`services/kg_conflation_monitor.py` flags distinct-name same-type same-tier NON-person pairs only. Persons are excluded
on purpose (names cluster ≥ threshold by themselves → permanent noise). Expected 0; it never mutates.

## Graph expansion (`GRAPH_EXPANSION_ENABLED`, dark; off = `query` byte-identical)
- `graph_expansion.py::expand_fused` runs **POST-RRF** in `PolymorphicAtomStore.query` — the single seam.
- Level-synchronous BFS (min-hop distance), `kg_entities_circles_filter` **PER HOP**, per-hop frontier cap,
  decay = pivot/(1+hop), caps `GRAPH_EXPANSION_MAX_HOPS` / `GRAPH_EXPANSION_MAX_EXPANDED`.
- A `kg_edge` is emitted only when **BOTH endpoints are accessible** (leak-safe). Mark `payload.expanded` + `hop`.

## Endpoint names
`get_relevant_atoms` / `get_relevant_context` resolve endpoint names ONLY through `KGRetrieval._resolve_entity_names`
→ `kg_entities_circles_filter`, "?" on miss. Never build an unfiltered `name_map`.
