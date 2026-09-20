---
paths:
  - "src/backend/services/note_*.py"
  - "src/frontend/src/pages/wissen/**"
  - "src/frontend/src/pages/NotesPage.tsx"
  - "src/frontend/src/components/wissen/**"
  - "src/frontend/src/components/Note*.tsx"
---
# Notes (`note` atom) & the unified Wissen workspace

Loaded only when a note service or a Wissen-workspace / notes frontend file is read. Long form:
`docs/design/notes-atom.md` (notes), `docs/CIRCLES.md` + `docs/SECOND_BRAIN.md` (workspace, routes, pages).
Access model: `circles.md`.

## Notes — `NOTES_ENABLED`, opt-in/dark
- A note is a first-class **`note` atom_type** (`notes` table, migration `pc20260720`). `services/note_service.py`
  creates each via `AtomService.create_with_source`, **never a direct INSERT** — that is what makes notes
  circle-tiered and lets them surface in `/brain` through `polymorphic_atom_store` (an 8th RRF source).
- Retrieval `services/note_retrieval.py` is hybrid: dense halfvec cosine (`notes.embedding` + HNSW, migration
  `pc20260721`) RRF-fused with GENERATED multilingual FTS on Postgres; sqlite = LIKE fallback. Embedding on write is
  best-effort / Postgres-only, gated `notes_semantic_search_enabled`, and degrades to FTS when the embed model is down.
- **`[[links]]` ride the KG substrate** (`services/note_links.py`), not a parallel table: a note mirrors to a
  `kg_entities` row (`entity_type='note'`), each `[[Target]]` is a `note_link` `kg_relations` edge via
  `resolve_entity` + `save_relation` + the tier cascade.
- Links resolve by **EXACT title only**: `match_entity_type=True`, `use_embedding=False` — never fuzzy. For the same
  reason note entities are **excluded from the KG reconciler and the conflation tripwire** (never fuzzy-merged).
- The title is the link key → **unique per owner, SERVICE-enforced** (`NoteTitleConflict` → 409): the Postgres partial
  unique index treats a NULL (auth-off) owner as distinct, so the index alone does not hold.
- REST: `/api/notes` CRUD (owner-gated 404, `notes_enabled` gate) + `/api/notes/{id}/links`. Notes are a
  project-timeline source (`services/project_timeline.py`).
- Markdown (`components/NoteMarkdown.tsx`: `react-markdown` + `remark-gfm` + the `remarkWikilink` plugin →
  `[[Target]]` chips) renders to **real React elements — no raw HTML**, so the strict CSP holds; styling is the scoped
  `.note-md`. `components/NoteBodyEditor.tsx` = the `[[ ]]` title typeahead.

## Wissen workspace — `wissen_workspace_enabled`, off by default
- On: the corpus surfaces collapse into `/wissen` (`pages/wissen/WissenLayout.tsx`) — lens rail
  (`components/wissen/LensRail.tsx`; Übersicht · Dokumente · Graph · Erinnerungen · Fristen · Prüfen, plus the Notizen
  lens `notizen`, `note`→`notes` segment). Per-lens gating comes from the permission/feature metadata in
  `pages/wissen/lenses.ts`.
- **Off = the legacy flat nav, byte-identical.** Keep both modes working.
- Redirects when on (search + hash preserved): `/knowledge`, `/brain`, `/brain/review`, `/brain/fristen`, `/memory`,
  `/knowledge-graph` → `/wissen/*`; `/notes` → the Notizen lens and the standalone `nav.notes` entry collapses.
  `/brain/skills` and `/brain/audit` stay standalone.
- `WissenSearchBar` is lens-scoped: `?scope=lens|everything` — on Documents/Graph the query drives that lens's OWN
  inline search, otherwise a cross-corpus RRF overlay.
- `WissenDetailDrawer` edits tiers across **two id spaces**: atom UUID via `usePatchAtomTier`, `kg_node` via the KG
  int id `useUpdateKgEntityTier`. Do not mix them. A dedicated `note` branch is deferred (the generic fallback works).
- The shell must persist across lens switches: `Layout.tsx` keys `/wissen/*` on a stable content key.
- The drawer and per-entity Graph results depend on `PolymorphicAtomStore` emitting per-entity `kg_node` +
  per-relation `kg_edge` atoms (`KGRetrieval.get_relevant_atoms`) rather than one aggregated blob.
