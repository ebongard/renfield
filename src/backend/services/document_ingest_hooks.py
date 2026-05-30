"""Register ``post_document_ingest`` hook consumers.

Shared by the FastAPI lifecycle (``api/lifecycle.py``) **and** the
document-worker (``workers/document_processor_worker.py``).

The worker is the *primary* ingestion path
(``/api/knowledge/upload`` → Redis stream → worker) and fires
``run_hooks("post_document_ingest", ...)`` from
``RAGService.process_existing_document``. The worker process never runs the
FastAPI lifecycle, so the global hook registry is empty there unless it
populates it itself. Without this call, neither KG extraction nor Schicht A
field extraction runs for knowledge-base uploads — the hooks silently no-op.

Kept import-light (no ``main``, no ``api.lifecycle``, no MCP modules) so the
worker module-isolation budget (``test_document_worker_isolation``) holds. The
heavy service modules are imported lazily inside the gated branches and only
when the corresponding feature flag is on.
"""
from __future__ import annotations

from loguru import logger

from utils.config import settings
from utils.hooks import _hooks, register_hook

_EVENT = "post_document_ingest"


def register_document_ingest_hooks() -> None:
    """Register every ``post_document_ingest`` consumer, each gated on its flag.

    Idempotent: a handler already present is not re-appended, so calling this
    from both the API lifecycle and the worker startup in the same process is
    safe (``register_hook`` itself does not deduplicate).
    """
    registered: list[str] = []

    if settings.knowledge_graph_enabled:
        from services.knowledge_graph_service import kg_post_document_ingest_hook

        if kg_post_document_ingest_hook not in _hooks.get(_EVENT, []):
            register_hook(_EVENT, kg_post_document_ingest_hook)
            registered.append("knowledge_graph")

    if settings.schicht_a_extraction_enabled:
        from services.schicht_a_extractor import schicht_a_post_document_ingest_hook

        if schicht_a_post_document_ingest_hook not in _hooks.get(_EVENT, []):
            register_hook(_EVENT, schicht_a_post_document_ingest_hook)
            registered.append("schicht_a")

    if registered:
        logger.info(
            f"✅ post_document_ingest hooks registered: {', '.join(registered)}"
        )
