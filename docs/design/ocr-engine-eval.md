# OCR-Engine Evaluation — Design

> Status: **HARNESS SHIPPED, evaluation not yet run.** Backs the TODOS.md item
> "OCR engine evaluation / swap". This is a benchmark + methodology, NOT a swap
> decision. No production behaviour changes here; `bin/run_ocr_engine_eval.py` is a
> read-only harness against the prod corpus.

## Premise

The KB ingest OCR path (`src/backend/services/document_processor.py`) is
**already docling-based** — not the bare `easyocr` the original TODO assumed. It
runs docling `EasyOcrOptions` for force-full-page OCR, plus a poppler
`pdftotext -layout` text-layer path, and records which pathway produced the
chunks in `document_processing_history.ocr_engine` ∈
`docling` / `docling_full_page_ocr` / `poppler_text_layer`.

Everything downstream of the engine — the `is_low_quality_text` gate
(`utils/content_quality.py`), the per-chunk-rate re-OCR trigger, the retrieval
filter — is a **layered workaround for upstream OCR quality**. The open question
is therefore: on the corpus of documents the pipeline **already flagged as
low-quality**, does a different OCR engine (chiefly **Tesseract**) put materially
more usable text into the corpus than the current docling-EasyOcr default? If
yes by a wide margin, a migration is worth planning; if not, the engine is not
the bottleneck and the workarounds stay the right layer.

## Candidate engines (offline-first)

| engine | what it is | role |
|---|---|---|
| `docling` | docling standard converter (embedded text + OCR for bitmap regions) | **baseline** (current default) |
| `docling_full_page_ocr` | docling `EasyOcrOptions(force_full_page_ocr=True)` — the current re-OCR fallback | current alternative |
| `poppler_text_layer` | `pdftotext -layout` text layer → `_simple_chunk` (the Schicht-A text-layer path) | current alternative |
| `docling_tesseract` | docling `TesseractCliOcrOptions` (or `TesseractOcrOptions`), `force_full_page_ocr=True`, `lang=["deu","eng"]` | **the main thing under test** |
| `cloud_ocr_stub` | gated behind `--cloud`; a **no-op stub with a TODO** | placeholder only |

Every engine's output goes through the **same production chunker**
(`DocumentProcessor._create_chunks`) and the **same quality gate**
(`is_low_quality_text`) a live ingest uses, so the counts are directly
comparable and "usable text" means exactly what it means in prod. The harness
reuses `DocumentProcessor` internals read-only (it reuses the DP's own
`_converter` / `_ocr_converter` / `_chunker`, and builds *one extra* converter
for Tesseract) — it does **not** modify `document_processor.py` or its production
`process_document` path.

### Offline-first constraint on cloud OCR

Renfield is fully offline-capable and self-hosted; the ethos precludes shipping a
cloud OCR engine (Azure/Google/AWS Textract) as a default. Document bytes are
private household/business records — sending them off-box is a deliberate, opt-in
deployment decision, not a benchmark convenience. The `cloud_ocr_stub` engine is
therefore a **no-op** that never calls any external API; wiring a real cloud
backend is left as a documented TODO (`_run_engine`, `CLOUD_ENGINE` branch) to be
taken up only if the offline constraint is relaxed for a specific instance, with
an explicit no-exfiltration-by-default gate.

## Corpus selection

The harness benchmarks the population the product already considers bad, reusing
the existing selection signals (no new definition of "low quality" that could
drift from the UI):

1. **Paperless-audit low-quality signal** (`ha_glue/services/paperless_audit_service.py`):
   `status='failed' AND error_message LIKE 'ocr_quality%'` **OR** the latest
   `document_processing_history` row dropped ≥ 30 % of its chunks at the quality
   gate (`dropped / NULLIF(produced + dropped, 0) >= 0.30`). The harness imports
   `_LOW_QUALITY_DROP_RATIO` and `PaperlessAuditService._latest_history_subquery`
   directly so the rule cannot diverge.
2. **Chunkless population** (`services/kb_maintenance_tool.py`, i.e.
   `internal.ingest_status` / `internal.reindex_documents`): `status='completed'`
   with **0 chunk rows**.

`--doc-id` benchmarks a specific document verbatim (any status), `--limit N`
caps the flagged corpus (newest first, default 20), `--all-flagged` takes the
whole flagged+chunkless set.

### File bytes

`Document.file_path` is a filesystem path on the uploads PVC — the same path
`RAGService.process_existing_document` hands to
`DocumentProcessor.process_document`. The harness reads the original bytes from
there. Documents whose file is missing on disk are skipped with a warning (never
a crash). **Assumption:** uploads are on-disk at `Document.file_path`, not
stored in the DB — confirmed by `document_processor` / `rag_service`, which both
operate on `doc.file_path` directly.

## Metric — relative, ground-truth-free (state the limitation loudly)

There are **no human reference transcripts** for these documents, so there is no
character/word error rate and no absolute "accuracy". The only honest comparable
signal is **relative**: which engine puts the most low-quality-gate-passing text
into the corpus, at the lowest drop-ratio.

Per `(doc, engine)` the harness records:

- `chars_extracted` — length of the full extracted text,
- `chunks_kept` / `chunks_dropped` — after the production quality gate,
- `drop_ratio` = `chunks_dropped / (kept + dropped)`,
- `usable_chars` = total characters across the **gate-passing** chunks (the text
  that would actually enter the corpus), the primary comparison signal,
- `wall_s` — convert + chunk wall-time.

A document is **improved** vs the baseline when a candidate yields materially
more `usable_chars` (> 5 %), **regressed** when materially less, **unchanged**
otherwise (both-zero counts as unchanged — a candidate gets no credit for also
extracting nothing). The aggregate reports, per engine: docs run, mean
drop-ratio, mean wall-time, mean usable-chars, and improved/unchanged/regressed
counts over the docs where both the baseline and the candidate ran.

**Limitation — this measures "more gate-passing text", NOT "more CORRECT text".**
An engine that confidently emits fluent-but-wrong OCR would score *well* here
because its output clears the `is_low_quality_text` heuristic (which only rejects
glyph-noise, not plausible-looking mistakes). The verdict is a **screening
signal** — "this candidate is worth a human spot-check on N docs" — never a swap
decision on its own. Any migration must be confirmed by reading the actual
extracted text on a sample of the "improved" docs.

## Decision rule

Mirroring the TODO's ">50 % failure-rate reduction" bar, but expressed on the
relative signal this harness can actually measure:

> **Swap only if** a candidate engine **improves** (materially more usable text)
> on **> 50 % of the comparable flagged corpus** while **not regressing** more
> than a small minority (say ≤ 10 %), its mean drop-ratio is clearly lower than
> the baseline's, AND a **human spot-check** of a sample of the improved docs
> confirms the extra text is correct (not confident garbage). Wall-time must stay
> within the ingest budget (OCR is already ~45 s/doc; a 2-3× slower engine that
> only marginally improves quality is not worth it).

If no candidate clears that bar, the conclusion is that **the engine is not the
bottleneck** — the existing heuristic filter + re-OCR trigger + retrieval gate
are the correct layer, and the item closes as "measured, no swap".

## How to run (on .159, against the prod corpus)

Backend code does not run in CI; it runs on the `.159` build box / inside the
backend image against the prod DB + uploads PVC (see
`memory/reference_test_runner_159.md`). Tesseract and its language data must be
present in the image (see deps below).

```bash
# inside the backend image / on .159, with PYTHONPATH=src/backend and the prod .env
python bin/run_ocr_engine_eval.py --limit 20
python bin/run_ocr_engine_eval.py --doc-id 1234 --doc-id 1235
python bin/run_ocr_engine_eval.py --all-flagged --json /tmp/ocr-eval.json
python bin/run_ocr_engine_eval.py --limit 20 --cloud        # includes the no-op stub

python bin/run_ocr_engine_eval.py --self-test               # pure-logic, no IO/deps
```

The harness **degrades gracefully**: if the `tesseract` binary / docling
Tesseract option / poppler `pdftotext` is missing, that engine is marked
unavailable and skipped (per-doc `note`), never crashing the run — so it produces
a partial comparison even on an incompletely-provisioned host.

### Dependencies to run the full comparison on .159

- `docling>=2.0.0`, `docling-core>=2.0.0`, `easyocr>=1.7.0` — already in
  `src/backend/requirements.txt` (baseline + `docling_full_page_ocr`).
- `poppler-utils` (the `pdftotext` binary) — already required by the
  `poppler_text_layer` path in production.
- **Tesseract** (for `docling_tesseract`) — **now baked into the backend image**:
  `tesseract-ocr` + `tesseract-ocr-deu` + `tesseract-ocr-eng` are installed in the
  runtime stage of `src/backend/Dockerfile` (alongside `poppler-utils`). The harness
  uses docling's `TesseractCliOcrOptions` (ships with docling 2.x, shells out to the
  `tesseract` binary — no `tesserocr` C-binding needed). So after the next backend
  build, the full comparison runs with no manual install; **on a pod running an
  older image**, `apt-get install tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng`
  provides it ephemerally.
  - **Language codes differ:** Tesseract uses ISO 639-2 (`deu`/`eng`); EasyOcr
    uses `de`/`en`. The `deu`/`eng` traineddata must be present or Tesseract
    OCR produces nothing.
- **The harness itself** (`bin/run_ocr_engine_eval.py`) is not baked into the image
  (`bin/` is outside the `src/backend` build context, per convention with the other
  `bin/*` scripts). Run it by `kubectl cp`-ing it into a pod that has the uploads
  PVC mounted (e.g. `document-worker`) — the document bytes live at
  `Document.file_path` on that PVC, so the harness must run where it's mounted.

These deps are needed **only to run the benchmark**. A `docling_tesseract` swap
would additionally require adding Tesseract to the backend image and wiring a
`TesseractOcrOptions` branch into `document_processor._ensure_initialized` — out
of scope for this evaluation; that is the migration the decision rule gates.

## References

- TODOS.md — "OCR engine evaluation / swap" (premise refreshed 2026-06-17).
- `src/backend/services/document_processor.py` — the production OCR path this
  benchmarks against (untouched).
- `src/backend/utils/content_quality.py` — the `is_low_quality_text` quality gate.
- `src/backend/ha_glue/services/paperless_audit_service.py`,
  `src/backend/services/kb_maintenance_tool.py` — the corpus-selection signals.
- `bin/run_ocr_engine_eval.py` — the harness.
