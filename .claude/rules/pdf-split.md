---
paths:
  - "src/backend/services/pdf_split*.py"
  - "src/backend/workers/pdf_split_worker.py"
  - "src/backend/api/routes/pdf_split*.py"
---
# PDF-Split

Loaded only when a PDF-split file is read. Long form: `docs/design/pdf-split.md`.
`PDF_SPLIT_ENABLED` — opt-in / dark by default. Multi-document batch scans are detected + split at ingest as a
document-worker **pre-stage before Docling**, so ALL batch entry points are covered without new upload paths.

## Never
- **Page count is NEVER a gate, cap or signal.** Batches are arbitrary mixes of 1-page docs and long contracts;
  boundaries come from content evidence via ONE strict-JSON text-model call, context-window-batched by
  `PDF_SPLIT_WINDOW_CHARS`. The VLM lane likewise has deliberately NO page cap — cost is bounded by a per-call
  timeout, not by skipping pages.
- **Approve NEVER splits in the API pod.** It parks `split_pending` + enqueues; the worker replays the stored plan.
- **Never re-run the boundary LLM on resume.** A confident split persists its plan as an *approved*
  `pdf_split_proposals` row **BEFORE** executing; crash-resume replays it verbatim (the LLM is nondeterministic).
- **Never let the archived original come back.** The combined original is archived (`split_archived`, chunkless,
  Paperless-settled) behind a **flag-INDEPENDENT** worker guard + a reindex-409 — neither a flag-off rollback nor a
  reindex click may resurrect it.

## Decision path
- Whole-file confidence gate `PDF_SPLIT_AUTO_THRESHOLD`: confident → auto split; uncertain → OWNER REVIEW on
  `/brain/review` (`pdf_split_proposals` + `/api/pdf-split`).
- Review resolution is durable via a conditional UPDATE. Reject = permanent treat-as-single, consulted by detection.
  A same-action retry idempotently re-enqueues (the Redis-blip strand recovery). NULL-owner proposals are resolvable
  by admins.
- Garbled scans → the dedicated VLM split lane (`workers/pdf_split_worker.py`, stream `renfield:tasks:pdfsplit`, row
  heartbeat `documents.split_heartbeat_at`, replicas:1). Routed **ONLY while its worker heartbeat is fresh AND
  `OLLAMA_VISION_MODEL` is set**, else status-quo single ingest. EVERY unreadable page is VLM-transcribed.
- **Fail-safe = hand back as ONE document.** Flag-off parks the lane's backlog in the PEL.
- Transient failures raise `SplitTransientError` → the worker's PEL-retry (not the fail-safe).

## Children
- Children re-enter via `folder_ingest.ingest_document` (dedup / tier / Paperless / enqueue for free).
- Filenames are deterministic and parent-hash-prefixed — they are the **resume keys**; do not change the scheme
  casually. Lineage is `split_from_document_id`.
