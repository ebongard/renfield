"""
Admin API routes for Paperless Document Audit.

Dynamically mounted only when PAPERLESS_AUDIT_ENABLED=true and
Paperless MCP server is configured. See lifecycle.py.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/paperless-audit", tags=["paperless-audit"])


# --- Request/Response Models ---

class AuditStartRequest(BaseModel):
    mode: str = "new_only"  # new_only | full
    fix_mode: str | None = None  # review | auto_threshold | auto_all
    confidence_threshold: float | None = None
    document_ids: list[int] | None = None


class ApplyRequest(BaseModel):
    result_ids: list[int]


class SkipRequest(BaseModel):
    result_ids: list[int]


class ReOcrRequest(BaseModel):
    result_ids: list[int]


# --- Helper ---

def _get_service(request: Request):
    """Get the audit service from app state, or raise 503."""
    service = getattr(request.app.state, "paperless_audit", None)
    if not service:
        raise HTTPException(
            status_code=503,
            detail="Paperless Audit service not available",
        )
    return service


# --- Endpoints ---

@router.post("/start")
async def start_audit(body: AuditStartRequest, request: Request):
    """Start a new audit run."""
    service = _get_service(request)

    if service.get_status()["running"]:
        raise HTTPException(status_code=409, detail="Audit already running")

    # Run in background — returns immediately so Nginx doesn't timeout
    run_id = await service.run_audit_background(
        mode=body.mode,
        fix_mode=body.fix_mode,
        confidence_threshold=body.confidence_threshold,
        document_ids=body.document_ids,
    )

    return {"run_id": run_id, "status": "started"}


@router.get("/status")
async def get_status(request: Request):
    """Get current audit status."""
    service = _get_service(request)
    return service.get_status()


@router.post("/stop")
async def stop_audit(request: Request):
    """Cancel running audit."""
    service = _get_service(request)
    await service.stop()
    return {"message": "Audit stopped"}


@router.get("/results")
async def get_results(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    status: str | None = None,
    changes_needed: bool | None = None,
    ocr_quality_max: int | None = None,
    missing_field: str | None = None,
    detected_language: str | None = None,
    completeness_max: int | None = None,
    duplicate_group_id: str | None = None,
    sort_by: str | None = None,
    sort_order: str = "desc",
    search: str | None = None,
):
    """Get paginated audit results with optional filters, sorting, and search."""
    service = _get_service(request)
    return await service.get_results(
        page=page,
        per_page=per_page,
        status=status,
        changes_needed=changes_needed,
        ocr_quality_max=ocr_quality_max,
        missing_field=missing_field,
        detected_language=detected_language,
        completeness_max=completeness_max,
        duplicate_group_id=duplicate_group_id,
        sort_by=sort_by,
        sort_order=sort_order,
        search=search,
    )


@router.get("/results/{result_id}")
async def get_result(result_id: int, request: Request):
    """Get a single audit result."""
    service = _get_service(request)
    result = await service.get_result_by_id(result_id)
    if not result:
        raise HTTPException(status_code=404, detail="Result not found")
    return result


@router.post("/apply")
async def apply_results(body: ApplyRequest, request: Request):
    """Apply fixes for selected results."""
    service = _get_service(request)
    return await service.apply_results(body.result_ids)


@router.post("/skip")
async def skip_results(body: SkipRequest, request: Request):
    """Skip selected results."""
    service = _get_service(request)
    return await service.skip_results(body.result_ids)


@router.get("/stats")
async def get_stats(request: Request):
    """Get aggregated audit statistics."""
    service = _get_service(request)
    return await service.get_stats()


@router.post("/re-ocr")
async def trigger_reocr(body: ReOcrRequest, request: Request):
    """Trigger re-OCR for selected results."""
    service = _get_service(request)
    return await service.reprocess_documents(body.result_ids)


@router.post("/detect-duplicates")
async def detect_duplicates(request: Request):
    """Run duplicate detection post-audit pass."""
    service = _get_service(request)

    if service.get_status()["running"]:
        raise HTTPException(status_code=409, detail="Audit is running — wait for completion")

    return await service.run_duplicate_detection()


@router.get("/duplicate-groups")
async def get_duplicate_groups(request: Request):
    """Get all duplicate groups with their documents."""
    service = _get_service(request)
    return await service.get_duplicate_groups()


@router.get("/correspondent-normalization")
async def correspondent_normalization(
    request: Request,
    threshold: float = Query(0.82, ge=0.5, le=1.0),
):
    """Scan for similar correspondent names that may be duplicates."""
    service = _get_service(request)
    return await service.run_correspondent_normalization(threshold=threshold)
