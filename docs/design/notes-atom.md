# Notes as a 5th atom_type — design (Phase 4B)

> Status: **DESIGN LOCKED, build not started.** Decisions: Notes are a **first-class
> atom** (circles + polymorphic RRF + `/brain`/`/wissen`), not a parallel model; and
> `[[links]]` use the **KG substrate** (§9 Option A) — note ↔ `kg_entities(entity_type="note")`,
> link ↔ `kg_relations(predicate="note_link")`, **note→note for the 4B.2 MVP**, note→any-KG-entity
> deferred to v2. This doc resolves the data model, link semantics, retrieval integration,
> and phasing. Builds on the atom/circle machinery (`docs/CIRCLES.md`, `docs/SECOND_BRAIN.md`)
> and the Projects Phase-4A timeline. Parked-feature origin: `TODOS.md` "Notes Feature Design Doc".

## 1. Goal & scope

Hand-authored **atomic notes**: a markdown editor, `[[bidirectional links]]` between
notes, project scoping, and — because a note is an atom — circle-tiered access,
semantic retrieval in the unified store, and a badge on `/brain` / a lens in
`/wissen`. The substrate for written minutes, decisions, and cross-links
("decision X → meeting Y → client Z").

**In scope (4B):** the `note` atom type end-to-end (model → CRUD → circles → RRF →
`/brain` + a Notizen lens → markdown editor), `[[link]]` parse-and-resolve with
backlinks, project scoping (notes join the 4A timeline as a 5th source).
**Out of scope (v2, noted):** collaborative editing, note version history, an
outliner mode, importing an existing markdown vault.

## 2. Data model

New `notes` table (mirrors the `document_facts` NOT-NULL-`atom_id` atom-source
shape, `pc20260601_document_facts.py`):

```
notes
  id             BIGINT PK
  atom_id        VARCHAR(36) NOT NULL FK→atoms.atom_id ON DELETE CASCADE   # first-class atom
  owner_user_id  INTEGER FK→users.id ON DELETE SET NULL                    # direct owner
  circle_tier    INTEGER NOT NULL DEFAULT 0                                # denormalized mirror of atoms.policy.tier
  project_id     INTEGER FK→projects.id ON DELETE SET NULL, NULL           # optional project scope (4A synergy)
  title          VARCHAR(255) NOT NULL
  body           TEXT NOT NULL DEFAULT ''                                  # markdown source
  embedding      Vector(EMBEDDING_DIMENSION) NULL                          # dense retrieval (halfvec HNSW in migration)
  search_vector  tsvector GENERATED                                        # lexical retrieval (FTS)
  created_at / updated_at
  UNIQUE(owner_user_id, lower(title))    # title is the [[link]] key → unique per owner
```

- **`atom_id` NOT NULL** → the writer uses `AtomService.create_with_source` +
  `finalize_source_id` (the `document_facts` placeholder dance), so a note is born
  as an atom in one transaction. Never an orphan.
- **Both `embedding` and `search_vector`** — a note has real prose, so it earns
  dense semantic retrieval (like memories/chunks) AND lexical (like facts). Embedded
  via the existing `get_embed_client()` / `settings.ollama_embed_model` call on
  title+body at save. HNSW `halfvec(2560)` index created in the migration (the
  `document_chunks` pattern), FTS `search_vector` GENERATED (the `messages`/facts
  pattern).
- `title` is the `[[link]]` target key — unique per owner, case-insensitive.

### Atom registration (mechanical, per the integration map)
1. `ATOM_TYPE_NOTE = "note"` + `Note` ORM in `models/database.py`.
2. `"note": "notes"` in `AtomService._table_for_atom_type`; `"note_id"` in
   `_source_id_for`.
3. `notes_circles_filter(asker_id, alias="n", *, peer_scoped=False)` in
   `circle_sql.py` — direct-owner shape (mirrors `kg_entities_circles_filter`).

## 3. `[[bidirectional links]]`

**Parse** (`services/note_links.py`): extract `[[Target Title]]` tokens from the
body at save (a small regex, `\[\[([^\]]+)\]\]`, dedup, strip). No existing parser
in the repo — net-new but trivial.

**Resolve + store** — this is the fork (**§9**). The recommended path (**Option A —
KG substrate**):

- Each note is mirrored to a `kg_entities` row via
  `resolve_entity(name=title, entity_type="note", user_id=owner,
  create_tier=note.circle_tier, match_entity_type=True)`. `entity_type="note"` +
  `match_entity_type=True` scopes resolution to note-typed entities, so a note
  titled "Bonn" never collides with the place entity "Bonn". Requires adding
  `"note"` to `KG_ENTITY_TYPES` (or carrying it in `entity_types` multi-type with a
  generic primary).
- Each `[[Target]]` becomes a `kg_relations` row `subject=this note's entity,
  predicate="note_link", object=resolve_entity(Target, "note", …)`. A dangling
  `[[Target]]` auto-creates a stub note-entity (Obsidian's "create on link") — and
  optionally a stub `notes` row, or just the entity until the note is written.
- **Backlinks** = query `kg_relations WHERE object = this note's entity AND
  predicate='note_link'` (circle-filtered).

**Why Option A:** it reuses `resolve_entity` (dedup, surface-forms, dangling-stub),
the tier cascade (`kg_node`→incident-relations MIN(), already in
`AtomService.update_tier`), **graph_expansion multi-hop traversal**
(`GRAPH_EXPANSION_ENABLED` already walks `kg_relations`), and the 3D
`/wissen/graph` render — the graph-of-notes is the KG, for free. This is the
"KG-as-brain" direction the polymorphic store already states.

**Sync on edit/delete:** re-parse on every save; diff the link set → add/remove
`note_link` relations; on note delete, the `atoms` CASCADE + a KG cleanup drops the
note-entity + its relations (mirror `merge_guard`/tombstone patterns).

## 4. Retrieval integration (RRF)

- **`services/note_retrieval.py`** (`NoteRetrieval(db).search(query, *, asker_id,
  top_k, enforce_circles=False) -> list[dict]`), mirrors
  `DocumentFactRetrieval`: a dense branch (embedding cosine) UNION a lexical branch
  (`search_vector @@ plainto_tsquery`), both through `notes_circles_filter`, RRF-or-
  score-merged, returns `{id, atom_id, circle_tier, title, snippet, score}`.
- **`PolymorphicAtomStore.query`**: add `notes_task` to the `asyncio.gather`, a
  `_wrap_note_results` → `atom_type="note"` `AtomMatch` (real `atom_id`), append to
  `source_lists`. No route change — `/api/atoms` is generic over the fused output.

## 5. Circles / tier

Notes are directly owned + circle-tiered exactly like KG entities: `circle_tier` on
the row, `atom_id` for grants, `notes_circles_filter` = the 4-branch OR. Tier edit
via the `/wissen` drawer's atom-UUID `usePatchAtomTier` path (a note is a real
atoms row, not a synthetic `kg_node:*` id). `AtomService.update_tier`'s generic
single-row UPDATE covers the note; **if Option A**, the note-entity's tier cascades
to `note_link` relations via the existing `kg_node` branch.

## 6. Project scoping + timeline synergy

`notes.project_id` (nullable). A note scoped to a project becomes a **5th source on
the 4A project timeline** (`services/project_timeline.py` gains a `notes` branch,
stamped `updated_at`, kind `"note"`). Trivial extension of the merge; a nice payoff
of doing 4A first.

## 7. Frontend

- **`AtomType` union** (`brain.ts`) + `ATOM_TYPE_COLORS` (`BrainPage.tsx`, TS forces
  it) + i18n `circles.atomType.note` (de/en/it) + a **Notizen `LensDef`**
  (`wissen/lenses.ts` already hints "Notizen keeps Brain icon") + a `note` branch in
  `WissenDetailDrawer`.
- **Notes surface**: a `/notes` page (list + create) OR a lens-native list in
  `/wissen`. Editor = a **textarea markdown source + a rendered preview**
  (reuse the existing chat markdown renderer; no new heavyweight editor dep — CSP is
  strict, everything self-contained). `[[link]]` autocomplete (typeahead over the
  owner's note titles) is a v2 polish; MVP renders `[[Title]]` in preview as a link
  to the target note (or a "create" affordance if it's a dangling stub).
- Backlinks panel on a note detail: "Verlinkt von" (linked-from) list.

## 8. Migration + phasing

Migration `pc2026XXXX_notes` chains off `pc20260719_project_links`. Idempotent
(inspector-guarded), HNSW `halfvec` + FTS + `atom_id`/`(project_id,circle_tier)`
indexes.

**Phasing (separate PRs):**
- **4B.1 — note atom core:** `notes` table + atom registration + `circle_sql` +
  `NoteRetrieval` + polymorphic store + `/api/notes` CRUD + `/brain` badge. Notes
  are searchable + tiered. NO links yet.
- **4B.2 — `[[links]]`:** parser + resolve (Option A or B) + backlinks + timeline
  5th source + `/wissen/graph` note nodes.
- **4B.3 — editor polish:** markdown preview, `[[ ]]` typeahead, backlinks panel,
  Notizen lens.

**Gate:** `notes_enabled` (opt-in/dark), like every other feature.

## 9. `[[link]]` storage — DECIDED: Option A (KG substrate), note→note for MVP

| | **A — KG substrate ✅ CHOSEN** | **B — dedicated `note_links` table** |
|---|---|---|
| Storage | note ↔ `kg_entities(entity_type="note")`; link ↔ `kg_relations(predicate="note_link")` | `note_links(source_note_id, target_note_id, target_title)` |
| Reuses | `resolve_entity` dedup/stub, tier cascade, **graph_expansion multi-hop**, 3D `/wissen/graph` | nothing — self-contained |
| New code | widen `KG_ENTITY_TYPES` + link-sync on save | a table + backlink queries |
| Risk | note-identity rides KG-entity identity (closed by `match_entity_type` + `entity_type="note"`) | duplicates resolve/stub logic; no graph traversal without extra wiring |
| Fit | matches the stated "KG-as-brain" direction | simpler isolation, less ambitious |

Sub-fork (only if A): should `[[Target]]` resolve **note→note only** (MVP, clean),
or **note→any KG entity** (a note's `[[Bonn]]` links to the place entity — richer
"second brain", but re-opens the collision handling)? Recommend **note→note for
4B.2**, note→any-entity as a documented v2.

## 10. Docs to sweep (PR-lifecycle gate)

`docs/CIRCLES.md` (atom-shape example — also fix the stale `"document_chunk"` string
values), `docs/SECOND_BRAIN.md` (the "vier Informationsarten" table is stale — add
`note`, and while there, the missing `document_fact`/`procedural_skill` rows),
`docs/FEATURES.md`, `CLAUDE.md`.
