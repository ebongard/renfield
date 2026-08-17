# PDF-Split — automatic multi-document PDF detection + splitting at ingest

Plan: `~/.claude/plans/harmonic-toasting-toucan.md` (approved 2026-08-16). Dark by default (`PDF_SPLIT_ENABLED=false`).

**Hard requirement: page count is NEVER a gate, cap, or signal.** Input is arbitrary mixes of single-page docs + multi-page contracts. Boundaries come from content evidence only; long inputs are handled by context-window batching (sliding windows), bad scans by unbounded per-page VLM transcription in the dedicated split worker.

## PR1 — core auto-split (text-layer path)
- [x] Migration `pc20260816_pdf_split` (verified: up/down/up cycle on scratch PG + fresh-install no-op path)
- [x] `models/database.py`: columns, `PdfSplitProposal`, status constants `split_pending`/`split_review`/`split_archived`
- [x] `utils/config.py`: `pdf_split_enabled=False`, `pdf_split_window_chars`, `pdf_split_vlm_page_timeout_s`, `pdf_split_auto_threshold` (NO page-count settings)
- [x] `services/pdf_split_detector.py`: page signals, windowed boundary LLM call(s) with open-piece carry, pure-Python validation/merge
- [x] `prompts/pdf_split.yaml` (de/en, mixed-shapes guidance, strict JSON)
- [x] `services/pdf_splitter.py`: `split_pdf_bytes`, `execute_split` (idempotent filename-keyed resume, children via `ingest_document`, parent archived LAST + chunk purge), `maybe_split_at_ingest`
- [x] `workers/document_processor_worker.py` pre-stage (lazy import, flag/skip_split guard; slow-lane + low-confidence → logged single-doc until PR2/PR3)
- [x] `services/folder_ingest.py`: `classify_existing` `split_archived` → DUPLICATE, split-in-flight → RETRY
- [x] Tests: 100 passed on .159 (detector/splitter/prestage/folder-ingest); neighboring suites 159 passed
- [x] `docs/design/pdf-split.md` + `docs/ENVIRONMENT_VARIABLES.md` section
- [x] Full backend suite on .159: failure set byte-identical to main baseline (28 pre-existing rotten tests — NOT this feature; user should schedule /verify-tests)
- [x] Commit `eb6ce554` → high-effort /code-review → 10 verified findings → ALL fixed in `a177f270` (persisted plan, parent-hash resume keys, SplitTransientError taxonomy, flag-independent archive guard, reindex 409, status contract, ORM partial unique, model tests, poppler-first signals, shared parse_llm_json+salvage)
- [x] Docs sweep: CLAUDE.md, docs/FEATURES.md, docs/design/pdf-split.md, docs/ENVIRONMENT_VARIABLES.md, paperless-llm-metadata deferral resolved
- [ ] Post-fix full suite (running) + medium /review of fix commit → then WAIT for push/merge approval (no push without permission)

## PR2 — review flow
- [ ] `services/pdf_split_proposals.py` (create/approve/reject + proactive notification)
- [ ] `api/routes/pdf_split.py` (`/api/pdf-split`: list/detail/page-png/approve/reject) + `main.py` mount
- [ ] `api/routes/config.py` FeatureFlags `pdf_split_enabled`
- [ ] Frontend: BrainReviewPage section + `PdfSplitReviewCard`, KnowledgePage split badge/children, i18n de+en
- [ ] Tests: `test_pdf_split_routes.py`, proposal DB tests, React tests

## PR3 — VLM slow path
- [ ] `PdfSplitTaskQueue` (stream `renfield:tasks:pdfsplit`), `workers/pdf_split_worker.py` (meeting-worker template, row heartbeat, poison → treat-as-single)
- [ ] VLM page-signal fill-in (plain-text transcription, per-call timeout, circuit breaker, NO page cap)
- [ ] `k8s/pdf-split-worker.yaml` + kustomization + ConfigMap env
- [ ] Tests: `test_pdf_split_worker.py`

## Review / verification log
(fill during implementation)
