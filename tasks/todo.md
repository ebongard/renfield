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

## PR2 — review flow — DONE, PR #1099 (pushed, awaiting merge approval)
- [x] `services/pdf_split_proposals.py` (create idempotent + notify-once, durable conditional-UPDATE resolutions, idempotent same-action retry = strand recovery, has_rejected_proposal consulted by detection)
- [x] `api/routes/pdf_split.py` + main.py mount (owner-scoped, NULL-owner admin-resolvable, JPEG thumbs from bounded executor, worker-alive 503; `document_worker_is_alive` promoted to task_queue)
- [x] `api/routes/config.py` FeatureFlags `pdf_split_enabled`
- [x] Frontend: PdfSplitReviewSection (contiguity-by-construction editing, opt-in thumbs, prop re-sync), StatusBadge split states (DESIGN warning tone), polling terminal states, i18n de/en/it
- [x] Tests: 16 route/service + 6 RTL; 2 review cycles (10+1 findings fixed); full suite baseline-identical; rotten MeetingsPage test fixed (undici/jsdom File)

## PR3 — VLM slow path — DONE, merged #1100 (98348c07)
- [x] All of it + 2 review cycles (incl. release-blocking inert-queue-defaults bug) — see design doc

## Deployed 2026-08-18 (household)
- [x] Images `2026-08-18-pdfsplit`, migration applied, flag ON, worker live, netpols enforced, E2E green (synthetic 3-doc split perfect)
- [x] xidra rollout DONE 2026-08-20 (migration, flag, worker, netpols mit eigener privater Quelle k8s/xidra/redis-postgres-netpol.yaml; ~3-min Paperless-Netpol-Incident, behoben) ·
- [ ] Follow-ups: deploy-script fix (sed registry for alembic job) · [x] /verify-tests DONE 2026-08-20: 28→0, branch fix/test-rot-28 (awaiting push/merge) — Follow-ups: search-excludes-abandoned-branch Regressionstest, TokenBlacklist private-client cleanup · test docs 421-424 cleanup (user decision)

## Review / verification log
(fill during implementation)
