# Cross-user / household KG entity canonicalization — design (#876)

> **Status: DESIGN DRAFT — build REDIRECTED by eng review (2026-09-02) to a co-reference
> vs. shared-ownership spike BEFORE any Phase A code. See §14. Named-circles-v2 is NOT
> cleared to build; it must first win against a `sameAs` co-reference linker.**
> Structured-Memory **Phase 5** deferred item. Today entity resolution + every merge
> path are strictly **per-user** (`resolve_entity` filters `user_id == asker OR NULL`;
> `merge_entities` refuses a cross-user pair). This doc designs how a **household** can
> share canonical entities (one "Jutta" across family members) so that household-tier
> knowledge resolves to shared nodes — **without ever letting a merge raise visibility**,
> the single hardest correctness point (`tasks/structured-memory-plan.md` §Phase 1
> "Circle-Invariante", `docs/CIRCLES.md` "Merge-Invariante").
>
> **Two operator decisions gate the build** (§10): (D-A) the shape of the shared-owner
> primitive, and (D-C) the scope of what "cross-user canonicalization" is allowed to do —
> dedup-shared-only vs. fold-private-into-shared. This doc **recommends** an answer to each
> and is explicit about why the aggressive reading is unsafe.
>
> Sibling Phase-5 issues: **#875** (bi-temporal edges) and **#877** (`external_id` linker) —
> interactions in §9. Prior authoritative design: `tasks/structured-memory-plan.md`
> §Phase 5 + "Offene Produktentscheidungen #3" + D11/D3. Access model: `docs/CIRCLES.md`,
> `docs/SECOND_BRAIN.md`.

---

## 1. Context — what "per-user" means in the code today

Three facts pin the current behaviour (all confirmed on `main`):

1. **Every `kg_entities` row has exactly one owner** (`user_id`, denormalized onto
   `atoms.owner_user_id`). `resolve_entity` (`services/knowledge_graph_service.py:442`)
   matches candidates with `or_(KGEntity.user_id == user_id, KGEntity.user_id.is_(None))`
   at every step of the cascade (exact-name `:504`, surface-form `:528`, embedding
   `_find_similar_entity`). There is **no path** by which Alice's extraction folds into a
   node Bob created.

2. **`merge_entities` hard-refuses cross-user** (`:914`): `if loser.user_id != winner.user_id:
   … return None`. The invariant it *does* enforce is tier-only:
   `merged_tier = min(winner.circle_tier, loser.circle_tier)` (`:945`) and incident
   relations recompute to `LEAST(subject, object)` (`:990`). "A merge must never raise
   visibility" is implemented purely as **tier = MIN** — which is correct *because the owner
   never changes* (same user before and after).

3. **The access filter has an unconditional OWNER branch** (`services/circle_sql.py:124`):
   `{owner_col} = :asker_param`. The owner sees **everything they own regardless of tier**.
   Everyone else reaches a row only via `public` OR an explicit grant OR **tier-reach**
   (`circle_memberships` where the asker's tier value ≤ the row's `circle_tier`, `:187`).

**A "household" today is not an object.** It is only the emergent set of pairwise
tier-2 `circle_memberships` rows (Alice is in Bob's tier-2 ring, Bob is in Alice's). There
is **no shared owner** any atom can point at. That absence is precisely why #876 is blocked:
you cannot canonicalize "one Jutta for the household" until the model can name the household
as an owner.

---

## 2. Why named-circles v2 is a hard prerequisite (not a nicety)

Suppose we naively lifted the per-user filter and let Alice's household-tier "Jutta" and
Bob's household-tier "Jutta" merge into one node. That node needs a single `owner_user_id`.
Say the survivor keeps Alice's ownership. Now look at the filter:

- **Alice** reaches the node via the **OWNER branch** — unconditionally, at *any* tier,
  including anything later attached to it at tier 0.
- **Bob** reaches it only via **tier-reach** — tier ≤ node tier.

The two contributors have **asymmetric** access to a node they both built. Worse, the
OWNER branch is tier-blind: if Alice later drops a tier-0 (self) fact onto the shared Jutta
node, Alice sees it (owner) but so does the denormalization/merge machinery treat it as
"on a household node" — and any code path that reasons "this node is tier 2, therefore Bob
may see it" now leaks Alice's tier-0 fact to Bob. **The single-owner model cannot express a
node whose access is governed symmetrically by tier for all members.**

The missing primitive is a **named circle that can OWN atoms**, plus a filter branch that,
for a circle-owned atom, drops the raw owner-equality shortcut and governs access purely by
**circle membership + tier**. Only once an atom can be owned by *the household* (not by a
member) is "shared Jutta" expressible without an asymmetry to leak through.

This is the same gap the codebase already flagged: `models/database.py:1590` — *"team_id
remains on the row (parked for v2 named-circles per Finding 1.2C)."*

---

## 3. Phase A (prerequisite) — the shared-owner / named-circle primitive

**Goal:** make a *named circle* a first-class thing that (a) has members and (b) can be the
owner of an atom, and teach `circle_sql` a symmetric, tier-only access branch for
circle-owned atoms. This phase ships **entirely independently of any KG change** — it is the
access-model foundation, testable on its own, and useful beyond the KG (shared documents,
shared notes later).

### 3.1 New tables

```
named_circles
  id             INTEGER PK
  name           VARCHAR(120) NOT NULL          # "Familie van den Bongard"
  kind           VARCHAR(32) NOT NULL           # 'household' (only kind for v2)
  created_by     INTEGER FK→users.id NOT NULL
  created_at     TIMESTAMP

named_circle_members
  circle_id      INTEGER FK→named_circles.id ON DELETE CASCADE   PK
  member_user_id INTEGER FK→users.id ON DELETE CASCADE           PK
  role           VARCHAR(16) NOT NULL DEFAULT 'member'  # 'admin' | 'member'
  member_tier    INTEGER NOT NULL DEFAULT 2   # the tier this member reads shared atoms AT
  added_by       INTEGER FK→users.id
  added_at       TIMESTAMP
```

`member_tier` lets the circle keep the ladder semantics: a shared atom carries a
`circle_tier`, and a member reaches it iff `member_tier ≤ atom.circle_tier` — the exact
analogue of the existing per-user tier-reach, but keyed on circle membership instead of a
pairwise `circle_memberships` row. For a plain household, every member has `member_tier = 2`
(household) and all household-tier atoms are mutually visible; a `member_tier = 1` member
would additionally reach the circle's tier-1 atoms.

### 3.2 Atoms can be owned by a circle

Add to `atoms`:

```
owner_circle_id  INTEGER FK→named_circles.id ON DELETE RESTRICT NULL
```

**Exactly one of `owner_user_id` / `owner_circle_id` is set** (DB `CHECK`
`(owner_user_id IS NULL) <> (owner_circle_id IS NULL)`). A user-owned atom is unchanged
(byte-identical). A circle-owned atom has `owner_user_id = NULL` and points at the circle.

> **Rejected alternative — the "synthetic user" hack.** Model the household as a fake
> `users` row and give shared atoms `owner_user_id = <household_user>`. Tempting (zero schema
> change to `atoms`/`circle_sql`) but rejected: (a) it re-introduces the exact
> **federation `peer_scoped` collision class** the filter warns about
> (`circle_sql.py:99-118`) — a raw owner-equality value that isn't a real person; (b) it
> pollutes auth/RPBAC (a "user" that can't log in); (c) the OWNER branch would still be
> tier-blind for that synthetic user, which is the very asymmetry §2 is trying to kill. An
> explicit `owner_circle_id` with a **tier-only** branch is the honest primitive.

The KG source rows also need to express circle ownership for the denormalized hot-path
filter, mirroring the existing `atom_id` + `circle_tier` pair. Add `owner_circle_id`
(nullable FK) to `kg_entities` and `kg_relations`; when set, `user_id` is NULL. (The
denormalized column is a performance mirror of `atoms.owner_circle_id`, kept in lockstep by
`AtomService` — same discipline as `circle_tier`.)

### 3.3 The fifth filter branch

`circle_sql.circles_filter_clause` gains a **circle-membership branch** and, for
circle-owned rows, **suppresses the raw owner-equality shortcut** (there is no single owner
to shortcut). Sketch of the new predicate for a KG row `e`:

```
(   e.user_id = :asker                                   -- user-owned: unchanged
 OR e.circle_tier = :asker_pub                           -- public: unchanged
 OR EXISTS(atom_explicit_grants …)                       -- grant: unchanged (user-owned only)
 OR EXISTS(circle_memberships cm … cm.value::int <= e.circle_tier)   -- per-user tier-reach: unchanged
 OR EXISTS(                                               -- NEW: named-circle tier-reach
      SELECT 1 FROM named_circle_members ncm
      WHERE ncm.circle_id = e.owner_circle_id
        AND ncm.member_user_id = :asker
        AND ncm.member_tier   <= e.circle_tier ) )
```

Properties that make this leak-safe **by construction**:

- The new branch fires **only** when `owner_circle_id IS NOT NULL`, and for such rows
  `user_id IS NULL`, so the owner-equality branch is dead — access is **purely** membership
  + tier, **symmetric across all members**. No member is privileged over another; there is
  no tier-blind owner.
- It is `peer_scoped`-safe: a federation peer is never written into `named_circle_members`
  (membership is a local-only admin action), so the branch simply never matches a peer.
  Under `peer_scoped=True` we **also drop** the circle-membership branch for the same reason
  we drop owner-equality/grants (defence in depth — a peer must reach a shared atom only if
  it is `public`).
- `AUTH_ENABLED=false` (single-user household) short-circuits the whole filter, as today —
  the feature is inert there and only matters for the multi-user (xidra-shaped) case.

### 3.4 Admin surface (Phase A)

- `POST /api/circles/named` create a household circle (creator becomes `admin`).
- `POST/DELETE /api/circles/named/{id}/members` add/remove a member (admin-gated).
- `GET /api/circles/named` list circles the caller belongs to.
- `AtomService` learns `owner_circle_id`: `create_with_source(..., owner_circle_id=…)`,
  tier-cascade unchanged (it already recomputes denormalized `circle_tier`; it now also
  carries `owner_circle_id` through).

**Phase A ships behind `NAMED_CIRCLES_ENABLED` (dark).** Flag-off ⇒ no table use, no branch
(the branch is emitted but `owner_circle_id` is always NULL, so it never matches) ⇒
byte-identical. Phase A has its **own** eng + security review before Phase B builds on it.

---

## 4. Phase B — cross-user resolution into a shared node

With shared ownership expressible, `resolve_entity` gains a **household-shared lane**,
selected **strictly by the tier of the fact being extracted**:

```
resolve_entity(name, type, user_id, create_tier=T, …, shared_circle_id=C|None)
    if C is not None and T >= TIER_HOUSEHOLD:      # household-tier (or wider) fact
        # SHARED lane: resolve against the circle's shared entities
        1. exact-name  WHERE owner_circle_id = C AND canonical_id IS NULL [AND type]
        2. surface-form WHERE owner_circle_id = C …
        3. (persons still skip embedding-match; non-persons: same-tier embedding within C)
        4. create NEW entity owned by circle C at tier T  (owner_circle_id=C, user_id=NULL)
    else:                                            # self/trusted-tier fact
        …existing per-user lane, BYTE-IDENTICAL…
```

**Where the per-user filter loosens:** only inside the shared lane, and only the *owner
predicate* — from `user_id == asker OR NULL` to `owner_circle_id == C`. Everything else
(person embedding-skip, surface-form-only person identity, gray-zone→create-new, the
per-user entity cap) is unchanged.

**Where it must NOT loosen (the sharp edge):** a **self-tier (0)** or **trusted-tier (1)**
fact **never** enters the shared lane and **never** resolves to a shared node. `T >=
TIER_HOUSEHOLD` is the gate. A private "Jutta is stressed at work" stays a per-user node,
full stop. The tier of the *fact* — carried today as `create_tier`, already threaded from
`memory.circle_tier` by the Phase-3 bridge — is the single source of truth for the
ownership target. This is the same design the plan already uses for the bridge
(`create_tier` "so a backfilled household fact doesn't mint a self-tier entity",
`knowledge_graph_service.py:472`), extended from "which tier" to "which owner".

**Who is `C`?** The speaker's household circle. Resolved once per turn from
`named_circle_members` for `stated_by_user_id` (the authenticated speaker). Ambiguity — a
user in two households — is an **open question** (§10, O-1); the recommended v2 answer is
"a household fact resolves into the *one* household the current conversation is scoped to,"
and multi-household users are out of scope for v2.

**Shared nodes are born shared.** A household-tier fact with no existing shared node creates
one owned by `C` at tier 2. There is deliberately **no automatic promotion** of an existing
private node into the shared node (that would raise visibility without consent — see §5).

**Behind `KG_CROSS_USER_CANONICALIZATION_ENABLED` (dark), which requires
`NAMED_CIRCLES_ENABLED`.** Flag-off ⇒ `shared_circle_id` is always None ⇒ the shared lane is
unreachable ⇒ byte-identical.

---

## 5. Phase C — the never-raise-visibility invariant ACROSS users (the crux)

### 5.1 State the invariant in audience terms

Let `audience(N)` = the set of users who can see node `N` under the §3.3 filter. The
existing tier-only invariant ("tier = MIN") is a *special case* of the real rule. The full
cross-user rule is:

> **INVARIANT (cross-user never-raise): for every user `U`, if `U ∉ audience(loser)` OR
> `U ∉ audience(winner)`, then `U ∉ audience(survivor)`. Equivalently
> `audience(survivor) ⊆ audience(loser) ∩ audience(winner)`.**

A merge may only ever *shrink or preserve* an audience, never enlarge it — and it must do so
for **both** inputs simultaneously, because a merge fuses two histories into one node.

### 5.2 Why "tier = MIN" is not sufficient once owners can differ

Consider folding Alice's private "Jutta" (`owner=Alice`, tier 0, `audience = {Alice}`) into
the household "Jutta" (`owner=circle C={Alice,Bob}`, tier 2, `audience = {Alice, Bob}`).
Tier = MIN(0, 2) = 0 keeps the survivor at tier 0. But if the **survivor is circle-owned**,
its audience at tier 0 is still governed by the membership branch, and *nothing* about tier
= MIN removes Bob's Alice-authored facts problem: the survivor now carries Alice's formerly
private facts on a **circle-owned** node, and any later widening of that node re-exposes
them. The intersection here is `{Alice}` — so the *only* leak-safe survivor is one visible to
Alice alone, i.e. **owned by Alice at tier 0**, which defeats the purpose (the household
Jutta would lose Bob). **Folding private-into-shared is inherently visibility-raising in one
direction and cannot be a merge.**

### 5.3 The design consequence — what a cross-user merge is *allowed* to be

The intersection invariant is satisfied **trivially and by construction** only when the two
sides already have the **same audience**, i.e. **same owner AND same tier**. Therefore:

- **Auto-merge (reconciler) is `same-owner AND same-tier` only.** For shared nodes this means
  *both nodes owned by the same circle C at the same tier* — deduping the household's own
  "Jutta" / "Jutta M." spellings. Audience is identical before and after ⇒ the invariant
  holds with zero visibility change. This is the natural extension of the existing
  same-tier-only rule; the owner equality is the new conjunct.

- **A private↔shared pair is NOT a merge.** It is a **promote-to-household** decision that
  raises the private side's visibility, so it may happen **only through explicit action by
  the owner of the private data** — never the reconciler, never another member. The channel
  already exists: the owner raises their fact/relation's tier via the existing tier-cascade
  (`AtomService.update_tier` / `PATCH /api/atoms/{id}/tier`); the promoted fact then
  **re-resolves** its subject through the Phase-B shared lane (now `T >= household`) and
  attaches to the shared node. Consent is structurally present because the owner initiated
  the tier change on their own atom (the object-level owner-guard, `docs/CIRCLES.md`
  §Object-Level Authorization, already enforces "only the owner mutates").

- **Cross-*circle* pairs** (one household's shared node vs another's) never auto-merge and
  are, for v2, **not proposed** — different circles are different audiences with no
  intersection guarantee; out of scope.

### 5.4 Proof sketch that no leak is possible

1. **Reconciler path.** The find-query and the auto-merge gate both require
   `owner_circle_id(loser) = owner_circle_id(winner)` (or both user-owned + equal `user_id`,
   the legacy conjunct) **and** `loser_tier = winner_tier`. Same owner + same tier ⇒
   `audience(loser) = audience(winner) = audience(survivor)` (the merge changes surface
   forms/mentions/embedding, none of which the filter reads). The intersection invariant holds
   with equality. ∎
2. **Cross-owner is unreachable by auto-merge** (gate refuses) and **unreachable by naive
   approval** (§5.5). The only way a private fact reaches a shared node is the owner's own
   tier promotion, which is *defined* to raise that fact's audience and is authorised by the
   owner — not a merge, so the merge invariant is not the relevant guard there (the
   object-level owner-guard is).
3. **`merge_entities` keeps tier = MIN as a belt-and-braces backstop** even though §5.3 makes
   both sides equal — so a bug that let an unequal pair through still cannot *raise* tier, and
   the new **owner-equality assertion** (`assert owner(loser) == owner(winner)`, else refuse +
   log, mirroring the existing `:914` cross-user refusal) means it cannot silently re-home.

### 5.5 Approval cannot launder a visibility raise

The review queue (Phase D) may surface a **same-circle, cross-tier** shared pair (e.g. the
household minted "Jutta" at tier 2 and, via a different code path, "Jutta" at tier 3). Here
owner is equal but tier differs, so `audience` differs. Approval routes through
`merge_entities`, which sets tier = MIN — the survivor lands at the **narrower** tier, so
`audience(survivor) = audience(tier-2 side) ⊆ audience(tier-3 side)`. The invariant holds:
approval can only ever *narrow*. A UI that offered "merge up to the wider tier" would violate
the invariant and is **forbidden** — the TierPicker on a cross-tier shared merge is clamped to
`≤ MIN`, and the wider-tier facts on the loser are re-tiered down by the existing cascade.

---

## 6. Phase D — merge / review flow (extend, don't bypass)

The issue is explicit: *extend* the existing `/brain/review` `kg_merge_proposals` queue.

- **New reason constant** `KG_MERGE_REASON_CROSS_USER` (alongside `CROSS_TIER` /
  `GRAY_ZONE`). Reserved for the *rare* legitimate cross-tier-same-circle shared pair; a
  private↔shared pair is **never** proposed (it's a promote, not a merge — §5.3).
- **Proposal scoping becomes circle-aware.** `KgMergeProposal` currently carries `user_id`
  (the single owner). Add `owner_circle_id` (nullable): a shared-node proposal is owned by
  the circle, and `GET /api/knowledge-graph/merge-proposals` returns proposals for **every
  circle the caller is a member of**, in addition to their own per-user proposals. Ownership
  gating on approve/reject becomes "caller is the per-user owner **or** an admin member of
  the proposal's circle."
- **Who approves a cross-user household merge:** because a same-circle-same-tier merge
  changes no audience (§5.4), **any member may approve** a gray-zone shared dedup. A
  same-circle **cross-tier** merge (narrows a member's reach) is gated to a circle **admin**
  (role on `named_circle_members`) — the operator decides whether that's the creator only or
  any admin (O-2). The private→shared **promote** is not in this queue at all; it is the
  owner's tier action on `/knowledge` / the Wissen drawer.
- **Frontend.** `MergeProposalCard.tsx` / `MergeProposalsSection.tsx` already render a
  cross-tier warning + survivor toggle + TierPicker + 5s-undo. Extensions: a **shared-owner
  badge** ("Haushalt: Familie …") on a circle-owned side, the cross-tier TierPicker
  **clamped to ≤ MIN** (§5.5), and the section also lists circle-scoped proposals. i18n
  de+en for the new strings. (Design mirrors the locked merge-card spec in
  `tasks/structured-memory-plan.md`.)

---

## 7. Schema / migration deltas

One additive, reversible migration per phase (alembic `transaction_per_migration`;
`CONCURRENTLY` + `autocommit_block` + `DROP INDEX IF EXISTS` for any index, à la
`pc20260528`). All columns nullable/defaulted so flag-off is byte-identical.

| Phase | Migration | Delta |
|---|---|---|
| A | `pcYYYYMMDD_named_circles` | `named_circles`, `named_circle_members` tables; `atoms.owner_circle_id` FK + `CHECK` exactly-one-owner; indexes on `named_circle_members(member_user_id, circle_id)`. |
| A | (same) | `kg_entities.owner_circle_id`, `kg_relations.owner_circle_id` (nullable, index) — denormalized owner mirror. |
| C | `pcYYYYMMDD_kg_merge_cross_user` | `kg_merge_proposals.owner_circle_id` (nullable FK); new reason value (string, no schema change). |

No backfill: existing rows keep `owner_user_id`, `owner_circle_id = NULL` → the new branch is
dead until an operator creates a circle and shares into it. The migration-roundtrip test runs
a real `alembic upgrade` on `.159` Postgres (create_all + stamp, then upgrade-verify, per the
Phase-0 note in the plan).

---

## 8. Flag-gating, phasing, ordering

| Flag | Default | Guards |
|---|---|---|
| `NAMED_CIRCLES_ENABLED` | `false` (dark) | Phase A: the tables, the filter branch use, the admin routes. Off ⇒ `owner_circle_id` always NULL ⇒ branch never matches ⇒ byte-identical. |
| `KG_CROSS_USER_CANONICALIZATION_ENABLED` | `false` (dark) | Phase B/C: the shared resolution lane + the same-owner auto-merge conjunct + the cross-user proposal. **Requires `NAMED_CIRCLES_ENABLED`** (config validator hard-fails the combination on-without-prereq, mirroring the `AUTH_COOKIE_ENABLED` validator pattern). |

**Build order (strictly sequential — all touch `circle_sql` + `knowledge_graph_service` +
the migration chain):**

**Phase A** (named-circles primitive + filter branch + admin, own review) → **Phase B**
(shared resolution lane) → **Phase C** (same-owner auto-merge conjunct + `merge_entities`
owner-equality assertion + the leak-guard tests) → **Phase D** (circle-scoped review queue +
UI + promote-to-household polish).

A parallel lane (like the plan's eval harness): the **leak-guard property test** (§9.x) can be
written against the Phase-A filter before B/C exist — it is the acceptance gate for the whole
effort and doubles as the Phase-A security review artifact.

---

## 9. TDD on `.159` — the tests that gate the merge

Real Postgres (`@pytest.mark.database`), run on `.159` (`memory/reference_test_runner_159.md`),
because the whole feature is `halfvec` + `jsonb @>` + the SQL filter — the sqlite shim can't
exercise it.

**The mandatory leak-guard (the crux test).** Construct: circle `C = {Alice, Bob}`; a
household "Jutta" owned by `C` at tier 2; Alice's private "Jutta" owned by Alice at tier 0
with a tier-0 fact `"Jutta is stressed"`. Then, for **every** mutation path:

1. `resolve_entity` for Bob's tier-0 fact about "Jutta" ⇒ creates a **Bob-private** node,
   never folds into `C`'s shared node (self-tier never enters the shared lane).
2. The reconciler **refuses** to auto-merge Alice-private-Jutta with shared-Jutta
   (owner differs) — no merge, and (per §6) **no proposal** either.
3. `merge_entities(alice_private, shared)` called directly ⇒ **refused** by the
   owner-equality assertion (returns None, logs), byte-for-byte like the current cross-user
   refusal.
4. **Property assertion (the leak):** Bob's `memory_retrieval` / `kg_retrieval` /
   `polymorphic_atom_store` query for "Jutta" **never** returns `"Jutta is stressed"`, under
   flag-on, before and after every path above. Assert `audience(survivor) ⊆
   audience(loser) ∩ audience(winner)` for every merge actually applied.
5. **Same-owner same-tier dedup IS applied** (two `C`-owned tier-2 "Jutta" spellings merge;
   surface_forms union; audience unchanged).
6. **Cross-tier same-circle approval narrows** (tier-2 ∪ tier-3 shared ⇒ survivor tier 2;
   the tier-3-only facts re-tier down; no member gains reach).

**Other required tests:** Phase-A filter unit tests (a member sees shared-tier rows, a
non-member does not, a peer never does even under `AUTH_ENABLED=false` via `peer_scoped`);
migration roundtrip; `AtomService` circle-owned create + tier-cascade keeps
`atoms.owner_circle_id` and the denormalized mirror in lockstep; flag-off byte-identical
(golden query text unchanged); route ownership gating (member vs non-member vs admin on
approve). Frontend: vitest/RTL for the shared-owner badge + clamped TierPicker + circle-scoped
list.

---

## 10. Interaction with the other Phase-5 items

- **#875 (bi-temporal `valid_at`/`invalid_at`).** A shared node accumulates relations
  asserted by *different members at different times*; `stated_by_user_id` (already on
  `kg_relations`) plus #875's validity interval together answer "who in the household believed
  X about Jutta, and when." **Constraint:** a same-owner-same-tier merge must **preserve every
  relation's validity interval** — it dedups identical *triples* but must not collapse two
  edges with different `[valid_at, invalid_at)` into one. Keep **identity (merge) ≠ validity
  (expire)** exactly as #875 insists; the merge SQL already dedups on
  `(subject, predicate, object)` — with #875 the dedup key must include the validity window, or
  the merge must expire-not-drop. Flag both efforts to coordinate the dedup key. Sequencing:
  independent, but if both land, the merge-dedup change is the shared seam.
- **#877 (`external_id` offline linker).** A **shared** household node is the natural anchor
  for a world identity ("Michael Jackson" → Wikidata QID). Once populated, `external_id`
  becomes an **additional, high-precision resolution + dedup key** for the shared lane: two
  shared spellings that carry the *same* `external_id` are a confident same-owner merge (no
  embedding gray-zone). Cross-user canonicalization should **land first** (it creates the
  shared node the linker enriches); the linker then keys onto the canonical shared row rather
  than N per-user duplicates — strictly reinforcing, additive, no conflict.

---

## 11. Risks & open questions

**Risks**

- **R1 — a wrong owner-target leaks (highest).** If `create_tier` is ever mis-derived (a
  self-fact tagged household), a private fact mints on the shared node. Mitigation: the tier
  gate is the *only* selector, it reuses the already-audited `create_tier` threading, and the
  leak-guard property test (§9.4) is the acceptance gate. The `merge_entities` owner-equality
  assertion is the backstop.
- **R2 — filter-plan regression.** A fifth OR-branch on every KG/memory/doc query. Mitigation:
  the branch is emitted but **dead** (`owner_circle_id IS NULL`) whenever the flag/feature is
  off, and `EXISTS(named_circle_members …)` is indexed on `(member_user_id, circle_id)`.
  Verify with `EXPLAIN` on `.159` that flag-off plans are unchanged.
- **R3 — denormalization drift.** `owner_circle_id` now joins `circle_tier` as a denormalized
  mirror that must not diverge from `atoms`. Mitigation: same `AtomService`-only write
  discipline + CI lint that already guards direct source-table INSERTs (`docs/CIRCLES.md`
  Anti-Patterns).
- **R4 — federation collision.** The new branch must be `peer_scoped`-dropped, and
  `named_circle_members` must never receive a `PeerUser.remote_user_id`. Mitigation: explicit
  `peer_scoped` suppression + a test that a peer can't reach a shared-tier atom.
- **R5 — promote-to-household UX gap.** If the only channel for "bring my private Jutta into
  the household" is a raw tier bump, users may not discover it, and shared nodes stay sparse.
  Not a *safety* risk (fail-safe: knowledge stays private), but a value risk — flag to the
  operator whether a dedicated "share with household" affordance is worth Phase D.

**Open questions (need the operator)**

- **D-A (blocking, Phase A shape).** Confirm the **explicit `owner_circle_id`** primitive over
  the synthetic-user hack (§3.2 recommends explicit). This is a one-way door — it sets the
  filter shape for every future shared-atom type.
- **D-C (blocking, scope).** Confirm cross-user canonicalization = **dedup already-shared
  nodes only**, with private→shared handled by owner-initiated tier promotion (§5.3). The
  aggressive reading ("let the reconciler fold private into shared") is **provably
  visibility-raising and is refused** — confirm we are *not* building that.
- **O-1 — multi-household users.** A user in two households: which circle does a household-tier
  fact resolve into? Recommended v2 answer: the conversation's scoped household; multi-household
  is out of scope. Operator to confirm households are disjoint for v2.
- **O-2 — approval authority.** Any member vs. circle-admin vs. creator-only for (a) same-tier
  dedup and (b) cross-tier narrowing (§6). Recommended: any member for (a), admin for (b).
- **O-3 — where do shared nodes get created?** Only on a household-tier extraction (§4), or
  also proactively when a circle is created / a document is shared into it? Recommended:
  lazily, on first household-tier fact — no eager backfill.
- **O-4 — relationship to existing pairwise `circle_memberships`.** A household is today an
  emergent set of pairwise tier-2 rows. Do named circles **replace** those (a migration that
  folds mutual tier-2 memberships into a `named_circle`) or **coexist**? Recommended: coexist
  for v2 (named circles are additive; the pairwise rows keep governing user-owned atoms),
  revisit consolidation later. This is a modelling decision the operator should weigh.

---

## 12. Files to create / modify (by phase)

**Phase A — named-circles primitive**
- `alembic/versions/pcYYYYMMDD_named_circles.py` — new tables + `atoms.owner_circle_id` +
  `CHECK` + KG-row `owner_circle_id` columns/indexes. *(new)*
- `src/backend/models/database.py` — `NamedCircle`, `NamedCircleMember` models;
  `Atom.owner_circle_id`; `KGEntity.owner_circle_id`, `KGRelation.owner_circle_id`. *(modify)*
- `src/backend/services/circle_sql.py` — the named-circle tier-reach branch + `peer_scoped`
  suppression. *(modify — the single highest-leverage, highest-risk edit)*
- `src/backend/services/atom_service.py` — `owner_circle_id` on `create_with_source` /
  `upsert_atom`; tier-cascade carries it. *(modify)*
- `src/backend/services/circle_resolver.py` — `can_access_atom` learns circle-owned rows.
  *(modify)*
- `src/backend/api/routes/circles.py` (or new `named_circles.py`) — create/list/add/remove
  member routes, admin-gated. *(new/modify)*
- `src/backend/utils/config.py` — `named_circles_enabled`; validator that
  `kg_cross_user_canonicalization_enabled ⇒ named_circles_enabled`. *(modify)*

**Phase B — shared resolution lane**
- `src/backend/services/knowledge_graph_service.py` — `resolve_entity` shared lane
  (`shared_circle_id` param + tier gate); the create path mints a circle-owned entity.
  *(modify)*
- the extraction caller (`kg_post_message_hook` / `chat_handler`) — resolve the speaker's
  household circle once per turn and pass `shared_circle_id`. *(modify)*
- `src/backend/utils/config.py` — `kg_cross_user_canonicalization_enabled`. *(modify)*

**Phase C — merge/invariant**
- `src/backend/services/knowledge_graph_service.py::merge_entities` — owner-equality
  assertion (accept same-`owner_circle_id`); tier = MIN retained as backstop. *(modify)*
- `src/backend/services/kg_reconciler_service.py` — the find-query + auto-merge gate add the
  **same-owner** conjunct; cross-owner never proposed. *(modify)*
- `alembic/versions/pcYYYYMMDD_kg_merge_cross_user.py` — `kg_merge_proposals.owner_circle_id`.
  *(new)*
- `src/backend/models/database.py` — `KG_MERGE_REASON_CROSS_USER`;
  `KgMergeProposal.owner_circle_id`. *(modify)*

**Phase D — review UI**
- `src/backend/api/routes/knowledge_graph.py` — merge-proposals list returns circle-scoped
  proposals; approve/reject gating (member/admin). *(modify)*
- `src/frontend/src/components/MergeProposalCard.tsx` / `MergeProposalsSection.tsx` —
  shared-owner badge, clamped-to-MIN TierPicker, circle-scoped list. *(modify)*
- `src/frontend/src/i18n/locales/{de,en}.json`. *(modify)*

**Tests (throughout)**
- `tests/backend/test_named_circles_filter.py`, `test_kg_cross_user_resolution.py`,
  `test_kg_cross_user_merge_leak_guard.py` (the crux), migration-roundtrip, route gating;
  `tests/frontend/react/` for the card. *(new)*

**Docs (Doc-Gate before merge)**
- `docs/CIRCLES.md` (the fifth branch + named circles), `docs/SECOND_BRAIN.md`, `CLAUDE.md`
  (Circles section + Structured Memory), `docs/FEATURES.md`, `docs/ENVIRONMENT_VARIABLES.md`
  (the two flags), and this doc's Status → LOCKED.

---

## 13. Summary

The build is gated on a genuine access-model gap, not just KG plumbing: **there is no owner a
shared entity can point at.** Phase A adds that primitive (a named circle that owns atoms +
a symmetric tier-only filter branch) and is independently useful and independently reviewable.
Phases B–D then canonicalize **only what is already shared**, and route **private→shared**
exclusively through owner-initiated tier promotion — never a reconciler merge — because
folding private into shared is *provably* visibility-raising and therefore cannot be a merge.
The one invariant that carries the whole design is `audience(survivor) ⊆ audience(loser) ∩
audience(winner)`, enforced by the **same-owner-AND-same-tier** auto-merge gate, backstopped
by the `merge_entities` owner-equality assertion, and guarded by a mandatory `.159` leak-property
test. Two operator decisions (the primitive's shape, the scope of "canonicalization") must be
confirmed before Phase A starts.

---

## 14. Engineering review outcome (2026-09-02, `/plan-eng-review`)

**Verdict: build BLOCKED. Redirected to a design spike.** The review (4 architecture
findings + a same-family outside voice) surfaced that the whole shared-ownership
prerequisite may be over-built for #876's actual goal, and that no current deployment
would even exercise it. Before any `circle_sql` edit, run a short **co-reference vs.
shared-ownership** comparison design and let it decide whether Phase A is built at all.

### 14.1 The redirect (the load-bearing decision)

`#876`'s real goal is: *Alice and Bob resolve/retrieve the SAME "Jutta".* The outside
voice argues that a **`sameAs` co-reference edge** achieves this with **zero** ownership
change, **zero** new SQL branch, **zero** rollback hazard, and **zero** new leak surface —
each per-user node stays user-owned and independently filtered by the EXISTING
`circle_sql`; retrieval / `graph_expansion` expands across the link. This is essentially
the **#877 `external_id` linker**, so the spike also weighs swapping #877 before #876.

Two facts make the redirect urgent (do not skip the spike):
- **No live consumer runs shared-ownership** (OV-8): household is `AUTH_ENABLED=false` →
  `circle_sql` short-circuits → feature inert; xidra is auth-on but a *business* instance
  (no "Familie", `kind='household'` only); voice/unidentified turns have `user_id=None` →
  no `stated_by` → no `C`. Question the sequencing against #875/#877 first.
- **The riskiest edit in the codebase** (the doc's own words: `circle_sql`, §12) is being
  spent on shared ownership when co-reference likely satisfies the issue.

**The spike deliverable:** a data-driven `sameAs`-link vs. shared-ownership comparison
covering retrieval quality (does link-expansion answer "was weiß ich über Jutta" as well
as a merged node?), leak surface, rollback, and the `#875/#877` interaction — a `≤1`
design cycle that can eliminate Phase A/B/C/D entirely.

### 14.2 If shared-ownership still wins the spike — the accepted hardening

These four architecture findings were confirmed against the code and their fixes ACCEPTED;
they are prerequisites for the shared-ownership path IF the spike keeps it:

1. **Household-tier selector is missing in the main path.** `knowledge_graph_service.py:1121`
   (`extract_and_save`) calls `resolve_entity(...)` with no `create_tier`; `:560`
   `default_tier = 0 if create_tier is None`; `:810` relation tier = `MIN`. Every chat-extracted
   fact is tier 0, so Phase B's `T >= household` gate never fires → zero shared nodes.
   **Fix (accepted):** an explicit `household_tier_derivation` component in Phase B threading
   `create_tier` into `extract_and_save`; conservative signal (attach to an already-C-owned
   subject = audience-neutral; mint a NEW shared node only on an explicit household signal,
   never a silent heuristic on private chat). This selector IS the R1 leak surface → the
   §9.4 leak-guard tests it directly.
2. **Reconciler is blind to circle-owned nodes.** `kg_reconciler_service.py:176`
   (`user_id IS NOT NULL`), `:206/:209` (`a.user_id=b.user_id ... WHERE a.user_id=:uid`) →
   circle nodes (`user_id NULL`) never enumerated → the only permitted auto-merge
   (same-circle dedup) never fires. **Fix (accepted):** add a per-circle scan dimension
   (`list_active_circle_ids` + `find_duplicate_pairs_for_circle`, self-join
   `a.owner_circle_id=b.owner_circle_id=:cid AND a.circle_tier=b.circle_tier`) with a NEW
   advisory-lock namespace (clash-free vs `0x4B47/0x4F42/0x4F43/0x4F44/0x5341/0x5354/0x4444`);
   the scheduled-task builtin enumerates users AND circles.
3. **The 5th branch changes SQL for all 6 `circle_sql` consumers, but §9 tests only KG.**
   `circle_sql.py:37` is shared by knowledge_tool / memory_retrieval / rag_retrieval /
   rag_service (docs) / note_retrieval / KG. **Fix (accepted):** per-consumer named-circle
   filter test (member sees / non-member doesn't / peer never) + a flag-off byte-identical
   golden-SQL test per consumer. Also (OV-2) pin an explicit invariant + test that each
   *legacy* branch is dead on circle-owned rows — today it is dead only by `NULL = x`
   accident (`:134` owner-fallback, `:190` pairwise tier-reach), undocumented and unpinned.
4. **Multi-household routing ambiguity.** Decision (accepted): bind `C` to a new
   `conversations.scoped_circle_id` (nullable FK, **`ON DELETE SET NULL`**), NOT the speaker's
   membership set — multi-household/multi-mandant is real (xidra teams overlap). Unbound
   conversation → `NULL` → per-user lane, byte-identical. **Guard (required):** the tier
   derivation must fail-closed to the per-user lane when the speaker ∉ `members(C)` (binding
   ≠ membership). **Caveat (OV-4):** without an in-phase UI that SETS `scoped_circle_id`, the
   default-NULL binding makes the feature inert AND mints per-user tier-2 duplicates the
   reconciler can never merge with circle nodes — so the scope-setting UX is **in-phase
   required**, not deferred.

### 14.3 Outside-voice risk register (spike inputs — resolved by choosing co-reference OR by
building the fix if shared-ownership wins)

- **OV-1 — the proof models entities, but facts live on relations (edges).** A mixed-ownership
  edge (circle-owned subject, tier-0 author) has no defined owner/tier; circle-owned at
  `tier=LEAST(0,2)=0` with dead owner-equality → reachable by no one incl. the author (silent
  loss). §5 must be restated over *relations*, not entities, and define mixed-owner-edge
  ownership. **HIGH.**
- **OV-5 — "additive/reversible" (§7) holds only pre-use.** Once atoms carry `owner_circle_id`
  (`owner_user_id NULL`), flag-off makes the 5th branch dead → those atoms visible to no one,
  and the `CHECK` blocks nulling the circle id. Real rollback = a documented **un-share data
  migration** (re-home circle-owned atoms to a member owner) BEFORE flag-off, not
  `alembic downgrade`. **HIGH.**
- **OV-6 — read-time coherence private↔shared is undesigned.** Household holds shared-Jutta +
  Alice-private-Jutta + Bob-private-Jutta at once; "was weiß ich über Jutta" must union shared
  ∪ asker-private and `graph_expansion` treat them as one person. Co-reference solves this by
  construction; shared-ownership needs a new union step. **MEDIUM.**
- **OV-3 — `member_tier` (§3.1) contradicts the "symmetric across all members" claim (§3.3).**
  Differing `member_tier` ⇒ differing audiences ⇒ "no member privileged" is false. For v2,
  **drop `member_tier`** (YAGNI — a plain household is all tier 2), which shrinks the proof
  surface and makes symmetry true. **MEDIUM.**

### 14.4 Recommended next action

Run the co-reference vs. shared-ownership spike (`/office-hours` scope) with §14.1's
deliverable. Only if shared-ownership wins does Phase A start, and then with §14.2's four
fixes and §14.3's risk register folded in from the first commit.
