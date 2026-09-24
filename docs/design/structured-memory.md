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

One exception since 2026-09-21 (#876 field data from a live auth-on graph: the most common person duplicate was two
characters transposed *inside* one token of a four-token name — a subset in neither direction, so the guard left it
with no path at all, and the conflation monitor excludes persons): a **typo pair** — same tokens in the same order
except one, that one differing by a single in-token edit (`_names_near_typo`, OSA distance 1, both spellings ≥ 4
characters) — survives the find-time drop as a **review proposal** with `reason=name_typo`. It is never auto-merged:
`names_related` stays False, so the gate refuses it, and `block_auto_merge` is set. The token minimum is deliberate:
the same data set held six pairs of numbered test accounts differing only in a trailing two-character ordinal —
distinct identities that must stay apart. A cross-tier typo pair is labelled `cross_tier` (the visibility change is
the invariant-bearing fact and drives the card's warning). Since the same change, a proposal the owner **rejected**
is final for the reconciler — the self-join and `_propose` exclude `pending` and `rejected` pairs alike, so a
"maybe two people" verdict is not re-asked every run; the only way back is an explicit admin merge.

**Type-guard** — added 2026-09-24 (#1330) after the xidra queue showed what the embedding cannot see. The reconciler
had no notion of entity TYPE at all; only notes were excluded. A town and the company seated in it are described out
of the same documents, so they embed far above the 0.85 candidate bar: `place "Korschenbroich"` ~ `organization
"X-Idra Systems GmbH"` measured at **0.895**, with eight such pairs pending in xidra and one in the household. The
damage is not one bad row — the review queue groups pairs into connected components, so a single cross-type edge
drags a whole cluster of company spellings into the town, where "fold all" would have merged them. Above 0.95 the
pair would have auto-merged outright: `_name_collision_low_signal` does not apply once both sides carry their own
description, and a counter-test confirmed the subset case folding automatically without the guard.

The guard mirrors the person-guard's shape: if the two sides' **primary** types disagree (`_types_compatible`), the
pair is dropped — **unless the names are related** (equal or whitespace-token-subset). That is the one shape in which a foreign type means a *mis-typed duplicate* rather than two
different things, and both live graphs hold exactly one: `person "Pontresina"` → `place "Pontresina"` and
`organization "Publikationsplattform"` → `thing "Publikationsplattform der …"`. Those survive as review proposals
(`reason=cross_type`), never auto-merges — which type is right is a human call. An unknown type on either side counts
as compatible: the guard accuses, it never guesses. Note the asymmetry it closes — the inline `resolve_entity` path
has had `match_entity_type` since the bridge (3a below); the reconciler had nothing.

**Why the scalar type and not the superset.** `entity_types` only ever grows: `merge_entities` unions both sides into
the survivor, and every re-mention folds newly observed types in. An overlap test therefore disarms itself with
exactly the usage the guard exists for — approve one legitimate mis-typed-duplicate fold and the survivor claims both
types forever after, matching everything of either kind, invisibly. The scalar `entity_type` is stable: nothing
writes it but an explicit owner edit (`update_entity`). Two consequences fall out. `thing` has to be a **wildcard**
(`_UNTYPED`): `_build_entities` assigns it when the model named no type at all, so treating it as a claim would drop
the commonest duplicate shape in an LLM graph — the same firm extracted once as `thing` and once as `organization`,
names not token-related — with no merge, no proposal and no row the owner could ever find. And the guard must not
cancel the `name_typo` exception: a typo pair is `related=False` by construction, so the drop is conditioned on
`not typo` as well.

Both find-time guards drop silently by design, which is why `ReconcileReport` carries `dropped_cross_type` and
`dropped_person_guard` — `candidates` counts survivors, so without them a guard that is too greedy on some graph
leaves no trace at all. They appear in the pass's log line and in `/reconciler/run`.

**Two limits worth knowing.** The disjointness test runs in Python, *after* the self-join's `ORDER BY similarity
DESC LIMIT` (`cap = max(KG_RECONCILER_MAX_PER_RUN * 2, 2)`, so 100 by default). Every pair the guard eats therefore
consumed a slot in that window, and a genuine duplicate ranked below it is not fetched at all — the guard shrinks the
effective per-run budget. `dropped_cross_type` is the instrument for deciding whether that matters on a given graph;
pushing the test into the SQL predicate (a safe superset, e.g. `a.entity_type = b.entity_type OR … OR the names share
a token`) is the fix if it does. Not done pre-emptively: measure first.

Second, the guard does not re-label what was already there. `find_duplicate_pairs` excludes any pair that already has
a `pending` or `rejected` proposal, so the rows that were queued before the guard landed keep `reason=gray_zone`.
That is an audit-trail fact, not a UI one — the card derives its label from the live types (above), so those rows
still read "different kinds of thing". Nothing re-writes a stored reason; a backfill would be a one-off `UPDATE` and
is deliberately not part of the guard.

**Weak edges never take part in a bulk fold** (#1333). `resolve_cluster` also refuses any pair whose `reason` is in
`KG_MERGE_WEAK_REASONS` — today just `name_typo`, "maybe two different people, one character apart". Two reasons: the
cluster card shows a count and not the two names, so a bulk fold hands exactly the judgement the reconciler refused to
a single click; and one weak edge JOINS two components that were never compared, so the weak claim would carry
everything on both sides of it. Measured on the live household graph 2026-09-24: 11 `name_typo` pairs pending, **six
of them inside a foldable cluster**. The bar reads the persisted `reason`, not `block_auto_merge` — the latter is a
find-time flag on `MergeCandidate` and never reaches the queue, which is why the "review candidate only" promise held
in `_reconcile_pass` and nowhere else.

Refusing the weak EDGE turned out not to be enough, and the adversarial review found why: dropping an edge shrinks
reachability but does not stop both of its endpoints arriving in the survivor through a third entity. A—B weak, A—C
and B—C strong: the component is still {A,B,C}, B folds into A, and `_repoint_after_fold` closes the weak A—B
proposal as `superseded`. The weak claim would have been executed and its row closed while the response still
reported `skipped_weak_edge=1` — the owner told the pair stayed pending, then finding it gone. So a weak pair with
**both endpoints inside the component refuses the whole fold**, with a note naming the two entities; the owner
decides that one pair on its own card and the cluster folds afterwards. Folding it and merely reporting it honestly
was the alternative and was rejected: a merge cannot be taken back, and this is exactly the "maybe two different
people" case.

The same hole sat one guard over and was found by verifying that fix (#1334). `_types_compatible` is deliberately
non-transitive — `thing` is a wildcard, so `organization`~`thing` and `place`~`thing` both pass while
`organization`~`place` does not. A cross-type pair could therefore be a chord too: the fold proceeded through the
`thing` in the middle and `_repoint_after_fold` superseded the cross-type proposal, folding a place into an
organization while reporting `skipped_cross_type=1`. Route-only, because the UI groups on type EQUALITY which IS
transitive — and that is precisely what made it matter, since #1330 built that bar FOR the route. The blocking check
now reads every pairwise-refused pair, not just the weak ones.

The refusal itself returns `{"code": "cluster_has_undecidable_pair", "pairs": [...], "total": n}` (the shape the
user-delete guard uses, #1328) rather than a sentence: a backend string cannot be translated, and the earlier draft
put English into a German UI. `total` is uncapped while `pairs` is capped at three, so the owner is told how many are
not listed instead of being handed three of an unstated number.

Known and not fixed: `_repoint_after_fold` rewrites a pair's endpoints but not its `reason`, and since this change
that reason is a gate rather than a label. A re-pointed pair can therefore carry a stale reason in either direction
— over-refusal (harmless) or under-refusal (the hole, reopened for that one pair). Nothing re-evaluates it, because
the self-join excludes pairs with a pending proposal.

`resolve_cluster` carries the same bar as a second invariant next to same-tier (`skipped_cross_type`), and the
frontend's `mergeClusters` refuses to chain components across differing primary types — the same test on the same
field, so view and service agree exactly. That also covers the pairs that were already pending when the guard landed.
Note what follows from that agreement: because the view groups on primary-type EQUALITY, which is transitive, a
cluster it submits can never contain a type-incompatible pair, so `skipped_cross_type` guards the ROUTE (whose
`entity_ids` are caller-supplied) rather than the click path. For the same reason the cluster card names the kind
ONCE, in its header — per row it would be the same string repeated.

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
cross-tier warning + 5s undo toast). A cluster decision can be resolved only in PART — the service leaves visibility-
changing, type-incompatible and unreachable pairs pending on purpose — so the section reads `skipped_*` and `notes`
back and says so, putting the optimistically dismissed cards back. Discarding that payload made a partial refusal
look exactly like a success: the cards vanished and the pairs sat open until the next page load. The card de-emphasises the merge button for cross-tier, `name_typo`, `cross_type`
and any pair whose primary types simply differ, and derives the displayed reason label from the live types when they
differ and the pair is not cross-tier — the rows pending from before the type guard carry `gray_zone` and would
otherwise be labelled "similar but uncertain".

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
