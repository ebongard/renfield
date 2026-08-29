# Proper document search on /wissen/dokumente (+ /knowledge)

**Goal:** typing a query (omnisearch on `/wissen/dokumente`, or the search box on
`/knowledge`) returns **ranked document ROWS** — reusing the existing row + all
its actions ("An Simba senden", reindex, tier, delete) — via a **server-side
hybrid (RRF) search** over name + Schicht-A facts + chunk content. Fixes the
>100-newest reachability cap (search runs over the whole KB, not the 100-window).
Keep the semantic chunk snippets as a **secondary "Textstellen" section**.

Decisions (agreed): **full hybrid up front**; **keep chunk search as a secondary section**.

## Backend

- [ ] **Migration `pcXXXX_documents_fts`** — add GENERATED STORED `search_vector`
      tsvector on `documents` over `generated_title + title + filename`
      (multilingual union across `FTS_LANGUAGES`, mirror `notes`/`messages`) + a
      GIN index. Idempotent (`IF NOT EXISTS`); existing rows populate at ALTER.
      Model: add the column to `Document` (create_all parity, guarded like notes).
- [ ] **`services/document_search.py::search_documents(db, query, asker_id,
      knowledge_base_id, status, limit, offset) -> list[Document]`** — hybrid RRF:
  - Name list: `documents.search_vector @@ websearch_to_tsquery` (`ts_rank`) +
    ILIKE fallback on the 3 name columns (partial tokens like "Arkad").
  - Facts list: `DocumentFactRetrieval.search(query, asker_id)` → document_ids
    (already circle-filtered).
  - Chunk list: `RAGRetrieval` semantic (circle-filtered) aggregated to the best
    chunk per document_id (reuse the existing chunk search path).
  - Fuse the three ranked id-lists with **RRF** (`k = rag_hybrid_rrf_k`), sum
    scores, sort desc, take `limit`/`offset`.
  - **Circle-correct visibility for ALL signals** (apply `circle_sql` document
    filter to the name signal too, so search can't widen beyond what the user may
    see; auth-off short-circuits). Note: the browse list's non-circle-filtering is
    a separate pre-existing concern — out of scope.
  - Fetch `Document` rows for the fused ids in order; `_doc_to_response_kwargs` +
    `_augment_with_progress` → `DocumentResponse`.
- [ ] **Route** `GET /api/knowledge/documents`: add `q: str | None` (keep
      `limit`/`offset`). `q` present → `search_documents` (ranked); else the
      existing recency list. Same `DocumentResponse` output. Same KB-ACL gate.
- [ ] Optional: expose a `matched` hint or `rank` if useful for the UI (else omit).

## Frontend

- [ ] `api/resources/knowledge.ts`: `DocsFilter` gains `q`; `fetchDocuments`
      passes `params.q`; list query key includes `q`.
- [ ] `KnowledgePage.tsx`: debounce `searchQuery` (standalone) + `omniQ`
      (embedded, scope=lens) → feed the **document-list** `q`. The list becomes
      the ranked result set of document rows (all actions intact). Warm empty
      state on no match. At `scope=everything` defer to the omni overlay (unchanged).
- [ ] Keep the semantic chunk search as a **secondary collapsible "Textstellen"**
      section (the current snippet cards), below the ranked document rows —
      driven by the same query, best-effort.
- [ ] i18n (de/en/it) for the new labels/empty state.

## Tests

- [ ] Backend (real-PG): FTS migration applies; name match ranks a title-term doc
      to the top; facts + chunk signals contribute; RRF ordering; circle/ACL
      (a non-owner can't surface a restricted doc); >100-doc reachability (a doc
      outside the 100-newest window is found by name). Empty query → recency list.
- [ ] Frontend: list re-queries on `q`; renders document rows (not just chunks);
      the row actions still work; the secondary Textstellen section renders.

## Deploy / rollout
- [ ] Branch → build+tests on .159 → review → docs sweep → deploy xidra
      (migration via the alembic job, namespace-stripped) + browser E2E ("Arkadon"
      → doc #34 top of list, "An Simba senden" clickable).

## Federated search across document MCP sources (point 1)

The document search should also span **searchable document MCP servers**, not just
the local KB. Audit of connected MCPs:
- **Paperless** — `mcp.paperless.search_documents` (full-text archive search). PRIMARY external source.
- **Simba** — `list_transfers(contains=…)` now does **server-side** search (MCP v1.0.8). Include as a
  federation source (user-confirmed) — surfaces "already transferred to the tax accountant" hits.
- **Filesystem** — only `mcp.files.read_file` (NO content-search tool) → not federatable for search.
- **SearXNG** — `mcp.search.web_search` = WEB metasearch only; it has no index of / connector to
  the internal document sources, so it is NOT the federation layer (point 2 answer). Federation is
  renfield-side (fan-out + RRF), mirroring `PolymorphicAtomStore`. SearXNG stays web-search.

Architecture:
- [ ] **`services/federated_document_search.py`**: (a) run the local hybrid `search_documents`;
      (b) fan out **in parallel** to each registered searchable doc MCP (Paperless
      `search_documents`) via `mcp_manager`, with a per-call timeout (best-effort — a slow/down
      MCP never blocks local results); (c) normalize each source to a common `DocSearchHit`
      {source, source_id, title, snippet, date, ref/url, dedup_key}; (d) **dedup** across sources
      (folder-ingest files KB docs INTO Paperless, so most KB docs are ALSO in Paperless — dedup by
      checksum/title+date, prefer the local KB hit which has full row actions); (e) merge/rank.
- [ ] **Result model:** local KB hits = full `DocumentResponse` rows (all actions). External hits =
      a distinct shape with source-appropriate actions (Paperless: "In Paperless öffnen" /
      "In die Wissensbasis importieren"). Decision needed (see below): unified-ranked-with-source-
      badges vs primary-local-rows + secondary-per-source-sections.
- [ ] **Registry:** which MCP+tool is a "searchable document source" should be config/registry-driven
      (a small mapping), so a new document MCP = config, not code — consistent with the platform ethos.

### Access-control note (must resolve in review)
The local KB search is **circle-filtered**. Paperless has **no circle/tier concept** — its
`search_documents` returns the whole shared archive. Surfacing Paperless hits could expose documents
a user shouldn't see under circles (esp. xidra multi-user). Options: (i) gate external-source federation
behind a permission/flag; (ii) only federate for admins/owners; (iii) accept archive-wide visibility as
intended for the business instance. MUST be decided before shipping external federation.

### Open design questions (for the eng review)
1. Unified ranked list (source badges, heterogeneous actions) **vs** primary local rows + secondary
   per-source sections. The row-actions divergence argues for sections; a single ranked list is nicer UX.
2. Dedup key across KB↔Paperless (checksum? title+date? Paperless doc id stored on our `documents`?).
3. External-source access control (the circle vs archive-wide tension above).
4. Phasing: ship **local hybrid first** (solves the immediate "Arkadon" need), then add Paperless
   federation as a second PR — or build both together?
5. Latency/UX: external fan-out is async — stream local rows first, append external hits as they arrive?

## Out of scope (follow-ups)
- A paginated / "Mehr laden" BROWSE (no-query) beyond 100 newest — search fixes the
  immediate reachability; browse pagination is separate.
- Making the browse list itself circle-filtered (pre-existing over-broad behavior).

## Eng review outcome (plan-eng-review)

**Decisions locked:**
- **D1 Scope split:** ship **PR1 = local hybrid** (name+facts+chunks RRF → document rows +
  >100 reachability + Wissen integration). **PR2 = MCP federation** (Paperless + Simba), after
  resolving access-control + dedup deliberately. Keeps the safe fix off the risky federation's
  critical path (incremental / low blast radius).
- **D2 Search ACL:** **circle-filter ALL search signals** (name signal too, via `circle_sql`) —
  search is circle-correct, never widens visibility. Browse-list's non-circle-filtering stays a
  separate pre-existing concern (out of scope). For the owner it's identical (sees own docs).

**Bake-in recommendations (no separate decision needed):**
- **DRY:** reuse a shared RRF-fuse helper (don't reimplement the atom-store's RRF); factor the
  multilingual `to_tsvector` union into one helper reused by the new documents FTS + notes/messages
  (they currently repeat it). Explicit > clever.
- **Perf:** the ILIKE name-fallback is a seq scan (no trigram index). Fine at ≤ a few-thousand
  docs/KB (xidra = 401); if a KB grows large, add a `pg_trgm` GIN index on
  generated_title/title/filename. Note in code, don't build now.

### Test coverage (target 100% of new paths)
```
BACKEND
[+] migration pcXXXX_documents_fts
  └── [★★★] search_vector populates existing rows; GIN present  (real-PG)
[+] services/document_search.py :: search_documents()
  ├── [★★★] name FTS ranks a title-term doc (e.g. "Arkadon") to TOP
  ├── [★★★] >100-doc reachability: doc at recency-rank 368/401 IS found by name  ← the bug
  ├── [★★★] facts signal contributes (query matches a Schicht-A fact, not the title)
  ├── [★★★] chunk signal contributes (term only in body)
  ├── [★★★] RRF ordering: name match outranks content-only match
  ├── [★★★] circle-filter: a non-owner/circle-restricted doc is NOT returned  ← D2
  ├── [★★ ] ILIKE partial token ("Arkad") still matches
  └── [★★ ] empty/blank q → falls back to recency list (no search)
[+] route GET /api/knowledge/documents?q=
  ├── [★★★] q present → ranked; q absent → recency list (byte-identical to today)
  └── [★★ ] KB-ACL gate unchanged (403 without kb.own / KB access)
FRONTEND
[+] KnowledgePage
  ├── [★★  →E2E] typing "Arkadon" → doc row appears (not just chunk cards), "An Simba senden" clickable
  ├── [★★ ] list re-queries on debounced q (standalone box + omni ?q= on the lens)
  ├── [★★ ] scope=everything → defers to omni overlay (list not hijacked)
  └── [★  ] secondary "Textstellen" section still renders the chunk hits
```

### Failure modes (new codepaths)
- `websearch_to_tsquery` on odd input (operators, empty) → wrap/guard so a malformed query returns
  no rows, never a 500. Test + guard.
- A retrieval signal (facts/chunks) MCP/DB slow or throwing → the fuse must degrade to the signals
  that returned (best-effort), never fail the whole search. Local signals are DB-only (no MCP) so
  low risk; still guard each branch.
- Circle-filter mis-wire → **silent over-exposure** (a restricted doc appears). Highest-stakes;
  the D2 circle test is the guard. Flag CRITICAL if untested.

### NOT in scope (deferred)
- **MCP federation (Paperless + Simba)** → PR2 (own access-control + dedup + heterogeneous-result design).
- **Paginated / "Mehr laden" BROWSE** beyond 100 newest (no query) → search fixes the reachability need.
- **Circle-filtering the BROWSE list** (pre-existing over-broad behavior) → separate concern.
- SearXNG as a document-search backend → rejected (web metasearch, no internal index/connectors).

### What already exists (reuse, don't rebuild)
- `DocumentResponse`/`DocumentRow` + the row render + all actions (An Simba senden, tier, reindex,
  delete) — reused as-is; a search result must return this shape.
- `DocumentFactRetrieval.search()` — query→document_ids, already circle-filtered (facts signal).
- `RAGRetrieval` — circle-filtered chunk semantic search (chunk signal; aggregate to best-per-doc).
- The GENERATED `search_vector` + GIN + multilingual-union FTS pattern (notes/messages/facts/chunks).
- The Wissen lens `consumesQueryInline` wiring already reads `?q=`/`?scope=` on this lens.
- `rag.list_documents` (rag_service.py:888) — the seam to add `q`.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | Scope split to PR1/PR2; D2 circle-filter locked; DRY+perf baked in; 0 critical gaps |

- **UNRESOLVED:** none.
- **VERDICT:** ENG CLEARED — PR1 (local hybrid) ready to implement. Federation (Paperless + Simba) is PR2, gated on an access-control + dedup design.

