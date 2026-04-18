# Document Processor Worker Split

Plan to extract document ingestion (Docling + EasyOCR + layout detection) from the
backend pod into a dedicated worker deployment.

## Status

- **Current**: the backend runs document processing inline in the upload
  request. The first real PDF upload after boot triggered an `OOMKilled`
  (exit 137) at 6 GiB — Docling initialises RT-DETR layout detection plus
  EasyOCR detection + recognition models (770 weights) on top of the already
  resident Whisper+Transformers footprint.
- **Short-term mitigation** (shipped): backend memory limit raised from 6 GiB
  to 8 GiB. Eliminates the crash for the current `WHISPER_MODEL=small` +
  Docling workload with headroom.
- **Long-term plan** (this document): extract document processing into a
  separate worker deployment so the backend's footprint stays around 3–4 GiB
  regardless of upload traffic.

## Why a separate deployment

- Isolates a heavyweight, spiky memory consumer (Docling easily peaks at
  1.5–2 GiB on large PDFs) from the steady-state API serving path.
- Survives `WHISPER_MODEL` being bumped to `medium`/`large-v3` without having
  to re-budget the backend every time.
- Lets document ingestion scale horizontally without scaling FastAPI replicas.
- Aligns with the pattern already used for Ollama (separate GPU-bound pods
  behind a ClusterIP Service) and matches the Celery/Redis dependencies
  already declared in `requirements.txt`.

## Architecture target

```
    upload request                                  poll / WS notify
    ─────────────►  ┌─────────────┐                ◄───────────────
                    │   Backend   │                     Client
                    │  (FastAPI)  │
                    └──────┬──────┘
         create Doc         │
      record(status=queued) │
         enqueue task       ▼
                    ┌────────────┐                  ┌──────────────┐
                    │   Redis    │◄────dequeue──────│  Doc-Worker  │
                    │  (queue)   │                  │  (Celery /   │
                    └────────────┘                  │  TaskQueue)  │
                                                    │  — Docling — │
                           read/write                │  — EasyOCR — │
                                                    │  — Embedder  │
                                                    └──────┬───────┘
                                                           │
                                                           ▼
                                                    shared upload storage
                                                    (NFS /mnt/data/renfield-uploads)
                                                           │
                                                    ┌──────▼───────┐
                                                    │ Postgres DB  │
                                                    │  (chunks +   │
                                                    │  embeddings) │
                                                    └──────────────┘
```

## Changes required

### Backend code

1. `src/backend/services/rag_service.py`
   - Split `ingest_document()` into two entry points:
     - `create_document_record(file_path, kb_id, filename, hash) → Document`
       creates the row with `status=queued`, returns its id.
     - `process_existing_document(document_id, force_ocr) → None` runs the
       pipeline for a record already in the DB (Docling, chunking, embedding,
       FTS population, status transitions).
   - The current `ingest_document()` can remain as a thin wrapper that calls
     both in sequence, to avoid breaking callers outside the upload path.

2. `src/backend/api/routes/knowledge.py`
   - `POST /api/knowledge/upload`:
     - After duplicate check + file save → `create_document_record(...)`
     - Enqueue `{"type": "document_process", "parameters": {"document_id": …,
       "force_ocr": …}}` via `TaskQueue.enqueue(...)`
     - Return `202 Accepted` with the document metadata (status = queued).
   - `GET /api/knowledge/documents/{id}` already exposes the live status —
     no new endpoint needed.

3. New module `src/backend/workers/document_processor_worker.py`
   - Async main loop:
     - `redis = TaskQueue()`
     - forever: `dequeue()` (with BRPOP-backed blocking or sleep fallback)
     - on `document_process` type: open a fresh SQLAlchemy session, call
       `process_existing_document(doc_id, force_ocr)`, update task status.
     - graceful shutdown on SIGTERM (finish current task, then exit).
   - Entry point: `python -m workers.document_processor_worker`.

### K8s

4. `k8s/document-worker.yaml`
   - Deployment, 1 replica, same backend image (`registry.treehouse.x-idra.de/renfield/backend:latest`)
   - `command: ["python", "-m", "workers.document_processor_worker"]`
   - Memory `requests: 1Gi, limits: 6Gi` (owns the Docling peak alone)
   - Mounts the shared uploads volume + ConfigMap like backend
   - `imagePullSecrets: harbor-pull-secret`
   - No Service — worker is a pure consumer.

5. `k8s/backend.yaml`
   - Drop memory limit from `8Gi` back to `4Gi` once the worker takes over
     document ingestion.
   - Drop `renfield-data` PVC if the only consumers of `/app/data` were
     uploads (otherwise keep it; migrate only the uploads subdir).

6. Shared uploads storage
   - The current Longhorn PVC `renfield-data` is `ReadWriteOnce` — backend
     and worker on different nodes can't both mount it.
   - Two options:
     - **A** hostPath `/mnt/data/renfield-uploads` on the NFS mount
       (`192.168.1.9:/mnt/data`) available on both GPU workers. Simplest; same
       pattern as Ollama's `/mnt/llm`. Requires the directory to exist on both
       nodes (one-time setup).
     - **B** Longhorn RWX PVC (via RWX engine). More moving parts, not yet
       enabled in the cluster.
   - Recommendation: start with A. Migration of existing files is a `cp -r`
     step during rollout.

### Frontend (optional, follow-up)

7. `src/frontend/src/pages/KnowledgePage.tsx`
   - After upload (now returns `status=queued`), poll `/api/knowledge/documents/{id}`
     until `status in {completed, failed}` and surface progress in the UI.
   - Without this change the user experiences a "completed" badge only after
     the next list refresh — acceptable, but unpolished.

## Migration plan

1. Ship the code split (new RAGService entry points + worker module) + K8s
   manifest + shared-storage mount, behind a feature flag
   `DOCUMENT_WORKER_ENABLED=false` — existing inline path still works.
2. Create the NFS upload directory on gpu-1 and gpu-2, `cp -r` the existing
   uploads from the Longhorn PVC into it.
3. Flip the flag on in the ConfigMap, roll out.
4. Backfill the frontend polling (separate PR).
5. Once stable, drop the feature flag and the inline code path; lower the
   backend memory limit to 4 GiB.

## Out of scope

- Task retries / DLQ semantics (worker will just log + mark `failed` on
  unhandled exceptions, same behaviour as today).
- Horizontal autoscaling of the worker.
- Celery — the existing `TaskQueue` in `services/task_queue.py` is already
  Redis-backed and fit for purpose; pulling in Celery adds more moving parts
  without a concrete need right now. Can be revisited if we need richer
  features (priorities, periodic tasks, scheduled retries).
