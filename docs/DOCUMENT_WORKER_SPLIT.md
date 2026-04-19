# Document Processor Worker Split

Plan to extract document ingestion (Docling + EasyOCR + layout detection) from the
backend pod into a dedicated worker deployment.

Revision history:
- v1 (2026-04-18): initial draft, inline feature flag, hostPath NFS, existing Redis-list TaskQueue.
- **v2 (2026-04-19)**: pressure-tested in `/plan-eng-review`. Six changes incorporated:
  (1) TaskQueue replaced by Redis Streams for at-least-once durability;
  (2) shared storage via NFS-CSI driver, not hostPath;
  (3) worker heartbeat gates the upload route's fast-path;
  (4) test matrix made mandatory (worker-crash recovery is blocking);
  (5) frontend polling ships in the cutover PR, not a follow-up;
  (6) worker entrypoint audited for module isolation.

## Status

- **Current**: backend runs document processing inline in the upload request.
  First real PDF upload OOMKilled at 6 GiB — Docling boots RT-DETR layout +
  EasyOCR detection/recognition models (770 weights) on top of the resident
  Whisper + transformers footprint.
- **Short-term mitigation** (shipped in PR #387): backend memory limit 12 GiB.
  Works, but still a landmine for scanned multi-hundred-page PDFs.
- **Long-term plan** (this document): separate worker deployment, backend
  drops back to ~5 GiB.

## Why a separate deployment

- Isolates a heavyweight, spiky memory consumer from the steady-state API path.
- Survives `WHISPER_MODEL` being bumped to `medium`/`large-v3` without re-budgeting.
- Lets document ingestion scale horizontally without scaling FastAPI replicas.
- Aligns with the pattern already used for Ollama (separate pods behind a
  Service) and uses the Redis instance already in the cluster.

## Architecture target

```
    upload request                                     poll / WS notify
    ─────────────►  ┌────────────────┐               ◄──────────────────
                    │    Backend     │                       Client
                    │   (FastAPI)    │
                    └────────┬───────┘
     1. create Doc          │
        record(queued)      │   4. check worker heartbeat
     2. XADD on stream      │      → 503 if stale (> 90s)
                            ▼
                    ┌───────────────┐              ┌────────────────┐
                    │     Redis     │              │   Doc-Worker   │
                    │ stream+group: │◄──XREADGROUP─│  (1 replica)   │
                    │ renfield:doc  │──XACK──      │                │
                    │ heartbeat key │              │   Docling      │
                    └───────┬───────┘              │   EasyOCR      │
                            │ heartbeat            │   → embedder   │
                            │ SET renfield:        │     (Ollama)   │
                            │ worker:hb EX 90s     └────────┬───────┘
                            │  ◄─────────────────── every 30s
                            │
                            │  read/write PDFs
                            ▼
                    ┌──────────────────────┐         ┌──────────────┐
                    │ RWX PVC via NFS-CSI  │         │ Postgres DB  │
                    │ uploads-shared       │         │ chunks +     │
                    │ NFS: 192.168.1.9:    │         │ embeddings   │
                    │  /mnt/data/          │         └──────────────┘
                    │  renfield-uploads    │
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
- `XREADGROUP` reads without removing — the entry stays in the Pending Entries
  List (PEL) until ACKed.
- `XACK` on successful completion removes it from the PEL.
- Worker crash → entry stays pending → next worker (or the same worker after
  restart) reads it via `XPENDING` + `XCLAIM` after a visibility timeout.
- Consumer-group semantics for free horizontal scale (post-MVP).

No Celery needed. `redis.asyncio` already ships XREADGROUP support.

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
    close()
```

The old `TaskQueue` class stays for any non-document fire-and-forget uses
(none currently, but `ensure_admin_user` or cleanup scheduling might adopt
it later). We rename the module carefully to avoid breaking imports.

## Shared uploads storage — NFS-CSI driver

The uploads directory must be visible to both the backend (for save) and the
worker (for read). Longhorn is `ReadWriteOnce` — multi-node access is not
possible. `hostPath` was ruled out: `/mnt/data/renfield-uploads` is **not**
exported from `192.168.1.9` today (only `/mnt/data/llm` is), and any
hostPath-based solution requires per-node manual mount management that
silently breaks on node replacement. That violates our "no per-host patches"
rule.

### Chosen approach: CSI Driver NFS

Install the official Kubernetes CSI driver for NFS (kubernetes-csi/csi-driver-nfs).
This is a standard, well-maintained k8s addon that speaks to any NFSv3/v4
server and exposes ReadWriteMany PersistentVolumes declaratively.

**Server-side prep** (one-time, on `192.168.1.9`):

```
sudo mkdir /mnt/data/renfield-uploads
sudo chown nobody:nogroup /mnt/data/renfield-uploads
sudo chmod 0770 /mnt/data/renfield-uploads
# /etc/exports — add a line for each worker node subnet:
/mnt/data/renfield-uploads  192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
sudo exportfs -ra
```

**Cluster-side** (tracked in `../private_k8s/nfs-csi/`):

1. Install the driver via the upstream manifests (vendor pinned copy under
   `../private_k8s/nfs-csi/driver-v4.x.y.yaml`).
2. Create a `StorageClass` `nfs-csi`:
   ```yaml
   apiVersion: storage.k8s.io/v1
   kind: StorageClass
   metadata:
     name: nfs-csi
   provisioner: nfs.csi.k8s.io
   parameters:
     server: 192.168.1.9
     share: /mnt/data/renfield-uploads
   reclaimPolicy: Retain
   volumeBindingMode: Immediate
   mountOptions:
     - nfsvers=4.2
     - hard
     - timeo=600
   ```
3. In `k8s/backend.yaml` and `k8s/document-worker.yaml`, mount a
   `PersistentVolumeClaim` with `accessModes: [ReadWriteMany]` and
   `storageClassName: nfs-csi` at `/app/data/uploads`.

This puts the NFS mount **inside** the Kubernetes object model. Node
replacement is transparent — the CSI driver re-attaches on whatever node
the pod lands on. No `/etc/fstab`, no per-node `mkdir`, no drift.

### Data migration

Existing uploads live on the Longhorn RWO PVC under `/app/data/uploads/`. The
migration is a one-shot `rsync` executed with the backend in a read-only window:

1. Create the new NFS-CSI PVC `renfield-uploads-shared`.
2. Spin up a migrator Job that mounts **both** the old PVC and the new PVC and
   runs `rsync -a --delete /old/uploads/ /new/uploads/`.
3. Scale backend to 0 briefly (or set it to respond 503 on `/api/knowledge/upload`
   during the cutover).
4. Re-run rsync to capture last-second deltas.
5. Redeploy backend with the new PVC mounted, worker deployment up, flag flipped.
6. Keep the old PVC with `reclaimPolicy: Retain` for 30 days in case of rollback.

## Worker heartbeat gates the upload route

If `DOCUMENT_WORKER_ENABLED=true` but no worker is consuming (e.g. image-pull
stalled, worker crash-looping, ConfigMap out of sync), the upload endpoint
would enqueue tasks into a stream no one reads. Users see permanent spinners.

Solution: worker publishes a liveness key every 30 s:

```
SET renfield:worker:document:heartbeat <pod_name> EX 90
```

Upload endpoint checks it before enqueuing:

```python
async def _worker_is_alive(redis) -> bool:
    return await redis.get("renfield:worker:document:heartbeat") is not None
```

If the key is missing **and** `DOCUMENT_WORKER_ENABLED=true`, the upload
endpoint returns `503 Service Unavailable` with a clear message. The client
can retry. We do **not** fall back to inline — that would silently hide the
infrastructure outage. Fail loudly, escalate to ops.

If `DOCUMENT_WORKER_ENABLED=false`, the old inline path runs, heartbeat is
ignored.

## Worker entrypoint — module isolation

`python -m workers.document_processor_worker` must **not** import or boot
the FastAPI app, or it pulls the MCP client (connecting to 10 servers on
startup), Whisper, Speechbrain, Ollama clients, the full lifecycle init —
exactly what we're trying to excise from the worker.

The worker module imports only:

- `services.database` (engine + session factory)
- `services.rag_service` (`RAGService`, specifically the new
  `process_existing_document` entry point)
- `services.document_processor` (Docling)
- `services.task_queue` (new `DocumentTaskQueue`)
- `utils.config.settings`
- `utils.llm_client.get_embed_client` (Ollama calls from Docling's chunk loop)

Explicit test: `importlib.import_module("workers.document_processor_worker")`
must not trigger `main.app` instantiation or MCP-connect.

## Backend code changes

### 1. `src/backend/services/rag_service.py`

Split `ingest_document` into two entry points, keep the existing function as
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
    """Insert the Document row with status=queued; returns the persisted row."""

async def process_existing_document(
    self,
    document_id: int,
    force_ocr: bool = False,
) -> None:
    """Run Docling → chunking → embedding → FTS on a pre-existing row.
    Transitions status queued → processing → completed/failed.
    Commits a final error_message on any unhandled exception."""

# Back-compat wrapper (non-upload callers, test fixtures):
async def ingest_document(self, *args, **kwargs) -> Document:
    doc = await self.create_document_record(...)
    await self.process_existing_document(doc.id, kwargs.get("force_ocr", False))
    return await self.db.get(Document, doc.id)
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
    return DocumentResponse(id=doc.id, status="queued", ...)  # 202
else:
    # Legacy inline path — unchanged.
    doc = await rag.ingest_document(...)
    return DocumentResponse(...)
```

HTTP status code for the new path is `202 Accepted`, not `200`.

### 3. New module `src/backend/workers/document_processor_worker.py`

Async main loop:

```python
async def main():
    redis = await aioredis.from_url(settings.redis_url)
    queue = DocumentTaskQueue(redis, consumer_id=pod_name())
    await queue.ensure_group()

    # Reclaim entries from dead consumers on startup
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
            async with AsyncSessionLocal() as db:
                rag = RAGService(db)
                try:
                    await rag.process_existing_document(
                        document_id=params["document_id"],
                        force_ocr=params.get("force_ocr", False),
                    )
                    await queue.ack(entry_id)
                except Exception as e:
                    logger.exception(f"Task {entry_id} failed: {e}")
                    # Do NOT ack — entry stays in PEL, reclaim_stale picks it
                    # up after visibility timeout. Document row already has
                    # status=failed and error_message from process_existing_document.
    finally:
        heartbeat_task.cancel()
        await queue.close()
        await redis.aclose()
```

### 4. `src/frontend/src/pages/KnowledgePage.tsx`

After upload returns 202, poll `/api/knowledge/documents/{id}` every 2 s
until `status ∈ {completed, failed}`. Surface per-document progress (queued,
processing, completed, failed) with a spinner and a clear error path.

This is **not** a follow-up — it ships in the cutover PR. Without it, users
see a silent hung spinner immediately after upload, which is a UX regression
vs. the synchronous `200` today.

## K8s changes

### `k8s/document-worker.yaml` (new)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: document-worker
  namespace: renfield
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels: {app.kubernetes.io/name: document-worker}
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
            # Postgres, Redis, Ollama URLs come from renfield-env; secrets as in backend
          volumeMounts:
            - {name: uploads, mountPath: /app/data/uploads}
            - {name: cache-home, mountPath: /app/data/cache-home}  # shared HF+EasyOCR cache
            - {name: mcp-config, mountPath: /app/config/...}
          resources:
            requests: {cpu: 500m, memory: 1Gi}
            limits:   {cpu: "2",  memory: 6Gi}
      volumes:
        - name: uploads
          persistentVolumeClaim: {claimName: renfield-uploads-shared}
        - name: cache-home
          persistentVolumeClaim: {claimName: renfield-cache-shared}
        - name: mcp-config
          configMap: {name: renfield-mcp-config}
```

No Service — worker is a pure stream consumer.

### `k8s/backend.yaml` (modified)

- Replace `renfield-data` PVC mount at `/app/data` with two PVCs:
  `renfield-uploads-shared` (RWX, NFS-CSI) at `/app/data/uploads`, and
  `renfield-cache-shared` (RWX, NFS-CSI) at `/app/data/cache-home`.
- After cutover: memory limit `12Gi → 5Gi`.

### `k8s/configmap.yaml`

- Add `DOCUMENT_WORKER_ENABLED: "false"` (default off, flipped in rollout).

### `../private_k8s/nfs-csi/` (new, cluster-wide)

- Pinned copy of `csi-driver-nfs` manifests (version tracked in the README).
- Two `StorageClass` objects: `nfs-csi` for uploads, shared by namespaces as
  they need RWX NFS.

## Test matrix (mandatory, blocking)

| # | Path | Kind | Tool |
|---|------|------|------|
| 1 | `create_document_record` happy path | unit | pytest + async |
| 2 | `create_document_record` with duplicate hash → 409 | unit | pytest |
| 3 | `process_existing_document` happy path (stubbed Docling) | unit | pytest |
| 4 | `process_existing_document` Docling failure → `status=failed` with error_message | unit | pytest |
| 5 | `process_existing_document` embedder raises → `status=failed`, DB rolled back | unit | pytest |
| 6 | Upload endpoint 202 with flag=on, worker heartbeat present | API | httpx + test client |
| 7 | Upload endpoint 503 with flag=on, heartbeat missing | API | httpx |
| 8 | Upload endpoint legacy 200 with flag=off | API | httpx |
| 9 | Worker loop: successful processing → XACK called | integration | testcontainers redis |
| 10 | **Worker crash recovery: SIGKILL mid-task → next worker reclaims via XCLAIM** | **integration, blocking** | testcontainers redis |
| 11 | Worker SIGTERM: finishes current task, exits cleanly | integration | subprocess + signal |
| 12 | Worker module import does NOT instantiate FastAPI app | unit | importlib + introspection |
| 13 | Frontend: upload → 202 → polling → completed badge shown | e2e | vitest + MSW |
| 14 | Frontend: upload → 202 → polling sees `status=failed` → error surface | e2e | vitest + MSW |

Test 10 is the reason we're switching to Streams. Without it, the whole
redesign is lipstick.

## Migration plan

Sequenced, each step independently revertable:

1. **Cluster prep** — install NFS-CSI driver, create StorageClass, verify
   provisioning with a throwaway test PVC.
2. **Server prep** — create `/mnt/data/renfield-uploads` on `192.168.1.9`,
   add `/etc/exports` entry, `exportfs -ra`.
3. **PR A (infra)** — add `DocumentTaskQueue` (Streams) alongside existing
   `TaskQueue`; add `k8s/document-worker.yaml` + PVC manifests + worker
   module; `DOCUMENT_WORKER_ENABLED=false`. Nothing behaviourally changes.
4. **PR B (refactor)** — split `RAGService.ingest_document`; unit tests 1–5.
5. **PR C (cutover-ready)** — upload endpoint branch on flag; heartbeat
   gating; frontend polling; tests 6–14.
6. **Rollout** — deploy PR C with flag still off. Run migrator Job.
   `docker compose up` cutover check:
   1. Scale backend to 0, run final rsync.
   2. Scale backend to 1 with flag still off — verify new PVC mounts read/write.
   3. Apply ConfigMap with `DOCUMENT_WORKER_ENABLED=true`.
   4. Restart backend + worker. Smoke-test an upload end-to-end.
7. **Cleanup** — drop backend memory `12Gi → 5Gi` in a follow-up commit once
   the stack has been stable 48 h. Remove legacy inline path once flag has
   been on for a week.

## Out of scope

- Celery migration — Streams covers everything we need.
- Worker horizontal autoscaling — 1 replica holds current load.
- Multi-tenant priority queues (e.g. premium uploads first) — single stream.
- PDF-level progress reporting (per-page %) — status=processing is enough.

## Open questions

- **NFS-CSI version pin.** Current stable is v4.10.x — check against K8s
  v1.35 compatibility before ingesting.
- **Migration-window tolerance.** How long can uploads be 503 during rsync?
  Current estimate: 2 min for today's payload. If unacceptable, we can
  double-write during the transition and cut over lazily.
