"""Retry re-enqueuer for Paperless filing — LIGHT, no Docling in the backend.

The document-worker files each folder/email-ingest document into Paperless during
ingest (``paperless_filing_hook``), reusing the worker's own high-quality Docling
OCR. That is the normal path. If the hook couldn't settle (Paperless was down when
the doc was processed), the doc stays ``paperless_state='pending'`` after it has
``completed``.

This periodic backend scan re-ENQUEUES those stragglers as ``paperless_refile``
worker tasks — the heavy Docling/OCR + MCP work runs in the WORKER (its home);
the backend only enqueues. This is the fix for the OOM outage: the backend used to
run Docling here (concurrent, atop the full app) and exceeded its 6 Gi limit.

A grace window (``paperless_reconciler_refile_grace_seconds``) keeps the scan from
racing an initial filing that is still in flight (the hook runs fire-and-forget
after the doc is marked completed), so a fresh doc isn't refiled while its first
attempt is mid-upload.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import or_, select

from models.database import (
    DOC_STATUS_COMPLETED,
    PAPERLESS_STATE_PENDING,
    Atom,
    Document,
)
from services.chat_paperless_split import settle_open_intents, split_filing_active
from services.database import AsyncSessionLocal
from services.redis_client import get_redis
from services.task_queue import DocumentTaskQueue
from utils.config import settings

# Redis lease key: one refile in flight per doc (SET NX EX). See
# settings.paperless_reconciler_refile_lease_seconds for the rationale.
_REFILE_LEASE_KEY = "paperless:refile:lease:{doc_id}"


async def reenqueue_pending_paperless(mcp_manager: Any = None) -> None:
    """Enqueue a ``paperless_refile`` worker task for each straggler pending doc.

    ``mcp_manager`` is accepted but unused — kept so the scheduler wiring stays
    uniform with the other reconcilers; the backend no longer touches the MCP for
    Paperless (the worker does)."""
    grace = timedelta(seconds=settings.paperless_reconciler_refile_grace_seconds)
    cutoff = datetime.now(UTC).replace(tzinfo=None) - grace
    batch = settings.paperless_reconciler_batch

    # Filings requested from the chat that have not reached their KB document
    # (or its split children) yet -- the convergence point of the split-aware
    # route. Stamped `pending` here, picked up by the scan below.
    if split_filing_active():
        async with AsyncSessionLocal() as db:
            await settle_open_intents(db, limit=batch)

    async with AsyncSessionLocal() as db:
        # Left-join atoms for the owner (Document has no direct owner column —
        # ownership lives on the atom via Document.atom_id) so the refile files
        # metadata with the SAME owner-scoped correspondent/learned-examples the
        # inline hook path uses. A NULL processed_at (anomalous/legacy completed
        # row) is by definition older than any grace → include it (SQL
        # ``NULL < cutoff`` is unknown and would otherwise drop it forever).
        rows = (
            await db.execute(
                select(Document.id, Atom.owner_user_id)
                .join(Atom, Document.atom_id == Atom.atom_id, isouter=True)
                .where(
                    Document.paperless_state == PAPERLESS_STATE_PENDING,
                    Document.status == DOC_STATUS_COMPLETED,
                    or_(
                        Document.processed_at < cutoff,
                        Document.processed_at.is_(None),
                    ),
                )
                .order_by(Document.id)
                .limit(batch)
            )
        ).all()
    if not rows:
        return

    redis = get_redis()
    ttl = settings.paperless_reconciler_refile_lease_seconds
    queue = DocumentTaskQueue(redis_client=redis)
    enqueued = 0
    for doc_id, owner_user_id in rows:
        # Lease the doc before enqueuing so a still-in-flight refile isn't
        # re-queued every tick (processed_at is fixed, so the row re-selects).
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
        enqueued += 1
    if enqueued:
        logger.info(
            f"paperless-reconciler: re-enqueued {enqueued} pending doc(s) for "
            f"worker refile (grace={grace.total_seconds():.0f}s, lease={ttl}s)"
        )
