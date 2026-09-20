---
paths:
  - "src/backend/services/schicht_a_extractor.py"
  - "src/backend/services/document_date*.py"
  - "src/backend/services/document_search.py"
  - "src/backend/services/document_fact_retrieval.py"
  - "src/backend/services/document_dedupe*"
---
# Documents & Schicht-A facts (titles, dates, search, per-fact tier)

Loaded only when a Schicht-A / document-date / document-search / dedupe service is read. Long form:
`docs/SECOND_BRAIN.md` (incl. backfill tooling + frontend wiring); access model in `circles.md` / `docs/CIRCLES.md`.

## Fact reads never leak the parent document
`services/document_fact_retrieval.py` (keyword FTS + identifier-ILIKE + obligations) is circle-filtered, but a fact can
be MORE visible than its document. Fact-source document titles are therefore circle-filtered **separately**
(`_visible_document_meta` in `services/knowledge_tool.py`): a non-visible source falls back to a generic
`Dokument {id}` label with no chip, so a tier-overridden-public fact on a private doc never leaks title/filename.

## Generated titles (`documents.generated_title`, migration `pc20260611`)
`services/schicht_a_extractor.generate_document_title(facts)` synthesizes the title **from the facts, not the OCR
text** — that is what keeps ingest and `bin/backfill_document_titles.py` in agreement. Best-effort after the facts
commit, gated by `schicht_a_extraction_enabled`. `display_name = generated_title → title → filename`.

## Document date (`documents.document_date`, migration `pc20260831`) — `services/document_date.derive_document_date`
- Order: **explicit document-date kind** (`rechnungsdatum` first, then `belegdatum`/`dokumentdatum`/
  `ausstellungsdatum` — trusted even AHEAD of the import, a pre-dated invoice) → **event/period kind**
  (`leistungsdatum`/`transaktionsdatum`/`leistungszeitraum` …) → the generated title's date. The last two count only
  up to `created_at` + `FUTURE_TOLERANCE_DAYS` (7, below the shortest common payment term).
- A rejected candidate **falls through to the next source, never to a clamped date**.
- NEVER count: obligations (`category='obligation'`) and every unlisted kind (`faelligkeit*`, `gueltigkeit`,
  `naechster_abschlag` …). "First parsable date of ANY fact" dated documents up to a year ahead.
- Kinds are free LLM labels → match through ONE canonical key, `_canonical_kind`, applied to the extracted kind and
  every list entry alike: separators dropped; a trailing 1–2 digit enumeration stripped (a 4-digit year tail like
  `rechnungsdatum_2024` stays a different fact); `datum_X`→`Xdatum`; `X_vom`→`Xdatum`; a linking `s` before `datum`
  folded. So `Rechnungs-Datum`/`rechnung_datum`/`datum_rechnung`/`rechnung_vom` keep their rank — with no blanket
  "contains datum" rule.
- Deliberately NOT folded / unlisted: `_am` (`faellig_am` is a deadline; document-date `_am` forms are explicit
  entries); `zahlungsdatum`/`payment_date` and bare `stichtag` (the import-relative limit cannot tell an old document's
  due/debit date from a real date).
- Shared by `simba_ingest_review._document_period` and `services/document_date_backfill.py` (`--rederive`).

## Document search (`GET /api/knowledge/documents?q=`, always-on) — `services/document_search.py::search_documents`
Three signals — NAME (`documents.search_vector` FTS + ILIKE), FACTS, CHUNKS — RRF-fused (`rag_hybrid_rrf_k`), then
**ONE circle gate on the fused ids** (`document_chunks_circles_filter`). **The NAME signal is NOT circle-filtered on its
own**: its safety rests entirely on that gate, which is Postgres-only — hence
`gate_available = not enforce_circles or postgres`, and NAME contributes only when it is true. FACTS/CHUNKS are already
filtered in-service. A failing signal degrades to the others; blank `q` → the recency list. Migration
`pc20260829_documents_fts` (GENERATED `search_vector` via `build_generated_tsvector_expression`, GIN `CONCURRENTLY`).

## Per-fact tier override (`document_facts.tier_overridden`)
- A per-fact tier PATCH sets it; `AtomService.update_tier`'s kb_document cascade re-tiers only
  `WHERE NOT tier_overridden` — **sticky in BOTH directions** (a public issuer stays public after the doc is privatized).
  Cleared by `AtomService.reset_fact_tier` / `POST /api/atoms/documents/facts/{id}/reset-tier` (owner-only).
- Carry-over across re-extraction: the ingest hook snapshots prior overridden facts by `_fact_identity_key`
  (`category` + `kind` + a `_squish`-ed value signature) BEFORE writing the fresh set. A fact that drifted enough not
  to match **reverts to the doc tier** (fail-safe: never more visible than the parent); a reset is not resurrected.
- Write-then-purge is serialized by a per-document advisory lock on a dedicated connection (`_reindex_lock`, NS
  `0x5341`); the loser skips. Acquisition is bounded-timeout and degrades to UNLOCKED under pool pressure — a
  best-effort guard that never blocks ingest.
