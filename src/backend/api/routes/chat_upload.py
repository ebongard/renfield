"""
Chat Upload API Routes

Upload documents directly in chat for quick text extraction.
Optionally index into RAG knowledge base or forward to Paperless-NGX.
"""
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    UPLOAD_STATUS_COMPLETED,
    UPLOAD_STATUS_FAILED,
    ChatUpload,
    KnowledgeBase,
)
from services.auth_service import get_optional_user
from services.database import AsyncSessionLocal, get_db
from services.document_processor import DocumentProcessor
from utils.config import settings

from .chat_upload_schemas import (
    ChatUploadResponse,
    CleanupResponse,
    EmailForwardRequest,
    EmailForwardResponse,
    IndexRequest,
    IndexResponse,
    PaperlessResponse,
)

router = APIRouter()

_document_processor: DocumentProcessor | None = None


def _get_processor() -> DocumentProcessor:
    global _document_processor
    if _document_processor is None:
        _document_processor = DocumentProcessor()
    return _document_processor


@router.post("/upload", response_model=ChatUploadResponse)
async def upload_chat_document(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    knowledge_base_id: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    user=Depends(get_optional_user),
):
    """
    Upload a document in chat for quick text extraction.

    Returns extracted text preview and metadata.
    """
    # Validate file extension
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower().lstrip('.')
    if ext not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format: .{ext}",
        )

    # Read file content
    content = await file.read()

    # Validate file size
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File too large (max. {settings.max_file_size_mb}MB)",
        )

    # Compute SHA256 hash
    file_hash = hashlib.sha256(content).hexdigest()

    # Save file to upload dir
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    safe_name = os.path.basename(filename.replace("\x00", ""))
    unique_filename = f"{uuid.uuid4().hex}_{safe_name}"
    file_path = upload_dir / unique_filename

    try:
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(content)
    except Exception as e:
        logger.error(f"Chat upload: Datei speichern fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail="File save failed")

    # Extract text
    extracted_text = None
    status = UPLOAD_STATUS_COMPLETED
    error_message = None

    try:
        processor = _get_processor()
        extracted_text = await processor.extract_text_only(str(file_path))
    except Exception as e:
        logger.error(f"Chat upload: Text-Extraktion fehlgeschlagen: {e}")
        status = UPLOAD_STATUS_FAILED
        error_message = str(e)

    # Create DB entry
    upload = ChatUpload(
        session_id=session_id,
        filename=safe_name,
        file_type=ext,
        file_size=len(content),
        file_hash=file_hash,
        extracted_text=extracted_text,
        status=status,
        error_message=error_message,
        knowledge_base_id=knowledge_base_id,
        file_path=str(file_path),
    )
    db.add(upload)
    await db.commit()
    await db.refresh(upload)

    # Fire KG extraction for extracted text (fire-and-forget)
    if extracted_text and settings.knowledge_graph_enabled:
        from utils.hooks import run_hooks
        background_tasks.add_task(
            run_hooks,
            "post_document_ingest",
            chunks=[extracted_text],
            document_id=None,
            user_id=user.id if user else None,
        )

    # Auto-index to KB if enabled
    if settings.chat_upload_auto_index and status == UPLOAD_STATUS_COMPLETED:
        background_tasks.add_task(
            _auto_index_to_kb, upload.id, str(file_path), safe_name, file_hash,
            session_id=session_id,
        )

    return ChatUploadResponse(
        id=upload.id,
        filename=upload.filename,
        file_type=upload.file_type,
        file_size=upload.file_size,
        status=upload.status,
        text_preview=extracted_text[:500] if extracted_text else None,
        error_message=upload.error_message,
        created_at=upload.created_at.isoformat() if upload.created_at else "",
    )


# ============================================================================
# Manual KB Index Endpoint
# ============================================================================


@router.post("/upload/{upload_id}/index", response_model=IndexResponse)
async def index_chat_upload(
    upload_id: int,
    request: IndexRequest,
    db: AsyncSession = Depends(get_db),
):
    """Index a chat upload into a RAG knowledge base."""
    # Fetch upload
    result = await db.execute(
        select(ChatUpload).where(ChatUpload.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")

    # Check file exists on disk
    if not upload.file_path or not Path(upload.file_path).is_file():
        raise HTTPException(status_code=400, detail="File no longer available on disk")

    # Already indexed?
    if upload.document_id is not None:
        raise HTTPException(status_code=409, detail="Already indexed")

    # Verify KB exists
    kb_result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == request.knowledge_base_id)
    )
    kb = kb_result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    # Ingest via RAGService
    from services.rag_service import RAGService
    rag = RAGService(db)
    doc = await rag.ingest_document(
        file_path=upload.file_path,
        knowledge_base_id=request.knowledge_base_id,
        filename=upload.filename,
        file_hash=upload.file_hash,
    )

    # Update ChatUpload
    upload.document_id = doc.id
    upload.knowledge_base_id = request.knowledge_base_id
    await db.commit()

    return IndexResponse(
        success=True,
        document_id=doc.id,
        knowledge_base_id=request.knowledge_base_id,
        chunk_count=doc.chunk_count,
        message="Indexed successfully",
    )


# ============================================================================
# Paperless Forward Endpoint
# ============================================================================


@router.post("/upload/{upload_id}/paperless", response_model=PaperlessResponse)
async def forward_to_paperless(
    upload_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Forward a chat upload to Paperless-NGX via MCP."""
    # Fetch upload
    result = await db.execute(
        select(ChatUpload).where(ChatUpload.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")

    # Check file exists on disk
    if not upload.file_path or not Path(upload.file_path).is_file():
        raise HTTPException(status_code=400, detail="File no longer available on disk")

    # Get MCP manager
    manager = getattr(request.app.state, "mcp_manager", None)
    if not manager:
        raise HTTPException(status_code=503, detail="MCP not available")

    # Read and base64-encode file
    async with aiofiles.open(upload.file_path, 'rb') as f:
        file_bytes = await f.read()
    file_content_base64 = base64.b64encode(file_bytes).decode("ascii")

    # Execute MCP tool
    try:
        mcp_result = await manager.execute_tool(
            "mcp.paperless.upload_document",
            {
                "title": upload.filename,
                "filename": upload.filename,
                "file_content_base64": file_content_base64,
            },
        )
    except Exception as e:
        logger.error(f"Paperless forward failed: {e}")
        raise HTTPException(status_code=502, detail=f"Paperless forwarding failed: {e}")

    # Parse MCP response for task_id
    task_id = None
    if mcp_result and mcp_result.get("message"):
        try:
            inner = json.loads(mcp_result["message"])
            if inner.get("success") and inner.get("data"):
                task_id = inner["data"].get("task_id")
        except (json.JSONDecodeError, TypeError):
            pass

    if not mcp_result or not mcp_result.get("success"):
        raise HTTPException(status_code=502, detail="Paperless forwarding failed")

    return PaperlessResponse(
        success=True,
        paperless_task_id=str(task_id) if task_id else None,
        message="Sent to Paperless",
    )


# ============================================================================
# Email Forward Endpoint
# ============================================================================


@router.post("/upload/{upload_id}/email", response_model=EmailForwardResponse)
async def forward_via_email(
    upload_id: int,
    email_request: EmailForwardRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Forward a chat upload via Email MCP."""
    # Fetch upload
    result = await db.execute(
        select(ChatUpload).where(ChatUpload.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(status_code=404, detail="Upload not found")

    # Check file exists on disk
    if not upload.file_path or not Path(upload.file_path).is_file():
        raise HTTPException(status_code=400, detail="File no longer available on disk")

    # Get MCP manager
    manager = getattr(request.app.state, "mcp_manager", None)
    if not manager:
        raise HTTPException(status_code=503, detail="MCP not available")

    # Read and base64-encode file
    async with aiofiles.open(upload.file_path, 'rb') as f:
        file_bytes = await f.read()
    file_content_base64 = base64.b64encode(file_bytes).decode("ascii")

    subject = email_request.subject or f"Document: {upload.filename}"
    body = email_request.body or f"Attached: {upload.filename}"

    # Determine MIME type
    mime_type = "application/octet-stream"
    ext = upload.file_type
    if ext:
        mime_map = {
            "pdf": "application/pdf",
            "txt": "text/plain",
            "md": "text/markdown",
            "html": "text/html",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "doc": "application/msword",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
        }
        mime_type = mime_map.get(ext, mime_type)

    # Execute MCP tool
    try:
        mcp_result = await manager.execute_tool(
            "mcp.email.send_email",
            {
                "account": settings.chat_upload_email_account,
                "to": email_request.to,
                "subject": subject,
                "body": body,
                "attachments": [{
                    "filename": upload.filename,
                    "mime_type": mime_type,
                    "content_base64": file_content_base64,
                }],
            },
        )
    except Exception as e:
        logger.error(f"Email forward failed: {e}")
        raise HTTPException(status_code=502, detail=f"Email forwarding failed: {e}")

    if not mcp_result or not mcp_result.get("success"):
        logger.error(f"Email forward MCP result: {mcp_result}")
        detail = "Email forwarding failed"
        if mcp_result and mcp_result.get("message"):
            detail = f"Email forwarding failed: {mcp_result['message']}"
        raise HTTPException(status_code=502, detail=detail)

    return EmailForwardResponse(
        success=True,
        message=f"Sent to {email_request.to}",
    )


# ============================================================================
# Cleanup Endpoint
# ============================================================================


async def _cleanup_uploads(db: AsyncSession, days: int) -> tuple[int, int]:
    """Delete old non-indexed uploads. Returns (deleted_count, deleted_files)."""
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(days=days)

    result = await db.execute(
        select(ChatUpload).where(
            ChatUpload.created_at < cutoff,
            ChatUpload.document_id.is_(None),
        )
    )
    uploads = result.scalars().all()

    deleted_files = 0
    for upload in uploads:
        if upload.file_path:
            try:
                p = Path(upload.file_path)
                if p.is_file():
                    p.unlink()
                    deleted_files += 1
            except Exception as e:
                logger.warning(f"Failed to delete file {upload.file_path}: {e}")
        await db.delete(upload)

    deleted_count = len(uploads)
    if deleted_count > 0:
        await db.commit()

    return deleted_count, deleted_files


@router.delete("/upload/cleanup", response_model=CleanupResponse)
async def cleanup_old_uploads(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
):
    """Delete old non-indexed chat uploads."""
    deleted_count, deleted_files = await _cleanup_uploads(db, days)
    return CleanupResponse(
        success=True,
        deleted_count=deleted_count,
        deleted_files=deleted_files,
        message=f"Deleted {deleted_count} uploads ({deleted_files} files)",
    )


# ============================================================================
# Background Task: Auto-Index to KB
# ============================================================================


async def _get_or_create_default_kb(db: AsyncSession) -> KnowledgeBase:
    """Get or create the default KB for auto-indexed chat uploads."""
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.name == settings.chat_upload_default_kb_name)
    )
    kb = result.scalar_one_or_none()
    if kb:
        return kb

    kb = KnowledgeBase(
        name=settings.chat_upload_default_kb_name,
        description="Automatically indexed documents from chat uploads",
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _auto_index_to_kb(
    upload_id: int,
    file_path: str,
    filename: str,
    file_hash: str | None,
    session_id: str | None = None,
) -> None:
    """Background task: auto-index a chat upload into the default KB."""
    from api.websocket.shared import notify_session

    # Notify: processing started
    if session_id:
        await notify_session(session_id, {
            "type": "document_processing",
            "upload_id": upload_id,
            "filename": filename,
        })

    try:
        async with AsyncSessionLocal() as db:
            kb = await _get_or_create_default_kb(db)

            from services.rag_service import RAGService
            rag = RAGService(db)
            doc = await rag.ingest_document(
                file_path=file_path,
                knowledge_base_id=kb.id,
                filename=filename,
                file_hash=file_hash,
            )

            result = await db.execute(
                select(ChatUpload).where(ChatUpload.id == upload_id)
            )
            upload = result.scalar_one_or_none()
            if upload:
                upload.document_id = doc.id
                upload.knowledge_base_id = kb.id
                await db.commit()

            logger.info(f"Auto-indexed chat upload {upload_id} → KB '{kb.name}' (doc {doc.id})")

            # Notify: ready
            if session_id:
                await notify_session(session_id, {
                    "type": "document_ready",
                    "upload_id": upload_id,
                    "filename": filename,
                    "document_id": doc.id,
                    "knowledge_base_id": kb.id,
                    "chunk_count": doc.chunk_count,
                })
    except Exception as e:
        logger.error(f"Auto-index failed for upload {upload_id}: {e}")

        # Notify: error
        if session_id:
            await notify_session(session_id, {
                "type": "document_error",
                "upload_id": upload_id,
                "filename": filename,
                "error": str(e),
            })
