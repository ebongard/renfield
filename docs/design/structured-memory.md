# Structured Memory — KG canonicalization + subject attribution

Structured Memory lifts personal memory from flat text onto the typed knowledge-graph (KG) substrate: entities are
canonicalized (one live row per real-world thing, merges leave tombstones), every memory fact is bound to the subject
it is about, and flat memories are bridged onto canonical entities so "Was weiß ich über X" becomes deterministic.

This document is the long form that used to live in `CLAUDE.md` (moved 2026-09-20). The short, must-not-get-wrong
versions are the path-scoped rules `.claude/rules/kg-memory.md` and `.claude/rules/memory-extraction.md`.
Related designs: `docs/design/kg-cross-user-canonicalization.md` (household sharing, Phase 5),
`docs/design/kg-bitemporal-edges.md`, `docs/design/kg-contact-points.md`, `docs/CIRCLES.md` (the tier model every
path below must respect). Example names in this document are placeholders.

## Schema

All additive on the circles tables. Migrations `pc20260604_struct_mem` + `pc20260604b_kgmp`.

- `kg_entities`
  - `canonical_id` self-FK — NULL = canonical/live; non-NULL = merge tombstone → survivor (mirrors
    `procedural_skills.merged_into_id`).
  - `surface_forms` JSONB — absorbed aliases, GIN `jsonb_path_ops`.
  - `entity_types` JSONB — multi-type superset; the scalar `entity_type` stays the closed-enum primary.
  - `external_id` — column only.
- `kg_relations`: `stated_by_user_id` (who asserted the fact, ≠ owner) + `source_message_id`.
- `conversation_memories`: `subject_entity_id` + `subject_name` (WHO the fact is about).

## Entity resolution

The cascade in `KnowledgeGraphService.resolve_entity`:

exact name → surface-form (jsonb `@>`) → embedding (SAME-TIER only + high threshold, `::halfvec`, name+description) →
create new.

It never folds across tiers or on a weak signal inline; those cases become reconciler proposals.

### PERSON entities skip the embedding-match step

**PERSON entities skip the embedding-match step entirely.** The gate is keyed on the multi-type `seed_types`, so a
person carried as a secondary type is covered too.

Why: people are identified by name (exact + surface-form). A generic meta-description ("Vollständiger Name einer
Person") would otherwise turn a row into a generic-person *centroid* that any bare name folds into — the 127-mention
magnet-hub bug.

- Non-person types keep embedding-match; it salvages OCR/typo variants like Bnn→Bonn.
- The embedding is still computed + stored for persons (it backs retrieval + reconciler dedup); only the inline match
  is suppressed.

### Defense in depth

- `resolve_entity` strips generic descriptions (`is_generic_person_description`, **whole-string** match) from person
  rows before embed/store, so new rows never re-create the centroid.
- `services/kg_demagnetize.py` (+ `bin/demagnetize_person_entities.py`, `--dry-run`/`--apply` with a fail-closed
  pre-mutation audit dump) repairs existing magnet rows by NULLing the generic description and re-embedding name-only.
- The extraction prompt (`prompts/knowledge_graph.yaml`, all 4 variants) requires an entity-specific description or
  empty, never a type-meta-description.

### Conflation tripwire

`services/kg_conflation_monitor.py` (`KG_CONFLATION_MONITOR_ENABLED`, opt-in, read-only): a periodic per-user halfvec
self-join logs + gauges (`renfield_kg_conflation_candidates`) **distinct-name, same-type, same-tier NON-person** pairs
embedding ≥ threshold — a forming magnet in a type where resolve still embedding-matches.

**Persons are excluded** (primary OR multi-type): their names inherently cluster ≥ threshold name-only (measured: two
distinct first names at 0.894) and resolve skips embedding-match for them anyway, so a close person pair can't fold —
flagging it would be permanent noise. Expected 0; never mutates; on-demand via `bin/scan_kg_conflation.py`.

## Merge

**`merge_entities(loser, winner)`** ports the skill-curator merge (FOR UPDATE + the shared
`services/merge_guard.is_already_merged`) and adds entity-specific work:

- reparent `kg_relations` FKs, then re-dedup;
- recompute `circle_tier=LEAST(subject,object)` + atom policy;
- follow `conversation_memories.subject_entity_id`;
- tombstone the loser.

**Invariant: a merge never raises visibility** (survivor tier = MIN).

## Reconciler

`services/kg_reconciler_service.py` (`KG_RECONCILER_ENABLED`, opt-in): a periodic per-user halfvec self-join.
Same-tier high-confidence dupes auto-merge; cross-tier / gray-zone pairs become `kg_merge_proposals` for owner review
(never silently merged).

**Person-guard** — this is what makes it safe to enable, and it mirrors resolve's person embedding-skip. A
person-involving pair (either side person-typed, primary OR `entity_types` contains person) whose names are UNRELATED
is dropped entirely — no merge, no proposal — because distinct person names embed ≥ the candidate threshold by
themselves. `_names_related` = equal or whitespace-token-subset, e.g. "Alice" ⊆ "Alice B.", or a first name ⊆ the same
person's full name. The auto-merge gate re-requires name-relatedness for person pairs (defense in depth behind the
find-time drop), so a detection miss can't silently merge two distinct people.

Operational details:

- Each pass is serialized per-user by a non-blocking advisory lock (`_RECONCILER_LOCK_NS`); an overlapping run is a
  no-op.
- Each pass first re-embeds up to `KG_RECONCILER_EMBED_BACKFILL_PER_RUN` null-embedding entities (else they stay
  invisible to the self-join).
- Approving a proposal whose counterpart a concurrent approve already merged closes it as `superseded`, not
  `approved`.
- Scheduler `_schedule_kg_reconciler` (run_at_boot).

Routes — all `KG_VIEW`, scoped to the caller's own graph + per-proposal ownership 404:

- `/api/knowledge-graph/merge-proposals` (GET)
- `…/{id}/approve` (optional `winner_id` survivor override)
- `…/{id}/reject`
- `/reconciler/run`

Frontend: `MergeProposalsSection` + `MergeProposalCard` at the top of `/brain/review` (comparison + survivor toggle +
cross-tier warning + 5s undo toast).

## Memory→KG bridge

Phase 3, `MEMORY_KG_BRIDGE_ENABLED`, opt-in/dark by default (config); switched on in the household since 2026-07-17
(#977, `k8s/configmap.yaml`, together with `MEMORY_SUBSUME_TO_KG`). Closes "Was weiß ich über X" determinism by linking
flat memories to canonical entities. Off = retrieval/extraction byte-identical.

### 3a — `resolve_entity` made bridge-safe

Two additive, backward-compatible params:

- `create_tier` replaces the hardcoded tier-0 for the create path + the same-tier embedding search; the bridge passes
  the source memory's `circle_tier`.
- `match_entity_type` scopes exact-name/surface-form/embedding lookups to the primary `entity_type`, so an "Alice"
  person-fact never links a place of the same name.

Plus a reconciler **same-name gate**: a pair sharing a normalized name with empty/identical descriptions never
auto-merges → review, closing the conflation feedback.

### 3b — the bridge itself

- `ConversationMemoryService._bridge_subject_entity` resolves `fact`/`preference` memory subjects to
  `subject_entity_id` in the **background** extraction path (never the sync turn).
- `services/memory_bridge_backfill.py` (+ `bin/backfill_subject_entity_ids.py`, `--dry-run`/`--commit`) backfills
  existing rows — resolve-or-create at `memory.tier`, per-row atomic create+link, idempotent.

### 3c — retrieval

`memory_retrieval.retrieve` resolves query-named entities (exact word-token + surface-form, no LLM) and **unions**
their `subject_entity_id` memories into the embedding hits (similarity floor, own
`MEMORY_RETRIEVAL_SUBJECT_UNION_LIMIT`, `canonical_id` tombstone-chase, deduped) — **through the same `circle_sql`
filter** as the embedding branch (no second unfiltered path).

`GET /api/memory/by-subject/{entity_id}` (circle-filtered) backs the `/wissen` entity drawer's "Erinnerungen über
diesen Knoten"; `subject_name`/`subject_entity_id` ride on `MemoryResponse`.

## Subsume

`MEMORY_SUBSUME_TO_KG` (opt-in, aggressive): when on, decomposable `fact` memories with a subject are not stored flat
at all (they live in the KG); preferences/instructions/context stay flat.

### Per-(subject, turn) recall-loss fix (2026-06-16)

The memory- and KG-extractors were uncoordinated, so a state/attribute fact whose object isn't a named entity ("Alice
ist müde") was subsumed but produced no KG relation → silent loss.

When subsume is active the turn producer now runs ONE ordered **background** coroutine
(`services/turn_extraction.extract_structured_background`, re-exported as
`chat_handler._extract_structured_background`; spawned after the `done` frame — never delays the turn):

1. the `post_message` hooks run FIRST;
2. `kg_post_message_hook` populates a shared `captured_subjects` set with the subject names of the relations it
   actually saved this turn (KG extraction runs exactly once — no double-extract);
3. memory extraction runs with that set as `captured_kg_subjects`.

`ConversationMemoryService._should_subsume_fact` makes that per-turn set the PRIMARY subsume gate: a fact is dropped
only if its subject was captured this turn. `_subject_is_kg_representable` (the old subject-level proxy,
`MEMORY_SUBSUME_REQUIRE_KG_RELATION`) remains a fallback only for uncoordinated callers
(`captured_kg_subjects is None`). This closes the **cross-turn** residual the proxy missed (it keyed on PRIOR
relations; this keys on THIS turn's capture).

**NOT truly per-fact:** the signal is subject NAMES, not (subject, object) pairs. A single turn with two facts about
the same subject — one entity-object (relation saved) + one state/attribute (no relation) — still subsumes the state
fact. That is a narrower same-turn-same-subject residual, measured by the `mixed-same-subject-*` eval case.

Subsume off → coordination off, two independent tasks (legacy, byte-identical).

Eval `bin/run_subsume_recall_loss_eval.py --perfact`: single-fact cases LOST=0 (was 50% capture under the unguarded
surface); the same-turn-multi-fact-same-subject case still loses the state fact (documented).

**Still single-user only** — name-based capture + cross-user subject resolution/tier reach are unaddressed (see
`TODOS.md`).

## Graph expansion

Phase 4 — multi-hop retrieval, `GRAPH_EXPANSION_ENABLED`, opt-in/dark. Off = `query` byte-identical.

`services/graph_expansion.py::expand_fused` runs **post-RRF** in `PolymorphicAtomStore.query`. It takes the fused
`AtomMatch` list, finds the top `kg_node` pivots and walks `kg_relations` 1-`GRAPH_EXPANSION_MAX_HOPS` hops:

- **level-synchronous BFS** → correct min-hop distance;
- `kg_entities_circles_filter` per hop;
- per-hop frontier cap;
- **leak-safe `kg_edge`s** only when both endpoints are accessible;
- decay = pivot/(1+hop);
- cap `GRAPH_EXPANSION_MAX_EXPANDED`.

It appends provenance-marked (`payload.expanded`+`hop`) neighbour atoms, re-sorted, capped.

It is a single seam (no double-work, decay survives). The rebuild after the per-module MVP was re-deferred by
`/plan-eng-review`; the MVP is parked on `feature/structured-memory-phase4-subsume`.

~~Follow-up (`TODOS.md`)~~ **DONE (#874 via #1196):** `get_relevant_context` now runs through the same `expand_fused`
seam (`kg_retrieval.py`, "Phase 4: graph expansion"), so `internal.knowledge_search` benefits too. Original wording:
route the agent string path `get_relevant_context` onto the fused path so
`internal.knowledge_search` benefits too.

The pre-existing unfiltered `name_map` endpoint-name leak in `get_relevant_atoms`/`get_relevant_context` was fixed
2026-06-06 — both now route endpoint-name resolution through the shared `KGRetrieval._resolve_entity_names` →
`kg_entities_circles_filter`, "?" on miss, mirroring `kg_graph_service.focus`.

## Spoken turns feed the same extractors

`services/turn_extraction.py`.

Memory + KG extraction used to live INLINE in `chat_handler`, so only the BROWSER ever produced knowledge: a satellite
turn was transcribed, answered and persisted (`ollama.save_message` + a 5-exchange in-memory history) but ran neither
the `post_message` hooks nor memory extraction — anything said out loud was forgotten while the same sentence typed
was remembered.

The two coroutines plus the spawn/skip policy now live in `services/turn_extraction.py`:
`extract_memories_background` / `extract_structured_background` / `spawn_memory_extraction` /
`spawn_post_message_hooks`. `chat_handler` re-exports the two coroutines under their historical private names, so the
browser path is unchanged. `satellite_handler._spawn_satellite_extraction` calls the SAME seam right after the turn is
persisted — fire-and-forget, so the TTS is never delayed, and every failure is swallowed.

- ONE skip policy for both producers: memory flags off / empty answer / `action_success is False` → nothing scheduled.
- `TurnExtractionSpawn.owns_post_message` keeps KG extraction at exactly once per turn when subsume coordination is
  active.
- **Privacy boundary: a spoken turn is extracted ONLY when the speaker was recognized** — `sat_user_id` (Speaker →
  User via the `User.speaker_id` FK). `None` ⇒ no memory extraction AND no `post_message` hooks, because an
  unattributed voice in the room must not become somebody's memory.
- **Persistence boundary:** a turn that was never persisted (no conversation `session_id` — the same condition the
  message-save block is gated on) is skipped as well, so extraction can never produce memories attached to no
  conversation.
- Deliberate and unflagged (no kill-switch): on by default once deployed.

## The conflation fix (D9)

Memory extraction now binds each fact to a `subject_name`, retrieval carries it, and the injected context is
subject-tagged (`- [FACT · <name>] …`) so the LLM cannot conflate facts about different people.

Extraction also emits multi-type entities + tastes-as-relations and records `stated_by`.

KG-extraction eval: `bin/run_kg_extraction_eval.py` + `tests/eval/kg_extraction_eval.yaml`.
