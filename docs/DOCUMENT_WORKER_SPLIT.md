# Document Processor Worker Split

Plan to extract document ingestion (Docling + EasyOCR + layout detection) from the
backend pod into a dedicated worker deployment.

Revision history:
- v1 (2026-04-18): initial draft, inline feature flag, hostPath NFS, existing Redis-list TaskQueue.
- v2 (2026-04-19, morning): pressure-tested in `/plan-eng-review`. Six changes:
  (1) TaskQueue → Redis Streams for at-least-once durability;
  (2) shared storage via NFS-CSI driver, not hostPath;
  (3) worker heartbeat gates the upload route;
  (4) test matrix mandatory (worker-crash recovery is blocking);
  (5) frontend polling ships in the cutover PR, not a follow-up;
  (6) worker entrypoint audited for module isolation.
- **v3 (2026-04-19, afternoon)**: pressure-tested in `/plan-design-review`.
  Eight UX decisions adopted:
  (1) reuse existing `pending` status, don't invent `queued`;
  (2) full polling strategy (1 s start, 10 s cap, 30 min timeout, Visibility API, AbortController);
  (3) full DE copy map for all response states;
  (4) tab-title mutation + localStorage optimistic persistence;
  (5) per-page progress via stage + page counters;
  (6) queue-position always shown when ≥ 1;
  (7) full A11y spec (ARIA live, progressbar, focus management, contrast);
  (8) cutover PR split into C1 (minimal cutover) + C2 (polish).

## Status

**Shipped 2026-04-19.** The `/api/knowledge/upload` path is now fully async
— creates a pending Document, enqueues to the Redis Stream, returns 202,
polling frontend drives it to `completed`. Backend memory limit dropped
12 GiB → 8 GiB (would be 5 GiB if not for chat_upload, which still runs
Docling inline — follow-up).

Merged PRs:

- **PR A** (infra) — `4cb5800`: NFS-CSI, shared PVCs, DocumentTaskQueue
  (Streams), DocumentProgress, worker Deployment, `DOCUMENT_WORKER_ENABLED=false`.
- **PR B** (refactor) — `cf7ea1e`: RAGService split into
  `create_document_record` + `process_existing_document` + back-compat
  `ingest_document` wrapper.
- **PR C1** (cutover) — `dc46060`: upload endpoint branches on flag,
  heartbeat gate, batch endpoint, live progress, minimal frontend.
- **#392** (fix) — `a3c5162`: backend missed the shared PVC mounts at
  cutover; added them, migrated doc 9 via `kubectl cp`.
- **PR C2** (polish) — `fe48f24`: polling backoff + Visibility API +
  localStorage + tab-title + progressbar A11y + 413/415 semantics.
- **#394** (cleanup) — `65e39b7`: removed legacy inline path,
  `DOCUMENT_WORKER_ENABLED` config, 12 GiB → 8 GiB.
- **#395** (follow-up) — `5f702a3`: silent RAG regression
  (`services.llm_client` → `utils.llm_client`), npm cache NFS race,
  mail MCP default config.
- **#396** (race fix) — `fbc9b63`: unique `(file_hash, knowledge_base_id)`
  constraint + route IntegrityError handler → 409 not 500.

Still open:

- `chat_upload.py` **sync `/index` endpoint** — user-triggered path
  still calls `ingest_document` inline. Migrating this to 202+poll
  would break the current client contract (response has no
  `document_id` / `chunk_count` at 202 time). Not worth it until the
  chat UI needs the change for other reasons. The background
  auto-index path (the hot one) is on the worker.

## Why a separate deployment

- Isolates a heavyweight, spiky memory consumer from the steady-state API path.
- Survives `WHISPER_MODEL` being bumped to `medium`/`large-v3` without re-budgeting.
- Lets document ingestion scale horizontally without scaling FastAPI replicas.
- Aligns with the pattern already used for Ollama and uses the Redis instance
  already in the cluster.

## Architecture target

```
    upload request                                     poll / tab badge
    ─────────────►  ┌────────────────┐               ◄──────────────────
                    │    Backend     │                       Client
                    │   (FastAPI)    │
                    └────────┬───────┘
     1. create Doc          │
        record(pending)     │   4. check worker heartbeat
     2. XADD on stream      │      → 503 if stale (> 90s)
                            ▼
                    ┌───────────────┐              ┌────────────────┐
                    │     Redis     │              │   Doc-Worker   │
                    │ stream+group: │◄──XREADGROUP─│  (1 replica)   │
                    │ renfield:doc  │──XACK──      │                │
                    │ heartbeat key │              │   Docling      │
                    │ stage key     │              │   EasyOCR      │
                    │ progress key  │──SET─────────│   → embedder   │
                    └───────┬───────┘              │     (Ollama)   │
                            │ heartbeat            └────────┬───────┘
                            │ SET renfield:                 │
                            │ worker:hb EX 90s              │
                            │  ◄─────────────────── every 30s
                            │
                            │  read/write PDFs
                            ▼
                    ┌──────────────────────┐         ┌──────────────┐
                    │ RWX PVC via NFS-CSI  │         │ Postgres DB  │
                    │ renfield-uploads     │         │ chunks +     │
                    │ NFS: 192.168.1.9:    │         │ embeddings   │
                    │ /mnt/data/k8s/       │         └──────────────┘
                    │ renfield/uploads     │
                    └──────────────────────┘
```

## Queue durability — Redis Streams

The existing `services/task_queue.py` uses `LPUSH` + `RPOP`. `RPOP` is destructive:
the task leaves the queue before the worker acknowledges it. A worker crash
mid-processing loses the task, the Document row stays `status=processing`
forever, and the user sees a hung spinner with no recovery path. This pattern
is fine for "fire-and-forget" but unsafe for document ingestion where every
request must eventually succeed or fail visibly.

**Replace with Redis Streams** (`XADD`, `XREADGROUP`, `XACK`, `XPENDING`).
Streams give us:

- Single atomic `XADD` (no LPUSH+SET race).
- `XREADGROUP` reads without removing; entry stays in the Pending Entries
  List (PEL) until ACKed.
- `XACK` on successful completion removes it from the PEL.
- Worker crash → entry stays pending → next worker (or the same after restart)
  reads it via `XPENDING` + `XCLAIM` after a visibility timeout.
- Consumer-group semantics for free horizontal scale (post-MVP).

No Celery. `redis.asyncio` already ships XREADGROUP support.

Changes in `services/task_queue.py`:

```
class DocumentTaskQueue:
    stream_key   = "renfield:tasks:document"
    group_name   = "docworker"
    consumer     = f"worker-{pod_name}"  # unique per pod
    visibility_s = 600  # 10 min, covers worst-case PDF

    enqueue(params)          → XADD, returns stream entry id
    read_one(block_ms=5000)  → XREADGROUP BLOCK 5000 COUNT 1
    ack(entry_id)            → XACK
    reclaim_stale()          → XPENDING + XCLAIM for entries idle > visibility_s
    xlen_pending()           → XLEN + PEL depth for queue-position calculation
    close()
```

The old `TaskQueue` class stays for any non-document fire-and-forget uses.

## Shared uploads storage — NFS-CSI driver

Install kubernetes-csi/csi-driver-nfs. RWX PVCs are then declarative; the
driver mounts pod-local on-demand. Node replacement is transparent.

**Server-side state** (already verified 2026-04-19):

- `/mnt/data/k8s` is exported `*` from `192.168.1.9` and world-writable
  (`drwxrwxrwx`). Subdirectories created by the CSI provisioner land as
  `nobody:root 755`, which backend + worker (both container-root) can
  read/write through root_squash.
- Subdirectory convention: `/mnt/data/k8s/<namespace>/<purpose>/`.
  For Renfield: `/mnt/data/k8s/renfield/uploads/` and
  `/mnt/data/k8s/renfield/cache-home/`.

**Cluster-side** (tracked in `../private_k8s/nfs-csi/`):

1. Install csi-driver-nfs v4.13.2 (latest stable, released 2026-04-16).
2. Two StorageClasses:
   ```yaml
   # nfs-csi-renfield-uploads
   provisioner: nfs.csi.k8s.io
   parameters:
     server: 192.168.1.9
     share: /mnt/data/k8s/renfield/uploads
   reclaimPolicy: Retain
   mountOptions: [nfsvers=4.2, hard, timeo=600]
   ```
3. PVCs in `k8s/` reference these classes with `accessModes: [ReadWriteMany]`.

The NFS mount lives inside the Kubernetes object model. No `/etc/fstab`, no
per-node `mkdir`, no drift.

### Data migration

Existing uploads live on the Longhorn RWO PVC under `/app/data/uploads/`.
One-shot rsync with a short read-only window:

1. Create NFS-CSI PVC `renfield-uploads-shared`.
2. Migrator Job mounts both old + new, runs `rsync -a --delete old/ new/`.
3. Backend scale-to-0 briefly (or `/api/knowledge/upload` returns 503).
4. Re-rsync to capture deltas.
5. Redeploy backend with new PVC, worker up, flag flipped.
6. Old PVC kept with `reclaimPolicy: Retain` for 30 days.

## Worker heartbeat gates the upload route

If `DOCUMENT_WORKER_ENABLED=true` but no worker is consuming (image-pull
stall, crash-loop, ConfigMap drift), the upload endpoint would enqueue
tasks into a stream no one reads. Users see permanent spinners.

Solution: worker publishes a liveness key every 30 s:

```
SET renfield:worker:document:heartbeat <pod_name> EX 90
```

Upload endpoint checks before enqueuing; if missing and the flag is on,
return **503** with the copy specified below. The client shows a Retry CTA.
We do **not** fall back to inline — that silently hides the infrastructure
outage. Fail loudly, escalate to ops.

## Worker entrypoint — module isolation

`python -m workers.document_processor_worker` must **not** import or boot
the FastAPI app. Otherwise it pulls MCP-connect, Whisper, Speechbrain, Ollama
clients, full lifecycle init — exactly what we're excising from the worker.

Worker imports only:

- `services.database` (engine + session factory)
- `services.rag_service` (`RAGService`, `process_existing_document`)
- `services.document_processor` (Docling)
- `services.task_queue` (`DocumentTaskQueue`)
- `services.progress` (new, see below)
- `utils.config.settings`
- `utils.llm_client.get_embed_client`

Explicit test: `importlib.import_module("workers.document_processor_worker")`
must not trigger `main.app` or MCP connect.

## Processing stage + per-page progress

During `process_existing_document`, the worker writes two Redis keys so the
frontend can show granular progress. Keys are TTL-bound to 30 min to avoid
leaking state for dead tasks.

```
renfield:doc:{doc_id}:stage     → "parsing" | "ocr" | "chunking" | "embedding"
renfield:doc:{doc_id}:progress  → "47/120"  (current/total pages, or "" for non-paginated)
```

New module `src/backend/services/progress.py`:

```python
class DocumentProgress:
    def __init__(self, redis, doc_id: int): ...
    async def set_stage(self, stage: str) -> None: ...
    async def set_pages(self, current: int, total: int) -> None: ...
    async def clear(self) -> None: ...
    async def read(self) -> dict: ...  # used by GET /api/knowledge/documents/{id}
```

Docling's `DocumentConverter` already accepts progress callbacks in newer
versions; worker wires them to `DocumentProgress.set_pages`. Non-paginated
inputs (TXT, PNG) skip the page counter; stage alone is meaningful.

`GET /api/knowledge/documents/{id}` surfaces both fields alongside `status`:

```json
{
  "id": 42,
  "status": "processing",
  "stage": "ocr",
  "pages": { "current": 47, "total": 120 },
  "queue_position": null,
  "error_message": null
}
```

When status is `pending`: `queue_position` is set to the 1-indexed position in
the stream (via `XPENDING` + PEL depth). When status transitions to
`processing`, `queue_position` clears.

## Upload lifecycle UX

This is the complete user-facing contract for the new flow.

### Status taxonomy (reuses existing `pending`)

| Status | Label (DE) | Icon | Color | Meaning |
|---|---|---|---|---|
| `pending` | „In Warteschlange" | `Clock` | gray-500 | Enqueued, worker hasn't picked it up |
| `processing` | „Wird verarbeitet…" | `Loader2` (spin) | primary-500 | Active, with stage + page sub-label |
| `completed` | „Fertig" | `CheckCircle2` | green-500 | Indexed and searchable |
| `failed` | „Fehlgeschlagen" | `AlertCircle` | red-500 | Unhandled exception; error_message set |

**No new backend status.** `queued` is not introduced. `pending` (already in
the frontend's `statusFilters`) covers the enqueued-but-not-yet-read state.

### Sub-labels during `processing`

The badge sub-label reads the `stage` + `pages` fields:

| Stage | Sub-label |
|---|---|
| parsing | „Dokument wird gelesen…" |
| ocr | „Text wird erkannt… Seite `{current}` von `{total}`" (only shown when pages present) |
| chunking | „Abschnitte werden erstellt…" |
| embedding | „Wird in die Wissensbasis aufgenommen…" |

If `processing` persists > 30 s without stage progression, an additional
muted line reads: „Das kann bei großen PDFs eine Minute dauern."

### Queue position

When `status=pending` and `queue_position` is set, the badge sub-label reads
„Platz `{queue_position}`". Always shown when present; hidden when position
can't be computed (e.g., stream lookup failed — we don't show "Platz —").

### Polling strategy

- **First poll** 1 s after upload response.
- **Interval** doubles after each poll until capped at 10 s: 1, 2, 4, 8, 10, 10, …
- **Reset to 1 s** on every status/stage/page change (user just got useful info,
  keep it snappy).
- **Timeout** after 30 min continuous polling per document → mark locally as
  „Zeitüberschreitung", show Retry CTA. Remote state in DB unchanged; a manual
  refresh can pick it up if the worker eventually finishes.
- **Page Visibility API**: tab hidden → pause interval; tab visible → single
  catch-up fetch + resume interval.
- **AbortController** on every fetch; component unmount aborts in-flight requests.
- **Per-tab singleton**: one polling loop per page instance, not per document.
  Batch lookups via `GET /api/knowledge/documents?ids=1,2,3` (new query param)
  when > 1 document is active.

### HTTP response copy (DE)

| Code | Situation | UI surface | Message |
|---|---|---|---|
| **202** | Upload accepted, enqueued | Inline on upload zone + new row appears in list as `pending` | „Hochgeladen. Die Verarbeitung startet gleich." |
| **409** | Duplicate hash (existing_document in body) | Modal dialog | Title: „Dieses Dokument gibt es schon". Body: „`{existing.filename}` wurde am `{existing.uploaded_at|date('de')}` bereits hochgeladen." Primary: „Zum vorhandenen Eintrag springen" (anchor link). Secondary: „Abbrechen". |
| **413** | File too large | Toast (red) | „Datei zu groß. Maximum: `{max_mb}` MB." |
| **415 / 400** | Format not allowed | Toast (red) | „Dateiformat nicht unterstützt. Erlaubt: `{allowed}`." |
| **503** | Worker heartbeat missing | Toast (amber) with Retry CTA | „Die Dokumentverarbeitung ist gerade nicht erreichbar. Bitte in einer Minute erneut versuchen." Button: „Erneut versuchen" → re-POST the same file. |
| **500** | Unknown server error | Toast (red) with Details expander | „Beim Hochladen ist etwas schiefgegangen." Expander reveals raw `detail`. |
| `status=failed` from poll | Processing exception | Inline in the document row, replacing the spinner | „Fehlgeschlagen". Expander reveals `error_message`. Button: „Erneut hochladen" → re-POST (old document row is left alone). |

### Tab-title + localStorage optimistic state

- **Tab title** mutates while own uploads are `pending` or `processing`:
  `(2) Wissensbasis — Renfield`. On all complete: `(✓) Wissensbasis — Renfield`
  until the user focuses the tab or 30 s pass.
- **localStorage key**: `renfield.kb.inflight` = array of
  `{docId, filename, startedAt}` (max 20 entries, capped at 24 h age).
  Written on 202-response, trimmed after `completed|failed` is observed or the
  entry is older than 10 min (belt-and-suspenders against zombie entries).
- **On page load**: hydrate from localStorage. Any entry whose `docId` matches
  the server's list and is still `pending`/`processing` gets a subtle „Gerade
  hochgeladen" hairline over its row for 5 min.
- **No OS notifications** (`Notification.requestPermission()` is invasive for a
  household context and doesn't add enough on top of title mutation).

### Accessibility

- Status region on the document list is wrapped in `<div role="status"
  aria-live="polite">`. Screen readers announce status transitions as they
  happen. A dedicated polite-live sub-region for stage changes so users aren't
  spammed with page-counter updates (rate-limit: at most every 10 s).
- Progress bar on `processing` rows with `pages.total` present:
  `<progress role="progressbar" aria-valuenow={current} aria-valuemax={total}
  aria-valuetext="Seite 47 von 120">`. Without pages, use `aria-busy="true"`
  on the row instead.
- Icon-only status badges carry `aria-label="{statusLabel}: {filename}"`.
  The visible icon is `aria-hidden="true"`.
- 409 dialog: focus moves to „Zum vorhandenen Eintrag springen" on open.
  `Esc` closes the dialog. Focus returns to the upload trigger on close.
- Touch targets on Retry, Details expander, and row-level buttons ≥ 44×44 px.
- Keyboard path: Tab from upload zone → status filter → each document row's
  primary action → pagination. Enter/Space activate.
- Color contrast: every status color tested against WCAG AA in both Tailwind
  `dark:` and light mode. `gray-500` on `gray-100` fails AA — use `gray-700`
  for the pending label, only the icon carries the lighter tone.

## Backend code changes

### 1. `src/backend/services/rag_service.py`

Split `ingest_document` into two entry points; keep the legacy function as
a thin wrapper so non-upload callers don't break:

```python
async def create_document_record(
    self,
    file_path: str,
    knowledge_base_id: int | None,
    filename: str,
    file_hash: str,
    user_id: int | None,
) -> Document:
    """Insert the Document row with status=pending; returns the persisted row."""

async def process_existing_document(
    self,
    document_id: int,
    force_ocr: bool = False,
) -> None:
    """Run Docling → chunking → embedding → FTS on a pre-existing row.
    Reports progress via DocumentProgress. Transitions
    pending → processing → completed/failed. Always commits a final
    error_message on any unhandled exception."""

# Back-compat wrapper (non-upload callers, test fixtures):
async def ingest_document(self, *args, **kwargs) -> Document: ...
```

Audit required: grep for existing callers of `ingest_document` before merging.

### 2. `src/backend/api/routes/knowledge.py`

Upload endpoint branches on `settings.document_worker_enabled`:

```python
if settings.document_worker_enabled:
    if not await _worker_is_alive(redis):
        raise HTTPException(503, "Document worker unavailable")
    doc = await rag.create_document_record(...)
    await queue.enqueue({"document_id": doc.id, "force_ocr": force_ocr})
    return DocumentResponse(id=doc.id, status="pending", ...)  # 202
else:
    doc = await rag.ingest_document(...)
    return DocumentResponse(...)  # 200, legacy
```

New: `GET /api/knowledge/documents?ids=1,2,3` batch endpoint for the polling
client. Keeps Redis+DB load O(1) regardless of how many docs are in flight.

### 3. New module `src/backend/workers/document_processor_worker.py`

Async main loop:

```python
async def main():
    redis = await aioredis.from_url(settings.redis_url)
    queue = DocumentTaskQueue(redis, consumer_id=pod_name())
    await queue.ensure_group()
    await queue.reclaim_stale()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(redis, stop_event))

    try:
        while not stop_event.is_set():
            entry = await queue.read_one(block_ms=5000)
            if not entry:
                continue
            entry_id, params = entry
            progress = DocumentProgress(redis, params["document_id"])
            async with AsyncSessionLocal() as db:
                rag = RAGService(db, progress=progress)
                try:
                    await rag.process_existing_document(
                        document_id=params["document_id"],
                        force_ocr=params.get("force_ocr", False),
                    )
                    await queue.ack(entry_id)
                except Exception as e:
                    logger.exception(f"Task {entry_id} failed: {e}")
                    # Do NOT ack — reclaim_stale reaps after visibility timeout.
                finally:
                    await progress.clear()
    finally:
        heartbeat_task.cancel()
        await queue.close()
        await redis.aclose()
```

### 4. `src/frontend/src/pages/KnowledgePage.jsx`

Two sub-phases, matching the PR split below:

**C1 (cutover):**
- Upload handler accepts 202 response. Inserts optimistic row with
  `status=pending` while waiting for server list refresh.
- Basic polling loop (fixed 2 s) until `completed`/`failed`.
- Status badges use the full taxonomy above. Copy map applied for all six
  response codes. 409 uses a real modal, not a toast.
- Basic A11y: `role="status" aria-live="polite"` on the status column,
  `aria-label` on icon-only badges, 44 px touch targets.

**C2 (polish):**
- Polling strategy upgraded: 1 s start, 10 s cap, Visibility API, AbortController,
  batch endpoint.
- Stage + page sub-labels; progressbar on rows with pages.
- Tab-title mutation + localStorage optimistic persistence.
- Queue-position sub-label when `pending`.
- Full A11y: progressbar semantics, focus management on 409 dialog, keyboard
  path audit, contrast review.

## K8s changes

### `k8s/document-worker.yaml` (new, PR A)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: document-worker
  namespace: renfield
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app.kubernetes.io/name: document-worker}}
  template:
    metadata:
      labels: {app.kubernetes.io/name: document-worker, app.kubernetes.io/part-of: renfield}
    spec:
      imagePullSecrets: [{name: harbor-pull-secret}]
      containers:
        - name: worker
          image: registry.treehouse.x-idra.de/renfield/backend:latest
          imagePullPolicy: Always
          command: ["python", "-m", "workers.document_processor_worker"]
          envFrom: [{configMapRef: {name: renfield-env}}]
          env:
            - name: POD_NAME
              valueFrom: {fieldRef: {fieldPath: metadata.name}}
          volumeMounts:
            - {name: uploads, mountPath: /app/data/uploads}
            - {name: cache-home, mountPath: /app/data/cache-home}
          resources:
            requests: {cpu: 500m, memory: 1Gi}
            limits:   {cpu: "2",  memory: 6Gi}
      volumes:
        - name: uploads
          persistentVolumeClaim: {claimName: renfield-uploads-shared}
        - name: cache-home
          persistentVolumeClaim: {claimName: renfield-cache-shared}
```

No Service — worker is a pure consumer.

### `k8s/backend.yaml` (modified across PRs)

- **PR A**: add new PVC mounts alongside Longhorn (dual-write window).
- **PR C1 cutover**: drop Longhorn `renfield-data`, keep NFS-CSI PVCs.
- **Post-cutover**: memory limit `12Gi → 5Gi`.

### `k8s/configmap.yaml`

Add `DOCUMENT_WORKER_ENABLED: "false"` in PR A; flip during rollout.

### `../private_k8s/nfs-csi/` (new, cluster-wide)

- Pinned `csi-driver-nfs` v4.13.2 manifests.
- `StorageClass nfs-csi-renfield-uploads` and `StorageClass nfs-csi-renfield-cache`.

## Test matrix (mandatory, blocking)

| # | Path | Kind |
|---|------|------|
| 1 | `create_document_record` happy path | unit |
| 2 | `create_document_record` with duplicate hash → 409 | unit |
| 3 | `process_existing_document` happy path (stubbed Docling) | unit |
| 4 | `process_existing_document` Docling failure → `status=failed` with error_message | unit |
| 5 | `process_existing_document` embedder raises → `status=failed`, DB rolled back | unit |
| 6 | Upload endpoint 202 with flag=on, worker heartbeat present | API |
| 7 | Upload endpoint 503 with flag=on, heartbeat missing | API |
| 8 | Upload endpoint legacy 200 with flag=off | API |
| 9 | Worker loop: successful processing → XACK called | integration |
| 10 | **Worker crash recovery: SIGKILL mid-task → next worker reclaims via XCLAIM** | **integration, blocking** |
| 11 | Worker SIGTERM: finishes current task, exits cleanly | integration |
| 12 | Worker module import does NOT instantiate FastAPI app | unit |
| 13 | DocumentProgress stage + page writes + TTL expiry | unit |
| 14 | `GET /api/knowledge/documents?ids=1,2,3` batch endpoint returns all three | API |
| 15 | Frontend (C1): upload → 202 → row appears `pending` → polling → `completed` | e2e |
| 16 | Frontend (C1): upload → 202 → polling sees `failed` → error surface with Retry | e2e |
| 17 | Frontend (C1): upload → 503 → toast + Retry CTA | e2e |
| 18 | Frontend (C1): duplicate upload → 409 dialog → anchor jumps to existing | e2e |
| 19 | Frontend (C2): backoff observed 1 → 2 → 4 → 8 → 10 s | unit (hook test) |
| 20 | Frontend (C2): Visibility API pauses polling in hidden tab | unit |
| 21 | Frontend (C2): localStorage round-trip on reload during `processing` | unit |
| 22 | Frontend (C2): screen-reader announces status transitions (RTL + axe-core) | e2e |
| 23 | Frontend (C2): queue position sub-label renders when pending with position | unit |

Test 10 is the reason we're switching to Streams. Without it, the whole
redesign is cosmetic.

## Migration plan (revised after UX review)

Five sequenced PRs:

1. **PR A (Infra)** — NFS-CSI installation (vendored in `private_k8s/`),
   StorageClasses, PVC manifests, `DocumentTaskQueue` (Streams) alongside
   existing TaskQueue, `DocumentProgress`, `workers/document_processor_worker.py`,
   `k8s/document-worker.yaml`, `DOCUMENT_WORKER_ENABLED=false`. Nothing behaviourally
   changes from a user perspective.
2. **PR B (Refactor)** — `RAGService` split into `create_document_record` +
   `process_existing_document`. Unit tests 1–5, 13.
3. **PR C1 (Cutover, backend + minimal frontend)** — upload endpoint branches on
   flag; heartbeat gate; batch `?ids=` endpoint; DocumentProgress wired from
   Docling callbacks; minimal frontend (badges + copy map + basic polling +
   409 dialog + basic A11y). Tests 6–12, 14–18. After merge: run migrator Job,
   flip flag, verify.
4. **PR C2 (Polish)** — frontend UX additions: polling backoff, Visibility API,
   localStorage, tab-title mutation, progressbar, queue-position sub-label,
   full A11y audit, contrast review. Tests 19–23.
5. **Cleanup** — backend memory limit `12Gi → 5Gi`. Remove legacy inline path
   once C1 has been flag-on for a week without incident.

## Out of scope

- Celery migration — Streams covers everything we need.
- Worker horizontal autoscaling — 1 replica holds current load.
- Multi-tenant priority queues — single stream.
- Per-user upload quotas / retention — tracked separately.
- Native OS notifications (`Notification.requestPermission()`) — invasive for
  household context; tab-title + localStorage covers the need.

## Open questions

- **Migration window tolerance.** How long can uploads be 503 during rsync?
  Estimate: ~2 min for today's payload. If unacceptable, double-write during
  the transition.
- **NFS-CSI pinning.** Using v4.13.2 (released 2026-04-16). Verify compatibility
  with K8s v1.35 before PR A merges.
