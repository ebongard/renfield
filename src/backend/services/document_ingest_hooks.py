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
from utils.hooks import is_hook_registered, register_hook

_EVENT = "post_document_ingest"


def register_document_ingest_hooks() -> None:
    """Register every ``post_document_ingest`` consumer, each gated on its flag.

    Idempotent: a handler already present is not re-appended, so calling this
    from both the API lifecycle and the worker startup in the same process is
    safe (``register_hook`` itself does not deduplicate).

    Fail-open per consumer: if one consumer's import raises (bad prompt YAML, a
    new top-level dependency, a model-init side effect), it is logged and
    skipped — the other consumers still register, and crucially the worker's
    ingestion loop still starts. A registration crash must not turn "extraction
    off" into a full ingestion outage.
    """
    registered: list[str] = []

    def _maybe_register(label: str, fn) -> None:
        if not is_hook_registered(_EVENT, fn):
            register_hook(_EVENT, fn)
            registered.append(label)

    if settings.knowledge_graph_enabled:
        try:
            from services.knowledge_graph_service import kg_post_document_ingest_hook

            _maybe_register("knowledge_graph", kg_post_document_ingest_hook)
        except Exception:  # noqa: BLE001 — fail-open, never block ingestion
            logger.opt(exception=True).warning(
                "Failed to register KG post_document_ingest hook — KG extraction "
                "disabled for this process; ingestion continues."
            )

    if settings.schicht_a_extraction_enabled:
        try:
            from services.schicht_a_extractor import (
                schicht_a_post_document_ingest_hook,
            )

            _maybe_register("schicht_a", schicht_a_post_document_ingest_hook)
        except Exception:  # noqa: BLE001 — fail-open, never block ingestion
            logger.opt(exception=True).warning(
                "Failed to register Schicht A post_document_ingest hook — field "
                "extraction disabled for this process; ingestion continues."
            )

    # Paperless filing runs in the worker (Docling's home) and reuses the same
    # field_text this hook family receives — so the best-quality OCR lands in both
    # the KB and Paperless. Gated on either ingest→Paperless flag.
    if settings.folder_ingest_to_paperless or settings.email_ingest_to_paperless:
        try:
            from services.paperless_filing_hook import (
                paperless_filing_post_ingest_hook,
            )

            _maybe_register("paperless_filing", paperless_filing_post_ingest_hook)
        except Exception:  # noqa: BLE001 — fail-open, never block ingestion
            logger.opt(exception=True).warning(
                "Failed to register Paperless filing post_document_ingest hook — "
                "Paperless filing disabled for this process; ingestion continues."
            )

    # xidra-only: file a REVIEW proposal for a watch-folder PDF so the owner can
    # confirm the (irreversible) Simba tax-portal upload. Classifies from the same
    # field_text; never auto-uploads.
    if settings.folder_ingest_simba_enabled:
        try:
            from services.simba_ingest_review import simba_ingest_post_hook

            _maybe_register("simba_ingest", simba_ingest_post_hook)
        except Exception:  # noqa: BLE001 — fail-open, never block ingestion
            logger.opt(exception=True).warning(
                "Failed to register Simba-ingest post_document_ingest hook — "
                "Simba review disabled for this process; ingestion continues."
            )

    if registered:
        logger.info(
            f"✅ post_document_ingest hooks registered: {', '.join(registered)}"
        )
