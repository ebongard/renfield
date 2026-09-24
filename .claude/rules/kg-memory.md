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
- **Type-guard (#1330):** differing PRIMARY types (`_types_compatible`) drop the pair — UNLESS the names are related
  (the mis-TYPED-duplicate shape), which survives as a review proposal (`cross_type`), never an auto-merge. Embedding
  cannot see the difference: a town and the company seated in it are described out of the same documents (0.895
  measured). **Never the `entity_types` superset** — it only grows (merge unions it, re-mention folds into it), so an
  overlap test disarms itself; the scalar type is written only by an explicit owner edit. `thing` is a WILDCARD
  (`_UNTYPED`, the extraction's no-type bucket) and an absent type likewise; and the drop is conditioned on
  `not typo`, else it cancels the #876 exception. Both find-time guards count what they eat
  (`dropped_cross_type` / `dropped_person_guard`) — `candidates` counts survivors. The leniency (no type = no
  mismatch) belongs to the DROP only: an AUTO-merge additionally requires `types_known`, else a corrupt empty
  `entity_type` would be compatible with everything at the silent-merge gate.
- Same-name gate: same normalized name + empty/identical descriptions never auto-merges → review.
- Per-user non-blocking advisory lock `_RECONCILER_LOCK_NS`; an overlapping run is a no-op. Each pass first re-embeds
  up to `KG_RECONCILER_EMBED_BACKFILL_PER_RUN` null-embedding entities (else invisible to the self-join).
- Approving a proposal whose counterpart was already merged closes it as `superseded`, not `approved`.
- **A rejection is final for the reconciler.** The self-join AND `_propose` exclude pairs with a `pending` OR
  `rejected` proposal; a rejected pair never comes back on its own. The only way back is an explicit admin merge
  (`POST /entities/merge`, `KG_MANAGE`). Label precedence: `cross_tier` > `cross_type` > `name_typo` > `gray_zone`;
  the card keys its warning on `cross_tier` and DERIVES the label from the live types when they differ (rows pending
  from before the guard carry `gray_zone` and would otherwise read "similar but uncertain").
- Routes are `KG_VIEW`, own graph only, per-proposal ownership 404. Scheduler `_schedule_kg_reconciler`.
- **The queue's unit is the CLUSTER, not the pair** (`resolve_cluster`, `POST /merge-proposals/cluster`). Measured
  2026-09-24: 1 391 pending, 2 ever resolved — 1 365 pairs over 952 entities in 199 name clusters, and every pair
  above the auto bar had identical names + no description (the same-name gate above, working as designed). Two
  invariants make a bulk decision safe: **only same-tier pairs take part** (a cross-tier pair changes reach and is
  reported back in `skipped_cross_tier`, never swept — which also makes `tier = MIN` a no-op), and **the fold set is
  derived from the PROPOSALS, not from the request** (else the route merges two arbitrary entities on demand). #1330
  added a third: **only type-compatible pairs take part** (`skipped_cross_type`) — that bar is for the ROUTE, since
  the UI builds components on primary-type EQUALITY and can never submit such a pair. Pairs that clear both filters
  but do not reach the survivor are `skipped_unreachable`, NOT `skipped_cross_tier` — they sit at the survivor's own
  tier, so calling them a visibility skip was a lie. The frontend groups by connected components over the pairs, NOT
  by name (a pair can hold two spellings), and does not chain across differing primary types either.
- **#1333, the fourth bar: a WEAK pair never takes part** (`skipped_weak_edge`). `name_typo` = "maybe two different
  PEOPLE, one edit apart" — a bulk fold hands that judgement to a click showing a count, not the names. **NOT
  everything the reconciler refuses to auto-merge**: a `gray_zone` pair carries `block_auto_merge` from
  `_name_collision_low_signal` too, and folding those is the whole point (243 pending on the household). The bar reads
  the persisted `reason`, not `block_auto_merge` (a find-time flag that never reaches the queue).
  🛑 Refusing the EDGE is not enough, and this holds for BOTH pairwise bars — weak AND cross-type. Neither relation
  is transitive (`thing` is a wildcard: `organization`~`thing` and `place`~`thing` pass, `organization`~`place` does
  not), so both endpoints can still arrive in the survivor via a third entity, and `_repoint_after_fold` then closes
  the refused pair as `superseded` — executed, while the response calls it skipped. Any pairwise-refused pair with
  BOTH endpoints in the component therefore refuses the **whole fold**. The type bar exists FOR the route, which is
  exactly why leaving it out of this check left the route-only bar with a route-reachable bypass (#1334).
  The refusal returns a CODE plus the pairs plus an uncapped total (`cluster_has_undecidable_pair`), never a
  sentence: the UI translates it, and a capped list without the total truncates in silence.
  Measured 2026-09-24: 11 `name_typo` pending on the household, six inside a foldable cluster.
  🛑 `_repoint_after_fold` rewrites a pair's endpoints but NOT its `reason` — since #1333 that reason is a GATE, so a
  re-pointed pair can carry a stale one in either direction. Known, not fixed: the self-join excludes pending pairs,
  so nothing re-evaluates it.
- The proposal list carries what the embedding does not — description, edge count, first/last seen — because the owner
  facing two bare identical names is in exactly the position the reconciler refused to decide from.

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
