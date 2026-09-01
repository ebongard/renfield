"""
KB / ingest maintenance tools — platform-owned agent tools.

Two chat-triggerable `internal.*` tools for the document ingest → KB → Paperless
pipeline:

- ``internal.ingest_status`` (read-only): reports the live processing state —
  documents by status, how many completed docs are NOT SEARCHABLE (no embedded
  chunk), the worker queue depth + liveness, and the Paperless filing state. Backs
  questions like "wie ist der Verarbeitungsstatus?" / "sind alle Dokumente in
  Paperless?".

- ``internal.reindex_documents`` (write / maintenance): finds ``completed``
  documents with **no searchable (embedded) chunk** — either zero chunk rows OR
  only unembedded ``parent`` chunks whose searchable children were all dropped at
  embed time — and enqueues a ``user_reindex`` worker task for each (purge +
  rebuild) — the same path as ``POST /api/knowledge/documents/{id}/reindex``. Gated
  on ``Permission.RAG_MANAGE`` when auth is enabled (an authenticated low-privilege
  user is refused; auth-off / unidentified-voice turns are allowed, matching the
  platform's HA_CONTROL convention).

"No searchable chunk" (not merely "no chunk rows") is the unifying invisibility
predicate — see ``_searchable_chunk_subquery``. It closes the 2026-07 blind spot
where a document with only unembedded parent chunks was invisible to RAG yet also
invisible to this repair tooling.

Mirrors ``services/memory_list_tool.py``: flattened tool definitions registered by
``agent_tools._register_internal_tools`` + async handlers dispatched as special
cases in ``action_executor`` (which injects the authenticated ``user_id`` and,
for reindex, ``user_permissions``).
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import aliased

from models.database import (
    DOC_STATUS_COMPLETED,
    DOC_STATUS_PENDING,
    DOC_STATUS_SPLIT_ARCHIVED,
    DOC_STATUS_SPLIT_PENDING,
    DOC_STATUS_SPLIT_REVIEW,
    PAPERLESS_STATE_FAILED,
    PAPERLESS_STATE_PENDING,
    Atom,
    Document,
    DocumentChunk,
    DocumentProcessingHistory,
)
from models.permissions import Permission, has_permission
from services.database import AsyncSessionLocal
from services.document_processing_history import ProcessingTrigger
from utils.config import settings

REINDEX_DEFAULT_CAP = 200
REINDEX_MAX_CAP = 500
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200


def _as_bool(value) -> bool:
    """Coerce a tool param to bool (the agent may pass a string/number)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "ja", "force", "on")
    return bool(value)


def _searchable_chunk_subquery():
    """document_ids that have at least one **embedded** (searchable) chunk.

    A completed document ABSENT from this set is invisible to RAG retrieval, and
    that covers TWO failure modes, not one:
      1. no chunk rows at all (OCR/quality gate produced nothing), and
      2. only unembedded ``parent`` chunks — every searchable child was dropped at
         embed time (quality gate or embed-endpoint timeouts during a bulk ingest),
         the 2026-07 "parent-only" mode. Such a doc HAS chunk rows, so a plain
         ``document_id IS NULL`` check (any-chunk) misses it, yet it is exactly as
         unsearchable as a zero-chunk doc.

    Filtering on ``embedding IS NOT NULL`` unifies both: a doc not in this set has
    no searchable content regardless of how many parent rows it carries. (Parent
    chunks are intentionally unembedded — retrieval fetches them by id after a
    child hit — so they must not count as "searchable".)
    """
    return (
        select(DocumentChunk.document_id)
        .where(DocumentChunk.embedding.isnot(None))
        .group_by(DocumentChunk.document_id)
        .subquery()
    )


def _unindexable_exists():
    """Correlated EXISTS marking a chunkless doc as **genuinely unindexable**.

    True when a COMPLETED processing attempt ran the full pipeline and **produced
    0 usable chunks** (``chunks_produced`` 0/NULL) with positive evidence that a
    retry won't help — either the quality gate dropped everything
    (``chunks_dropped_low_quality > 0``: OCR ran but yielded only low-quality
    text) OR it was a re-derivation (any non-``initial_ingest`` trigger, i.e. the
    doc was already re-indexed and still came up empty). Such a doc re-produces 0
    chunks on every pass, so re-indexing it is wasted OCR — it needs a better
    source scan, not a retry.

    The ``chunks_produced = 0`` guard is load-bearing: a doc that once produced
    real chunks (produced > 0) and later lost them out-of-band (manual delete,
    cascade, index maintenance) is genuinely REPAIRABLE — reindexing re-derives
    the same good chunks — even if that historical run also dropped a few
    low-quality ones. Without the guard, branch 1 would misclassify it as
    unindexable and refuse to repair it.

    A chunkless doc that does NOT match this is a transient 0-chunk doc worth
    re-indexing (fail-safe: reindex unless we have evidence it's hopeless).
    Correlates on the enclosing ``Document.id``.
    """
    dph = DocumentProcessingHistory
    return exists().where(
        dph.document_id == Document.id,
        dph.status == DOC_STATUS_COMPLETED,
        func.coalesce(dph.chunks_produced, 0) == 0,
        or_(
            func.coalesce(dph.chunks_dropped_low_quality, 0) > 0,
            dph.trigger != ProcessingTrigger.INITIAL_INGEST.value,
        ),
    )

def _low_coverage_exists(threshold: float, *, reindexable: bool):
    """Correlated EXISTS: the doc's LATEST completed processing run dropped MORE
    than ``threshold`` of its chunks — few/no usable chunks despite completing
    (the usable-but-garbled text-layer case the VLM coverage trigger recovers on
    re-processing).

    ``reindexable=True``  → that latest run was the INITIAL ingest (never
        re-attempted under the coverage pipeline) → worth re-processing.
    ``reindexable=False`` → that latest run was already a re-derivation and STILL
        low-coverage → the VLM couldn't rescue it → 'attempted', skip to avoid a
        re-OCR loop (``force`` includes it).

    Latest-run-only (correlated NOT EXISTS of a newer completed run) so a doc a
    prior reindex already fixed (low drop on its newest run) is not re-flagged.
    """
    dph = DocumentProcessingHistory
    newer = aliased(DocumentProcessingHistory)
    prod = func.coalesce(dph.chunks_produced, 0)
    drop = func.coalesce(dph.chunks_dropped_low_quality, 0)
    trig = (
        dph.trigger == ProcessingTrigger.INITIAL_INGEST.value
        if reindexable
        else dph.trigger != ProcessingTrigger.INITIAL_INGEST.value
    )
    return exists().where(
        dph.document_id == Document.id,
        dph.status == DOC_STATUS_COMPLETED,
        (prod + drop) > 0,
        drop > threshold * (prod + drop),   # drop_rate > threshold
        trig,
        ~exists().where(
            newer.document_id == dph.document_id,
            newer.status == DOC_STATUS_COMPLETED,
            newer.finished_at > dph.finished_at,
        ),
    )


async def sweep_low_coverage_reindex(cap: int = 50, threshold: float | None = None) -> dict:
    """Autonomous self-healing sweep: re-enqueue completed LOW-COVERAGE documents
    so the ingest-time VLM coverage trigger recovers them on re-processing.

    Bounded (``cap``), idempotent, skips already-attempted + in-flight (only
    ``status=completed`` matches). No permission gate — invoked by the scheduled
    engine, not a user. force_ocr is deliberately False so the text-layer→VLM
    coverage path runs (force-OCR would drop positioned tokens). Returns
    ``{enqueued, skipped_attempted}``. Mirrors ``reindex_documents``'s
    enqueue-then-flip-status crash-safety.
    """
    if threshold is None:
        threshold = settings.ocr_vlm_coverage_drop_threshold
    if threshold <= 0:
        return {"enqueued": 0, "skipped_attempted": 0}

    async with AsyncSessionLocal() as db:
        attempted = (
            await db.execute(
                select(func.count()).select_from(Document).where(
                    Document.status == DOC_STATUS_COMPLETED,
                    _low_coverage_exists(threshold, reindexable=False),
                )
            )
        ).scalar() or 0
        doc_ids = list(
            (
                await db.execute(
                    select(Document.id)
                    .where(
                        Document.status == DOC_STATUS_COMPLETED,
                        _low_coverage_exists(threshold, reindexable=True),
                    )
                    .order_by(Document.id)
                    .limit(cap)
                )
            )
            .scalars()
            .all()
        )

    if not doc_ids:
        return {"enqueued": 0, "skipped_attempted": attempted}

    from services.redis_client import get_redis
    from services.task_queue import DocumentTaskQueue

    queue = DocumentTaskQueue(redis_client=get_redis())
    enqueued: list[int] = []
    for did in doc_ids:
        try:
            await queue.enqueue(
                {"document_id": did, "force_ocr": False, "user_id": None, "trigger": "user_reindex"}
            )
            enqueued.append(did)
        except Exception as e:  # noqa: BLE001 — one bad enqueue mustn't abort the batch
            logger.warning(f"low_coverage_reindex: enqueue failed for doc {did}: {e}")

    if enqueued:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Document)
                .where(Document.id.in_(enqueued), Document.status == DOC_STATUS_COMPLETED)
                .values(status=DOC_STATUS_PENDING, error_message=None)
            )
            await db.commit()

    return {"enqueued": len(enqueued), "skipped_attempted": attempted}


# Registered with the agent tool registry by
# `services/agent_tools.py::_register_internal_tools()`.
KB_MAINTENANCE_TOOLS: dict = {
    "internal.ingest_status": {
        "description": (
            "Report the CURRENT processing status of the document pipeline — "
            "knowledge-base indexing AND Paperless filing. Use for questions like "
            "'wie ist der Verarbeitungsstatus?', 'werden noch Dokumente "
            "verarbeitet?', 'gibt es einen Rückstau?', 'sind alle Dokumente in "
            "Paperless abgelegt?'. Returns how many documents are pending / "
            "processing / completed / failed, how many completed documents are NOT "
            "SEARCHABLE (no embedded chunk — an empty index OR only unembedded "
            "parent chunks) — split into REPAIRABLE (worth reindexing) vs genuinely "
            "UNINDEXABLE (unreadable scans the quality gate keeps rejecting) — the "
            "worker queue depth and whether the worker is alive, and the Paperless "
            "filing state (filed / pending / failed / not-filed)."
        ),
        "parameters": {},
    },
    "internal.reindex_documents": {
        "description": (
            "Re-index documents in the knowledge base that are NOT SEARCHABLE — no "
            "embedded chunk (finished indexing but produced no retrievable content: "
            "no chunks, or only unembedded parent chunks). Enqueues a "
            "background reindex (purge + rebuild) for each and reports how many "
            "were queued. Use when the user asks to 'reindex documents without "
            "chunks', 'Dokumente ohne Chunks neu indexieren', 'nicht durchsuchbare "
            "Dokumente reparieren', or 'repariere die leeren Dokumente'. THIS is also "
            "the tool for an OCR RE-RUN of unreadable/unindexable KB documents — "
            "'erzwungener Neulauf mit OCR', 'die Dokumente ohne Chunks mit OCR neu "
            "erkennen', 'unlesbare Scans neu verarbeiten': call it with force=true AND "
            "force_ocr=true (this re-runs full-page OCR in the KB pipeline — it is NOT "
            "a Paperless upload/forward, do NOT use forward_attachment_to_paperless or "
            "any Paperless tool for this). By DEFAULT skips documents already known to "
            "be genuinely unindexable (an unreadable scan the quality gate keeps "
            "rejecting — a plain retry just re-produces 0 chunks); pass force=true to "
            "reindex those too. Does nothing to documents that already have chunks "
            "or are currently being processed."
        ),
        "parameters": {
            "limit": (
                "Max documents to reindex in one call (optional; default "
                f"{REINDEX_DEFAULT_CAP}, max {REINDEX_MAX_CAP})"
            ),
            "force": (
                "Also reindex documents classified as genuinely unindexable "
                "(unreadable scans). Optional; default false — only repairable "
                "docs are reindexed. Set true for 'auch die unlesbaren' / "
                "'trotzdem alle neu indexieren'."
            ),
            "force_ocr": (
                "Re-run FULL-PAGE OCR (force_full_page_ocr) instead of reusing the "
                "existing text layer. Optional; default false. Use for scanned "
                "documents whose text extraction was garbled ('nochmal mit OCR', "
                "'als Scan neu erkennen'). Pairs well with force=true to give the "
                "unindexable docs a real second chance."
            ),
        },
    },
    "internal.list_chunkless_documents": {
        "description": (
            "List BY NAME the knowledge-base documents that are NOT SEARCHABLE — no "
            "embedded chunk (finished indexing but produced no retrievable content: "
            "no chunks, or only unembedded parent chunks). Use "
            "when the user wants to SEE WHICH documents are affected or their "
            "titles: 'welche Dokumente haben keine Chunks?', 'liste die leeren "
            "Dokumente auf', 'nenne mir die Titel der Dokumente ohne Chunks', "
            "'which documents have no chunks'. Each entry is labelled REPAIRABLE "
            "or UNINDEXABLE (unreadable scan). Returns id + display name for each, "
            "newest first. (For just the COUNT use ingest_status; to fix them use "
            "reindex_documents.)"
        ),
        "parameters": {
            "limit": (
                "Max documents to list (optional; default "
                f"{LIST_DEFAULT_LIMIT}, max {LIST_MAX_LIMIT})"
            ),
        },
    },
    "internal.list_unfiled_documents": {
        "description": (
            "Report the PAPERLESS FILING status of documents BY NAME. Two modes: "
            "(1) NO query → list the documents that are NOT (successfully) filed in "
            "Paperless — the ones whose filing FAILED or is still pending. Use for "
            "'welche Dokumente sind nicht in Paperless?', 'welche Dokumente konnten "
            "nicht abgelegt werden?', 'welche Dokumente fehlen in Paperless?', "
            "'which documents failed to file to Paperless?'. "
            "(2) WITH a query (a document name / invoice number / correspondent) → "
            "check whether THAT specific document is in Paperless and report its "
            "filing status. Use for 'ist die Rechnung X in Paperless?', 'ist das "
            "Dokument Y abgelegt?', 'is document Z in Paperless?'. Returns id + "
            "display name + filing state (filed/failed/pending/not-intended) and, "
            "when filed, the Paperless document id. This is the FILING-status "
            "readout; for the COUNT use ingest_status, for DUPLICATES use the dedupe "
            "tools."
        ),
        "parameters": {
            "query": (
                "Optional: a document name, invoice number, or correspondent to "
                "check a SPECIFIC document's Paperless status. Omit to list ALL "
                "documents that are not (successfully) filed."
            ),
            "limit": (
                "Max documents to list (optional; default "
                f"{LIST_DEFAULT_LIMIT}, max {LIST_MAX_LIMIT})"
            ),
        },
    },
    "internal.refile_to_paperless": {
        "description": (
            "RE-FILE documents to Paperless whose filing FAILED. Use when the user "
            "wants to FIX / retry the Paperless filing: 'lege die fehlgeschlagenen "
            "Dokumente in Paperless ab', 'versuche die fehlgeschlagenen Ablagen "
            "erneut', 'stelle die fehlenden Dokumente in Paperless ein', 're-file "
            "the failed documents to Paperless', 'retry filing to Paperless'. It "
            "resets each FAILED document and queues a fresh Paperless upload in the "
            "document worker (the backend never runs the heavy OCR itself). With a "
            "query it re-files only the matching failed document(s). Reports how many "
            "were queued. (To SEE which failed first, use list_unfiled_documents.)"
        ),
        "parameters": {
            "query": (
                "Optional: a document name / invoice number / correspondent to "
                "re-file only THAT failed document. Omit to re-file all failed ones."
            ),
            "limit": (
                "Max documents to re-file (optional; default "
                f"{REINDEX_DEFAULT_CAP}, max {REINDEX_MAX_CAP})"
            ),
        },
    },
}

# Redis lease key mirrors services/paperless_reconciler so a chat-triggered
# re-file and the periodic reconciler never double-enqueue the same doc.
_REFILE_LEASE_KEY = "paperless:refile:lease:{doc_id}"

# paperless_state → human label for the status readout.
_PL_LABELS = {
    "done": "abgelegt",
    "pending": "ausstehend",
    "failed": "fehlgeschlagen",
    "unfiled": "nicht vorgesehen",  # NULL → interactive uploads, never filed
}


async def ingest_worker_and_backlog() -> tuple[bool | None, int | None]:
    """Shared ingest-liveness probe → ``(worker_alive, live pending backlog)``.

    Backlog is ``pending_count`` (Redis XPENDING — delivered-but-unacked tasks),
    which DRAINS as the worker acks. Deliberately NOT ``stream_length`` (XLEN):
    the stream is never trimmed, so XLEN counts every task ever enqueued and only
    grows — it can't signal a live backlog (a caught-up worker would still read
    "overloaded" forever). Shared by ``ingest_status`` and the kiosk
    knowledge-health node so the two never diverge.
    """
    from api.routes.knowledge import _worker_is_alive
    from services.redis_client import get_redis
    from services.task_queue import DocumentTaskQueue

    worker_alive = await _worker_is_alive()
    backlog = await DocumentTaskQueue(redis_client=get_redis()).pending_count()
    return worker_alive, backlog


async def ingest_status(params: dict, user_id: int | None = None) -> dict:
    """Read-only snapshot of the ingest → KB → Paperless pipeline."""
    try:
        async with AsyncSessionLocal() as db:
            status_counts = {
                r[0]: r[1]
                for r in (
                    await db.execute(
                        select(Document.status, func.count()).group_by(Document.status)
                    )
                ).all()
            }
            # completed docs with no SEARCHABLE (embedded) chunk — zero-chunk docs
            # AND parent-only docs (see _searchable_chunk_subquery)
            chunk_sub = _searchable_chunk_subquery()
            chunkless = (
                await db.execute(
                    select(func.count())
                    .select_from(Document)
                    .outerjoin(chunk_sub, chunk_sub.c.document_id == Document.id)
                    .where(
                        Document.status == DOC_STATUS_COMPLETED,
                        chunk_sub.c.document_id.is_(None),
                    )
                )
            ).scalar()
            # of those chunkless docs, how many are genuinely unindexable
            unindexable = (
                await db.execute(
                    select(func.count())
                    .select_from(Document)
                    .outerjoin(chunk_sub, chunk_sub.c.document_id == Document.id)
                    .where(
                        Document.status == DOC_STATUS_COMPLETED,
                        chunk_sub.c.document_id.is_(None),
                        _unindexable_exists(),
                    )
                )
            ).scalar()
            pl_counts = {
                (r[0] or "unfiled"): r[1]
                for r in (
                    await db.execute(
                        select(Document.paperless_state, func.count()).group_by(
                            Document.paperless_state
                        )
                    )
                ).all()
            }

            # #1166: of the docs marked filed ('done'), how many actually LINK to a
            # Paperless document id (verified present) vs. are unlinked (the id was
            # never recorded → 'done' is unverified). Backfill links the rest.
            pl_done_linked = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(Document)
                        .where(
                            Document.paperless_state == "done",
                            Document.paperless_document_id.isnot(None),
                        )
                    )
                ).scalar()
                or 0
            )

        pl_done_unlinked = max(0, int(pl_counts.get("done", 0)) - pl_done_linked)

        # worker liveness + live backlog (best-effort; never fail the readout)
        worker_alive = None
        queue_depth = None
        try:
            worker_alive, queue_depth = await ingest_worker_and_backlog()
        except Exception as e:  # noqa: BLE001 - liveness is a nice-to-have
            logger.warning(f"ingest_status: worker/queue probe failed: {e}")

        pending = status_counts.get("pending", 0)
        processing = status_counts.get("processing", 0)
        completed = status_counts.get("completed", 0)
        failed = status_counts.get("failed", 0)
        # PDF-split lifecycle (status contract): archived combined originals +
        # split-lane in-flight rows must not vanish from the narrative counts.
        split_archived = status_counts.get(DOC_STATUS_SPLIT_ARCHIVED, 0)
        split_in_flight = status_counts.get(
            DOC_STATUS_SPLIT_PENDING, 0
        ) + status_counts.get(DOC_STATUS_SPLIT_REVIEW, 0)
        pl_pending = pl_counts.get("pending", 0)
        pl_failed = pl_counts.get("failed", 0)
        chunkless = int(chunkless or 0)
        unindexable = int(unindexable or 0)
        reindexable = max(0, chunkless - unindexable)

        parts = [
            f"KB-Verarbeitung: {completed} fertig, {pending} in Warteschlange, "
            f"{processing} in Arbeit, {failed} fehlgeschlagen."
        ]
        if split_archived or split_in_flight:
            split_bits = []
            if split_archived:
                split_bits.append(
                    f"{split_archived} kombinierte Original-PDF(s) nach "
                    f"Aufteilung archiviert (Einzeldokumente separat indexiert, "
                    f"Original bewusst NICHT in Paperless)"
                )
            if split_in_flight:
                split_bits.append(
                    f"{split_in_flight} PDF(s) in der Split-Prüfung/-Verarbeitung"
                )
            parts.append("PDF-Split: " + "; ".join(split_bits) + ".")
        if chunkless:
            if unindexable and reindexable:
                detail = (
                    f"{reindexable} reparierbar (neu indexieren) und "
                    f"{unindexable} vermutlich unlesbar (nicht durch Neu-Indexieren zu beheben)"
                )
            elif unindexable:
                detail = (
                    "alle vermutlich unlesbare Scans — nicht durch Neu-Indexieren "
                    "zu beheben (neuer Scan nötig)"
                )
            else:
                detail = "mit 'Dokumente ohne Chunks neu indexieren' reparierbar"
            parts.append(
                f"{chunkless} fertige Dokument(e) haben KEINE Chunks ({detail})."
            )
        pl_bits = ", ".join(
            f"{v} {_PL_LABELS.get(k, k)}" for k, v in sorted(pl_counts.items())
        )
        parts.append(f"Paperless: {pl_bits}.")
        if pl_done_unlinked:
            parts.append(
                f"Hinweis: {pl_done_unlinked} als abgelegt markierte Dokument(e) sind noch "
                f"nicht mit ihrer Paperless-ID verknüpft (Status unbestätigt; "
                f"`bin/backfill_paperless_document_ids.py` verknüpft sie per Prüfsumme)."
            )
        if worker_alive is not None:
            parts.append(
                f"Worker: {'aktiv' if worker_alive else 'NICHT erreichbar'}"
                + (f", {queue_depth} Aufgabe(n) in der Queue." if queue_depth is not None else ".")
            )

        return {
            "success": True,
            "message": " ".join(parts),
            "action_taken": True,
            "data": {
                "documents_by_status": status_counts,
                "completed_without_chunks": chunkless,
                "chunkless_reindexable": reindexable,
                "chunkless_unindexable": unindexable,
                "paperless_state": pl_counts,
                "paperless_pending": pl_pending,
                "paperless_failed": pl_failed,
                "paperless_done_linked": pl_done_linked,
                "paperless_done_unlinked": pl_done_unlinked,
                "worker_alive": worker_alive,
                "queue_depth": queue_depth,
            },
        }
    except Exception as e:
        logger.error(f"Error in ingest_status: {e}")
        return {
            "success": False,
            "message": f"Status-Abfrage fehlgeschlagen: {e!s}",
            "action_taken": False,
        }


async def reindex_documents(
    params: dict,
    user_id: int | None = None,
    user_permissions: list[str] | None = None,
) -> dict:
    """Enqueue a reindex (purge + rebuild) for every completed doc with no searchable
    (embedded) chunk — zero-chunk AND parent-only docs (``_searchable_chunk_subquery``).

    Gated on ``Permission.RAG_MANAGE`` when auth is enabled: ``user_permissions``
    is None (auth-off OR unidentified voice turn) is allowed — matching the
    platform's HA_CONTROL convention — but an authenticated user lacking
    rag.manage is refused (a low-privilege member can't trigger a fleet re-OCR).

    Known limitation: a parent-only doc (produced parent chunks but every embedded
    child was dropped) is classified REPAIRABLE (``_unindexable_exists`` keys on
    ``chunks_produced=0``, and a parent-only doc produced >0). A reindex re-embeds it,
    which FIXES the common case (a transient embed-endpoint outage during a bulk
    ingest). But a doc whose children fail EVERY time (e.g. content the quality gate
    always rejects) stays unsearchable and is re-offered on each manual call — bounded
    by manual invocation, not an auto-loop. History doesn't record embed-level outcomes,
    so auto-classifying these as unindexable would need a schema change (deferred);
    surfacing + retrying them is still strictly better than the prior silent invisibility.
    """
    if settings.auth_enabled and user_permissions is not None:
        if not has_permission(user_permissions, Permission.RAG_MANAGE):
            return {
                "success": False,
                "message": (
                    "Zum Neu-Indexieren fehlt die Berechtigung "
                    "(rag.manage / Dokumentenverwaltung)."
                ),
                "action_taken": False,
            }

    cap = REINDEX_DEFAULT_CAP
    if params.get("limit"):
        try:
            cap = max(1, min(REINDEX_MAX_CAP, int(params["limit"])))
        except (ValueError, TypeError):
            pass
    force = _as_bool(params.get("force"))
    force_ocr = _as_bool(params.get("force_ocr"))

    try:
        async with AsyncSessionLocal() as db:
            # completed docs with no SEARCHABLE (embedded) chunk — zero-chunk AND
            # parent-only docs (see _searchable_chunk_subquery). Excludes
            # pending/processing by construction, so no in-flight double-enqueue —
            # mirrors the route's dedup guard.
            chunk_sub = _searchable_chunk_subquery()
            chunkless_where = (
                Document.status == DOC_STATUS_COMPLETED,
                chunk_sub.c.document_id.is_(None),
            )
            # Accurate skip count (the FULL unindexable population, not just the
            # capped window). 0 when force, since none are skipped.
            unindexable_count = (
                await db.execute(
                    select(func.count())
                    .select_from(Document)
                    .outerjoin(chunk_sub, chunk_sub.c.document_id == Document.id)
                    .where(*chunkless_where, _unindexable_exists())
                )
            ).scalar() or 0

            # Select the ids to reindex. By default filter OUT unindexable docs so
            # the cap applies to REPAIRABLE docs only (no cap-starvation), and
            # len==cap honestly means "more repairable work follows". force=true
            # includes the unindexable ones.
            id_query = (
                select(Document.id)
                .select_from(Document)
                .outerjoin(chunk_sub, chunk_sub.c.document_id == Document.id)
                .where(*chunkless_where)
            )
            if not force:
                id_query = id_query.where(~_unindexable_exists())
            doc_ids = list(
                (
                    await db.execute(id_query.order_by(Document.id).limit(cap))
                )
                .scalars()
                .all()
            )

        skipped_unindexable = 0 if force else unindexable_count

        if not doc_ids:
            if skipped_unindexable:
                return {
                    "success": True,
                    "message": (
                        f"{skipped_unindexable} Dokument(e) ohne Chunks gefunden, aber "
                        "alle sind vermutlich unlesbare Scans — Neu-Indexieren hilft "
                        "nicht (ein neuer Scan ist nötig). Mit force=true trotzdem "
                        "erneut versuchen."
                    ),
                    "action_taken": True,
                    "empty_result": True,
                    "data": {"reindexed": 0, "skipped_unindexable": skipped_unindexable},
                }
            return {
                "success": True,
                "message": "Keine fertigen Dokumente ohne Chunks gefunden — nichts zu tun.",
                "action_taken": True,
                "empty_result": True,
                "data": {"reindexed": 0, "skipped_unindexable": 0},
            }

        # Enqueue FIRST, then flip only the successfully-enqueued docs to
        # 'pending'. The worker reprocesses a user_reindex regardless of the
        # doc's status, so enqueue-then-flip avoids the orphan a flip-then-enqueue
        # crash would leave (a doc stuck 'pending' with no task). A failed enqueue
        # simply leaves the doc 'completed' — retried on the next call — instead
        # of stranded.
        from services.redis_client import get_redis
        from services.task_queue import DocumentTaskQueue

        queue = DocumentTaskQueue(redis_client=get_redis())
        enqueued_ids: list[int] = []
        for did in doc_ids:
            try:
                await queue.enqueue(
                    {
                        "document_id": did,
                        "force_ocr": force_ocr,
                        "user_id": user_id,
                        "trigger": "user_reindex",
                    }
                )
                enqueued_ids.append(did)
            except Exception as e:  # noqa: BLE001 - one bad enqueue mustn't abort the batch
                logger.warning(f"reindex_documents: enqueue failed for doc {did}: {e}")

        # Had work but nothing could be enqueued → the queue is unreachable.
        # Report failure rather than a success with reindexed=0 (which is
        # indistinguishable from the legitimate "nothing to do" path and would
        # mislead the operator during exactly the outage this tool diagnoses).
        if not enqueued_ids:
            return {
                "success": False,
                "message": "Einreihen fehlgeschlagen — die Aufgaben-Queue ist nicht erreichbar.",
                "action_taken": False,
                "data": {"reindexed": 0},
            }

        # Cosmetic status flip so the KB list/poll shows them queued (the worker
        # sets the real state as it processes). Guard on status=completed so a
        # doc the worker already advanced (fast-worker race across the three
        # separate transactions) isn't dragged back to 'pending' with no task.
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Document)
                .where(
                    Document.id.in_(enqueued_ids),
                    Document.status == DOC_STATUS_COMPLETED,
                )
                .values(status=DOC_STATUS_PENDING, error_message=None)
            )
            await db.commit()

        # doc_ids only contains docs that WILL be enqueued (repairable, or all
        # when force), so len==cap honestly means more enqueueable docs follow.
        more = " (weitere folgen beim nächsten Aufruf)" if len(doc_ids) == cap else ""
        skip_note = (
            f" {skipped_unindexable} vermutlich unlesbare(s) Dokument(e) übersprungen "
            f"(force=true, um sie einzuschließen)."
            if skipped_unindexable
            else ""
        )
        return {
            "success": True,
            "message": (
                f"{len(enqueued_ids)} Dokument(e) ohne Chunks zum Neu-Indexieren "
                f"eingereiht{more}. Die Verarbeitung läuft im Hintergrund.{skip_note}"
            ),
            "action_taken": True,
            "data": {
                "reindexed": len(enqueued_ids),
                "document_ids": enqueued_ids,
                "skipped_unindexable": skipped_unindexable,
            },
        }
    except Exception as e:
        logger.error(f"Error in reindex_documents: {e}")
        return {
            "success": False,
            "message": f"Neu-Indexieren fehlgeschlagen: {e!s}",
            "action_taken": False,
        }


async def list_chunkless_documents(params: dict, user_id: int | None = None) -> dict:
    """List completed docs with no chunk rows, by display name (newest first)."""
    limit = LIST_DEFAULT_LIMIT
    if params.get("limit"):
        try:
            limit = max(1, min(LIST_MAX_LIMIT, int(params["limit"])))
        except (ValueError, TypeError):
            pass

    try:
        # display_name = generated_title → title → filename (matches the KB list).
        display_name = func.coalesce(
            Document.generated_title, Document.title, Document.filename
        )
        # no SEARCHABLE (embedded) chunk — zero-chunk AND parent-only docs
        chunk_sub = _searchable_chunk_subquery()
        unindexable_col = _unindexable_exists().label("unindexable")
        base = (
            select(Document.id, display_name, unindexable_col)
            .select_from(Document)
            .outerjoin(chunk_sub, chunk_sub.c.document_id == Document.id)
            .where(
                Document.status == DOC_STATUS_COMPLETED,
                chunk_sub.c.document_id.is_(None),
            )
        )
        count_base = (
            select(Document.id)
            .select_from(Document)
            .outerjoin(chunk_sub, chunk_sub.c.document_id == Document.id)
            .where(
                Document.status == DOC_STATUS_COMPLETED,
                chunk_sub.c.document_id.is_(None),
            )
        )
        async with AsyncSessionLocal() as db:
            total = (
                await db.execute(
                    select(func.count()).select_from(count_base.subquery())
                )
            ).scalar() or 0
            total_unindexable = (
                await db.execute(
                    select(func.count()).select_from(
                        count_base.where(_unindexable_exists()).subquery()
                    )
                )
            ).scalar() or 0
            rows = (
                await db.execute(base.order_by(Document.id.desc()).limit(limit))
            ).all()

        if not rows:
            return {
                "success": True,
                "message": "Alle fertigen Dokumente haben Chunks — keine leeren Dokumente.",
                "action_taken": True,
                "empty_result": True,
                "data": {"count": 0, "total": 0, "documents": []},
            }

        documents = [
            {"id": r[0], "name": r[1], "unindexable": bool(r[2])} for r in rows
        ]
        repairable = [d for d in documents if not d["unindexable"]]
        unindexable = [d for d in documents if d["unindexable"]]
        total_repairable = max(0, total - total_unindexable)

        sections = []
        if repairable:
            sections.append(
                "Reparierbar (neu indexieren):\n"
                + "\n".join(f"- {d['name']} (#{d['id']})" for d in repairable)
            )
        if unindexable:
            sections.append(
                "Vermutlich unlesbar (neuer Scan nötig, Neu-Indexieren hilft nicht):\n"
                + "\n".join(f"- {d['name']} (#{d['id']})" for d in unindexable)
            )
        truncated = total > len(documents)
        suffix = (
            f" (zeige {len(documents)} von {total}; höheres 'limit' für mehr)"
            if truncated
            else ""
        )
        header = (
            f"{total} Dokument(e) ohne Chunks — {total_repairable} reparierbar, "
            f"{total_unindexable} vermutlich unlesbar{suffix}:"
        )
        return {
            "success": True,
            "message": header + "\n" + "\n\n".join(sections),
            "action_taken": True,
            "data": {
                "count": len(documents),
                "total": total,
                "total_repairable": total_repairable,
                "total_unindexable": total_unindexable,
                "truncated": truncated,
                "documents": documents,
            },
        }
    except Exception as e:
        logger.error(f"Error in list_chunkless_documents: {e}")
        return {
            "success": False,
            "message": f"Auflisten fehlgeschlagen: {e!s}",
            "action_taken": False,
        }


# paperless_state values that mean "should be in Paperless but isn't (yet)".
_UNFILED_STATES = ("failed", "pending")


def _filing_status(state: str | None, pid: int | None) -> tuple[str, bool]:
    """(human label, is_in_paperless) for a document's Paperless filing state."""
    if state == "done":
        if pid:
            return (f"in Paperless abgelegt (Paperless-Dokument #{pid})", True)
        # done without a linked id = Paperless accepted it as a duplicate of an
        # existing document at consume time (no separate Paperless doc created).
        return ("in Paperless vorhanden (als Duplikat einer bestehenden Ablage)", True)
    if state == "failed":
        return ("Ablage FEHLGESCHLAGEN — nicht in Paperless", False)
    if state == "pending":
        return ("Ablage läuft noch (ausstehend)", False)
    # NULL → interactive/normal upload, never routed to Paperless by design.
    return ("nicht für Paperless vorgesehen (interner Upload)", False)


def _escape_like(term: str) -> str:
    """Escape LIKE/ILIKE wildcards so a query containing %/_ matches literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_unfiled_documents(
    params: dict,
    user_id: int | None = None,
    user_permissions: list[str] | None = None,
) -> dict:
    """Report Paperless filing status by name.

    No ``query`` → list completed documents that are NOT successfully filed
    (paperless_state failed/pending). With ``query`` → report the filing status of
    the matching document(s), so the user can ask "is document X in Paperless?".

    **Owner-scoped when auth is on:** a non-admin caller sees/searches only their
    OWN documents (by the atom owner); an ADMIN — or the auth-off single-user —
    sees all. This keeps the query mode from becoming a corpus-wide document-name
    search reachable by a low-privilege user on a multi-user instance (xidra).
    """
    limit = LIST_DEFAULT_LIMIT
    if params.get("limit"):
        try:
            limit = max(1, min(LIST_MAX_LIMIT, int(params["limit"])))
        except (ValueError, TypeError):
            pass
    query = (params.get("query") or "").strip()

    # Owner scope: auth on + a known non-admin caller → own documents only.
    apply_scope = (
        settings.auth_enabled
        and user_id is not None
        and not has_permission(user_permissions or [], Permission.ADMIN)
    )

    def _scoped(stmt):
        """Restrict a Document select to the caller's own docs (atom owner).

        INNER-joins atoms, so an atom-less (owner-less) document is invisible to a
        scoped non-admin caller — fail-closed, the intended privacy behavior."""
        if not apply_scope:
            return stmt
        return stmt.join(Atom, Document.atom_id == Atom.atom_id).where(
            Atom.owner_user_id == user_id
        )

    display_name = func.coalesce(
        Document.generated_title, Document.title, Document.filename
    )

    try:
        async with AsyncSessionLocal() as db:
            if query:
                like = f"%{_escape_like(query)}%"
                rows = (
                    await db.execute(
                        _scoped(
                            select(
                                Document.id,
                                display_name,
                                Document.paperless_state,
                                Document.paperless_document_id,
                            ).where(
                                Document.status == DOC_STATUS_COMPLETED,
                                or_(
                                    Document.generated_title.ilike(like),
                                    Document.title.ilike(like),
                                    Document.filename.ilike(like),
                                ),
                            )
                        )
                        .order_by(Document.id.desc())
                        .limit(limit)
                    )
                ).all()

                if not rows:
                    return {
                        "success": True,
                        "message": (
                            f"Ich habe kein Dokument gefunden, das zu „{query}“ passt."
                        ),
                        "action_taken": True,
                        "empty_result": True,
                        "data": {"query": query, "count": 0, "documents": []},
                    }

                documents = []
                lines = []
                for _id, name, state, pid in rows:
                    label, in_pl = _filing_status(state, pid)
                    documents.append(
                        {
                            "id": _id,
                            "name": name,
                            "paperless_state": state,
                            "paperless_document_id": pid,
                            "in_paperless": in_pl,
                            "status_label": label,
                        }
                    )
                    lines.append(f"- {name} (#{_id}): {label}")
                return {
                    "success": True,
                    "message": (
                        f"Ablage-Status zu „{query}“:\n" + "\n".join(lines)
                    ),
                    "action_taken": True,
                    "data": {"query": query, "count": len(documents), "documents": documents},
                }

            # list mode: documents that should be filed but aren't
            base = _scoped(
                select(
                    Document.id, display_name, Document.paperless_state,
                    Document.paperless_document_id,
                )
                .where(
                    Document.status == DOC_STATUS_COMPLETED,
                    Document.paperless_state.in_(_UNFILED_STATES),
                )
            )
            total = (
                await db.execute(
                    select(func.count()).select_from(
                        _scoped(
                            select(Document.id)
                            .where(
                                Document.status == DOC_STATUS_COMPLETED,
                                Document.paperless_state.in_(_UNFILED_STATES),
                            )
                        )
                        .subquery()
                    )
                )
            ).scalar() or 0
            rows = (
                await db.execute(base.order_by(Document.id.desc()).limit(limit))
            ).all()

        if not rows:
            return {
                "success": True,
                "message": (
                    "Alle für Paperless vorgesehenen Dokumente sind abgelegt — "
                    "keine fehlgeschlagenen oder ausstehenden Ablagen."
                ),
                "action_taken": True,
                "empty_result": True,
                "data": {"count": 0, "total": 0, "documents": []},
            }

        documents = []
        lines = []
        for _id, name, state, pid in rows:
            label, in_pl = _filing_status(state, pid)
            documents.append(
                {
                    "id": _id, "name": name, "paperless_state": state,
                    "paperless_document_id": pid, "in_paperless": in_pl,
                    "status_label": label,
                }
            )
            lines.append(f"- {name} (#{_id}): {label}")
        truncated = total > len(documents)
        suffix = (
            f" (zeige {len(documents)} von {total}; höheres 'limit' für mehr)"
            if truncated else ""
        )
        header = f"{total} Dokument(e) nicht (erfolgreich) in Paperless abgelegt{suffix}:"
        return {
            "success": True,
            "message": header + "\n" + "\n".join(lines),
            "action_taken": True,
            "data": {
                "count": len(documents), "total": total,
                "truncated": truncated, "documents": documents,
            },
        }
    except Exception as e:
        logger.error(f"Error in list_unfiled_documents: {e}")
        return {
            "success": False,
            "message": f"Abfrage fehlgeschlagen: {e!s}",
            "action_taken": False,
        }


async def refile_to_paperless(
    params: dict,
    user_id: int | None = None,
    user_permissions: list[str] | None = None,
) -> dict:
    """Re-file FAILED documents to Paperless: reset each + enqueue a worker refile.

    Targets only ``paperless_state='failed'`` (the genuinely stuck ones) — pending
    docs are already handled by the periodic reconciler, and clearing their
    ``paperless_task_id`` would risk a duplicate re-upload (the re-ingest-loop bug).
    A failed doc's task is terminal, so we clear it and flip to ``pending`` so the
    worker leg does a FRESH upload. The heavy Docling/upload runs in the document
    worker (``paperless_refile`` task) — the backend only resets + enqueues, never
    OCRs (the OOM lesson). Flip-then-enqueue is safe: a failed enqueue leaves the
    doc ``pending``, which the periodic reconciler re-enqueues as a backstop.

    Gated on ``Permission.RAG_MANAGE`` when auth is enabled (auth-off / unidentified
    turn allowed; an authenticated low-privilege user is refused).
    """
    if settings.auth_enabled and user_permissions is not None:
        if not has_permission(user_permissions, Permission.RAG_MANAGE):
            return {
                "success": False,
                "message": (
                    "Zum erneuten Ablegen in Paperless fehlt die Berechtigung "
                    "(rag.manage / Dokumentenverwaltung)."
                ),
                "action_taken": False,
            }

    cap = REINDEX_DEFAULT_CAP
    if params.get("limit"):
        try:
            cap = max(1, min(REINDEX_MAX_CAP, int(params["limit"])))
        except (ValueError, TypeError):
            pass
    query = (params.get("query") or "").strip()
    display_name = func.coalesce(
        Document.generated_title, Document.title, Document.filename
    )

    try:
        async with AsyncSessionLocal() as db:
            # failed docs (+ their atom owner, for owner-scoped Paperless metadata),
            # optionally filtered by name.
            q = (
                select(Document.id, Atom.owner_user_id)
                .join(Atom, Document.atom_id == Atom.atom_id, isouter=True)
                .where(
                    Document.status == DOC_STATUS_COMPLETED,
                    Document.paperless_state == PAPERLESS_STATE_FAILED,
                )
            )
            if query:
                like = f"%{query}%"
                q = q.where(
                    or_(
                        Document.generated_title.ilike(like),
                        Document.title.ilike(like),
                        Document.filename.ilike(like),
                    )
                )
            rows = (await db.execute(q.order_by(Document.id).limit(cap))).all()

            if not rows:
                msg = (
                    f"Kein fehlgeschlagenes Dokument gefunden, das zu „{query}“ passt."
                    if query
                    else "Keine fehlgeschlagenen Paperless-Ablagen — nichts erneut abzulegen."
                )
                return {
                    "success": True,
                    "message": msg,
                    "action_taken": True,
                    "empty_result": True,
                    "data": {"queued": 0},
                }

            ids = [r[0] for r in rows]
            # Flip failed → pending + clear the terminal task_id so the worker leg
            # does a fresh upload (guard on paperless_state so a concurrent settle
            # isn't overwritten).
            await db.execute(
                update(Document)
                .where(
                    Document.id.in_(ids),
                    Document.paperless_state == PAPERLESS_STATE_FAILED,
                )
                .values(
                    paperless_state=PAPERLESS_STATE_PENDING,
                    paperless_task_id=None,
                    error_message=None,
                )
            )
            await db.commit()

        from services.redis_client import get_redis
        from services.task_queue import DocumentTaskQueue

        redis = get_redis()
        ttl = settings.paperless_reconciler_refile_lease_seconds
        queue = DocumentTaskQueue(redis_client=redis)
        queued = 0
        for doc_id, owner_user_id in rows:
            try:
                # Lease so a concurrent periodic reconciler doesn't double-enqueue.
                acquired = await redis.set(
                    _REFILE_LEASE_KEY.format(doc_id=doc_id), "1", nx=True, ex=ttl
                )
                if not acquired:
                    continue
                await queue.enqueue(
                    {
                        "document_id": doc_id,
                        "trigger": "paperless_refile",
                        "user_id": owner_user_id,
                    }
                )
                queued += 1
            except Exception as e:  # noqa: BLE001 — one bad enqueue mustn't abort the batch
                logger.warning(f"refile_to_paperless: enqueue failed for doc {doc_id}: {e}")

        # The docs are already flipped to 'pending', so even if every direct enqueue
        # failed the periodic reconciler re-enqueues them — report honestly.
        more = " (weitere folgen beim nächsten Aufruf)" if len(rows) == cap else ""
        return {
            "success": True,
            "message": (
                f"{len(rows)} fehlgeschlagene(s) Dokument(e) für die erneute "
                f"Paperless-Ablage vorgemerkt, {queued} sofort eingereiht{more}. "
                "Die Ablage läuft im Hintergrund (Dokument-Worker)."
            ),
            "action_taken": True,
            "data": {"targeted": len(rows), "queued": queued, "truncated": len(rows) == cap},
        }
    except Exception as e:
        logger.error(f"Error in refile_to_paperless: {e}")
        return {
            "success": False,
            "message": f"Erneutes Ablegen fehlgeschlagen: {e!s}",
            "action_taken": False,
        }
