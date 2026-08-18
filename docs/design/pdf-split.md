# PDF-Split — automatic multi-document PDF detection + splitting at ingest

**Status:** PR1 (core auto-split) + PR2 (owner review flow) + PR3 (slow-lane
split worker with VLM page transcription) implemented. Dark by default
(`PDF_SPLIT_ENABLED=false`).

Closes the "multi-document files" deferral in
`docs/design/paperless-llm-metadata.md` (open question 3).

## Problem

Batch scans routinely staple 10+ independent documents — invoices, letters,
notices, contracts — into one PDF, often badly scanned with a poor or absent
text layer. Ingesting such a file as ONE document pollutes retrieval (one giant
mixed doc), produces one wrong Paperless record, and starves Schicht A of
per-document facts.

## Requirements (user-fixed)

1. Renfield detects **on its own** that a PDF contains multiple documents, at
   the **existing entry points** — no new upload paths.
2. **Page count is NEVER a gate, cap, or signal.** Input is an arbitrary mix of
   single-page documents and multi-page contracts: a 2-page file can be two
   documents, a 200-page file can be one contract. Boundaries come from content
   evidence only. Cost is bounded honestly instead: context limits via
   sliding-window batching, unbounded duration via a dedicated worker.
3. Confidence-gated automation: confident splits ingest automatically;
   uncertain boundaries are held for owner review.
4. The combined original is **archived, never ingested** — only the split
   single documents reach the KB + Paperless.

## Architecture

### Insertion point: document-worker pre-stage

All batch entry points (folder-ingest MCP push, email ingest,
`internal.ingest_file`, meeting pipeline) converge on
`services/folder_ingest.py::ingest_document` → `DocumentTaskQueue` →
`workers/document_processor_worker.py::_process_entry`. Detection runs there,
**before Docling**, so one seam covers every entry point and the API pod never
does slow work. The chat-upload lane (`api/routes/chat_upload.py`, its own
inline path with an interactive latency budget and a present user) is
**deliberately out of scope**.

Worker flow (initial_ingest branch, after the idempotent-consumer guard):

```
pdf_split_enabled?  ──no──▶ normal pipeline (byte-identical)
        │yes
maybe_split_at_ingest(db, doc_id, skip_split, user_id)
        │
        ├─ False ──▶ normal pipeline (single-doc verdict / any detection error)
        └─ True  ──▶ ack, stop (split lifecycle owns the doc)
```

`skip_split` in the task params is the loop-breaker: a treat-as-single
re-enqueue can never re-enter detection.

### Detection pipeline (`services/pdf_split_detector.py`)

1. **Per-page text signals** — poppler `pdftotext -layout` via the canonical
   `DocumentProcessor.extract_text_layer`, split on its `\f` page separators
   (T-A0-1: poppler is the only extractor recovering subsetted
   no-ToUnicode-font tokens, and `assess_text_layer_quality` was calibrated on
   ITS output); pypdfium2 textpages are the fallback when poppler is
   unavailable or its page segmentation disagrees with the real page count.
   Signal = first ~600 + last ~200 chars (letterhead / date / "Seite 1 von N"
   regions). A small number of garbage pages ride along as an explicit
   "unreadable" placeholder. A 1-page PDF short-circuits to a single-document
   verdict WITHOUT an LLM call — that is analytic (one page cannot contain two
   documents), not a page-count heuristic.
2. **Slow-lane classification** (`classify_slow_lane`) — fraction-based, never
   count-based: first page garbage OR >30% garbage pages → `vlm`; signals
   exceeding one LLM context window (`PDF_SPLIT_WINDOW_CHARS`) → `windows`.
   Both route to the dedicated split worker (`_route_to_slow_lane`): parent
   parked `split_pending` + enqueued on `renfield:tasks:pdfsplit`. Fail-safe
   gates keep the status quo (single ingest, loud log) when the lane cannot
   help: no worker heartbeat, or a VLM case without `OLLAMA_VISION_MODEL`.

   **The slow lane** (`services/pdf_split_slow_lane.py` +
   `workers/pdf_split_worker.py`, meeting-worker template, replicas:1, row
   heartbeat `documents.split_heartbeat_at`): stored-plan replay wins first,
   the durable rejection record is honored, then `vlm_fill_signals` transcribes
   EVERY garbage page via `ollama_vision_model` (plain text, per-call
   `PDF_SPLIT_VLM_PAGE_TIMEOUT_S`, deliberately NO page cap — cost is bounded
   by isolation + timeouts, never by skipping pages; a failed page keeps its
   placeholder) and the multi-window boundary call decides. Outcomes reuse the
   shared `act_on_verdict`; a `single` outcome HANDS THE DOC BACK to normal
   ingest (`skip_split`, enqueue-failure reverts the park + retries).
   Poison/transient-cap fail-safe = the same hand-back — lifecycle-aware:
   split_review is dropped (the review owns it), existing children mark the
   doc failed instead (REINGEST recovery; single-ingest would duplicate),
   and only a terminal mid-execute error does likewise. Guard rails: a
   wholesale VLM outage (0 of N garbage pages transcribed) retries as
   transient instead of deciding over placeholders; sessions are NOT held
   across the unbounded VLM work (two short phases, re-guarded);
   `PDF_SPLIT_ENABLED=false` parks the worker's backlog in the PEL (real
   kill switch — flagpark leaves don't burn the retry budgets); a live row
   heartbeat stops any other actor from un-parking a mid-VLM doc; 1-page
   files short-circuit analytically BEFORE routing.
3. **Boundary call(s)** — strict-JSON on the TEXT model (never JSON from the
   VLM: qwen3-vl think-buffer trap, see `paperless_metadata_extractor.py`).
   Template: `MinutesExtractor` (`prompts/pdf_split.yaml`, de/en, fence-tolerant
   parse, never raises). The prompt states explicitly that the file can be any
   mix of one-page docs and long contracts, lists the content evidence for a
   boundary, binds annexes/AGB to their parent, and prefers fewer documents
   when unsure. **Arbitrary length via sliding windows:** signals batch into
   context-budget windows; a non-final window's trailing piece is "open" and is
   re-decided by the next window (which is told the open document's start
   page); results merge in pure Python. One window = the common case.
4. **Validation** (`validate_boundaries`) — contiguous, non-overlapping,
   exhaustive coverage of the page range, confidences clamped. Anything
   invalid, unparseable, or single collapses to a single-document verdict.

Every failure mode degrades to "single document" — detection must never break
ingest — with two deliberate exceptions (`services/pdf_split_errors.py`):

- A **transient LLM-infrastructure failure** (host down, timeout, 5xx) raises
  `SplitTransientError`, which the worker's PEL-retry taxonomy treats as
  retryable. Swallowing it would permanently commit a multi-document PDF as
  one combined document (COMPLETED → every re-push dedups).
- An error while *executing* a split propagates (falling through to normal
  ingest after children were partially created would double-ingest the
  combined file). Within execution, a RETRY-class child outcome (disk full,
  lost create race) raises `SplitTransientError` (PEL retry, idempotent
  resume); only genuinely terminal child results raise `SplitExecutionError`
  (parent marked failed; the entry-point re-push → REINGEST is the deliberate
  retry).

### Confidence gate — whole-file

Auto-split iff ≥2 pieces AND `min(confidence) >= PDF_SPLIT_AUTO_THRESHOLD`
(default 0.85). Any piece below → the WHOLE file goes to owner review. No
per-boundary partial splits — a half-ingested parent is a state the dedup
matrix, Paperless leg and review UI would all have to model.

### Review flow (PR2, `services/pdf_split_proposals.py` + `/api/pdf-split`)

An uncertain verdict files/refreshes ONE pending `pdf_split_proposals` row
(partial unique; a refresh does not re-fire the owner notification), parks the
parent in `split_review` and notifies the owner (personal, presence-gated
downstream). The owner decides on /brain/review (`PdfSplitReviewSection`,
flag-gated): approve — optionally after editing ranges, where the UI permits
only contiguity-preserving operations (merge-with-next, add-boundary) and the
server re-validates (422) — or reject (treat-as-single).

- **Resolution never splits in the API pod**: approve persists the (edited)
  plan + parks the parent `split_pending` + enqueues; the WORKER executes via
  the stored plan (full crash-safe machinery). Reject re-enqueues with the
  `skip_split` loop-breaker.
- **Durable decisions**: resolutions use a conditional UPDATE (concurrent
  resolutions → 409, never silently discarded); a REJECTED row is the durable
  treat-as-single record — detection consults `has_rejected_proposal`, so a
  stale/reclaimed plain task can never re-park a document the owner chose to
  keep whole.
- **Strand recovery**: retrying the SAME resolution on an already-resolved
  proposal idempotently re-enqueues (the recovery route for a Redis blip
  between commit and enqueue); an ownerless proposal (NULL user_id) is
  visible/resolvable by admins under auth so it cannot strand invisibly.
- **Evidence**: per-page snippets ride the proposal row; page thumbnails are
  on-demand authenticated JPEG renders from a bounded dedicated executor.
- An error while FILING a proposal degrades to single-document ingest (an
  uncertain verdict never loses a document); transient DB errors PEL-retry.

### Split execution (`services/pdf_splitter.py`)

Each piece is written with pypdfium2 (`PdfDocument.new()` + `import_pages`) and
re-entered through `ingest_document`: children get sha256 dedup, owner/tier
(inherited from the parent's atom/tier), the Paperless-pending stamp (iff the
parent was filing-wanted at detection time), `source` inheritance
(`pdf_split` when the parent has none), and the worker enqueue — then flow
through OCR/Schicht-A/KG/Paperless like any upload. Children carry
`split_from_document_id` → parent.

**Idempotent resume — three pieces (each closed a /review finding):**

- **Persisted plan.** The boundary LLM is nondeterministic, so a crash-resume
  must never re-detect (boundary drift with unchanged titles could silently
  drop pages). The confident verdict is stored as an APPROVED
  `pdf_split_proposals` row BEFORE execution; a redelivered entry replays the
  stored plan verbatim (revalidated; a corrupt plan falls back to detection).
- **Deterministic, parent-scoped resume keys.** pdfium bytes are not
  run-deterministic, so resume keys on the child filename
  (`{parent-stem}_{parent-hash8}_teil{NN}_{title-slug}.pdf`): the parent's
  content-hash prefix makes recurring scanner names + recurring title slugs
  collision-free across different batch scans, and the resume probe (one
  batched SELECT) additionally requires the row's `split_from_document_id` to
  be this parent or still unstamped (the create-vs-stamp crash window).
- Parts are rendered ONE at a time (no all-parts-in-RAM peak), skipped parts
  get their lineage stamp healed, and the parent is archived LAST. A
  byte-identical part deduping onto an existing document counts as covered.

**Children never re-enter detection** (`split_from_document_id` guard) — no
wasted per-child LLM calls, no recursive re-splitting of a contract+annex
child whose pages renumbered from 1.

**Parent = archived:** `status='split_archived'`, `paperless_state='done'`
(settled — 'done' already covers the deliberate-skip case per its constant
docs; the reconciler additionally requires `status='completed'`, so the
archive is doubly excluded from filing), any partial chunks from a pre-split
ingest attempt purged → excluded from retrieval. Bytes stay on the uploads PVC.

**The archive cannot be resurrected:** the worker acks any entry for a doc in
a split-owned status (`DOC_SPLIT_OWNED_STATUSES` in `models/database.py`)
BEFORE and INDEPENDENT of the feature flag — a flag-off incident rollback
cannot re-ingest the combined original on a redelivered entry — and the same
guard covers `user_reindex`; `POST /documents/{id}/reindex` additionally 409s.
**Status contract:** the three split states are part of `DOC_STATUSES`, and
`internal.ingest_status` reports archived/split-in-flight rows in its
narrative (parked state must never dead-end invisibly).

### Dedup matrix change

`classify_existing` gains: `status='split_archived'` → `DUPLICATE` (a re-pushed
combined file is already handled — without this it would RETRY forever);
`split_pending`/`split_review` fall into the existing in-flight RETRY branch.

### Data model (migration `pc20260816_pdf_split`)

- `documents.split_from_document_id` (FK→documents, ON DELETE SET NULL,
  indexed) — child → archived original.
- `documents.split_heartbeat_at` — row-level claim for the slow lane (mirrors
  `Meeting.heartbeat_at`; jobs are unbounded-duration, reclaim keys on
  heartbeat staleness, never a duration estimate).
- `pdf_split_proposals` — uncertain splits awaiting review (precedent:
  `kg_merge_proposals`): proposal JSON (pieces), page_signals JSON (evidence
  for the UI), overall_confidence, status pending/approved/rejected, partial
  unique "one pending per document".
- Status constants: `split_pending` (slow lane), `split_review` (proposal
  open), `split_archived` (split executed).

Migration is inspector-guarded + rerunnable; fresh installs (no `documents`
table yet) are owned entirely by `Base.metadata.create_all`.

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `PDF_SPLIT_ENABLED` | `false` | Master flag (dark) |
| `PDF_SPLIT_WINDOW_CHARS` | `24000` | Char budget per boundary-LLM window (context batching, NOT a document-size assumption) |
| `PDF_SPLIT_VLM_PAGE_TIMEOUT_S` | `45` | Per-call time bound for VLM page transcription (PR3) |
| `PDF_SPLIT_AUTO_THRESHOLD` | `0.85` | Whole-file auto-split confidence gate |

Deliberately **no page-count settings** of any kind.

## Phasing

- **PR1 (this)** — migration, models, flags, detector (windowed), splitter +
  idempotent `execute_split`, worker pre-stage, `classify_existing` branch.
  Enabling it already auto-splits confidently-detected text-layer PDFs;
  slow-lane and uncertain cases log + status quo.
- **PR2 — review flow (this)** — proposals service + `/api/pdf-split` routes,
  the flag-gated `/brain/review` section, split status badges on `/knowledge`
  (+ polling treats parked states as terminal), `pdf_split_enabled` in
  `/api/config/features`, owner notification. Children-list on the archived
  parent card: deferred polish.
- **PR3 — slow lane** — `PdfSplitTaskQueue` (stream `renfield:tasks:pdfsplit`),
  `workers/pdf_split_worker.py` (meeting-worker template: row heartbeat,
  poison-pill → treat-as-single, transient/terminal split, replicas 1),
  per-page VLM transcription of garbage pages (plain text, per-call timeout,
  circuit breaker, NO page cap), `k8s/pdf-split-worker.yaml`.

## Risks / accepted residuals

- **Over-splitting** (contract + annexes): prompt guidance + whole-file gate;
  a miss degrades to review (PR2) or status quo — never data loss (parent
  bytes retained, children deletable).
- **Boundary-LLM latency inline** (single-window case in the doc worker):
  bounded to one call; escape hatch = route all boundary calls to the split
  worker (one-line change).
- **Terminal child failure after archive:** a child whose own Docling run
  fails terminally AFTER the parent archived has no automatic re-push source
  (its bytes were synthesized). It is visible as `failed` in /knowledge and
  recoverable via manual reindex; the combined original's bytes remain on the
  PVC. Accepted for v1.
- **REINGEST after a TERMINAL split failure** (parent marked failed, re-push
  re-enters): detection is not re-run blindly — the persisted plan replays —
  but if the plan itself was the problem (revalidation rejects it), fresh
  detection may propose different boundaries than already-created children.
  The hash-scoped filenames keep matching parts aligned; a drifted part shows
  as an extra visible child. Sharply narrowed vs v1's re-detect-always.
