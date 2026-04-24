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
    Conversation,
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


async def _get_owned_upload(
    db: AsyncSession,
    upload_id: int,
    user,
) -> ChatUpload | None:
    """Fetch a ChatUpload, scoped to the authenticated user's conversations.

    Ownership model: ChatUpload rows carry ``session_id`` only; the link back
    to a user runs through ``Conversation.user_id``. We join chat_uploads →
    conversations and filter by the authenticated user's id.

    When ``user`` is None (AUTH_ENABLED=false or anonymous dev setup), the
    scoping filter is skipped — matches the single-user fallback convention
    established in #433. In auth-enabled multi-user mode, a cross-user lookup
    returns None (soft 404) rather than a 403, so the response doesn't leak
    the existence of other users' uploads.
    """
    query = select(ChatUpload).where(ChatUpload.id == upload_id)
    if user is not None:
        query = query.join(
            Conversation, Conversation.session_id == ChatUpload.session_id,
        ).where(Conversation.user_id == user.id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


@router.post("/upload/{upload_id}/index", response_model=IndexResponse)
async def index_chat_upload(
    upload_id: int,
    request: IndexRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_optional_user),
):
    """Index a chat upload into a RAG knowledge base."""
    # Fetch upload (scoped to the authenticated user's conversations)
    upload = await _get_owned_upload(db, upload_id, user)
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
    user=Depends(get_optional_user),
):
    """Forward a chat upload to Paperless-NGX via MCP."""
    # Fetch upload (scoped to the authenticated user's conversations)
    upload = await _get_owned_upload(db, upload_id, user)
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
    user=Depends(get_optional_user),
):
    """Forward a chat upload via Email MCP."""
    # Fetch upload (scoped to the authenticated user's conversations)
    upload = await _get_owned_upload(db, upload_id, user)
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
    """Background task: auto-index a chat upload into the default KB via
    the document-worker pod (#388).

    This used to call ``rag.ingest_document`` inline in the backend pod,
    which meant Docling+EasyOCR ran in the API serving path for every
    chat attachment — the reason backend memory was stuck at 8 GiB after
    the main upload path moved to the worker. Now we:

      1. Create the Document row (``status=pending``) in the backend so
         the upload row can be linked immediately.
      2. Enqueue on the Redis Stream the worker already consumes.
      3. Poll the row's status with a 2 s tick, 30 min hard cap.
      4. Fire the same ``notify_session`` messages at the same moments
         the old inline call did — chat UI contract unchanged.

    Lives in the background-task executor, so the HTTP response for the
    original upload request has already returned. Polling here doesn't
    block anyone.
    """
    import asyncio

    from api.websocket.shared import notify_session
    from services.redis_client import get_redis
    from services.task_queue import DocumentTaskQueue

    _POLL_INTERVAL_S = 2
    _POLL_TIMEOUT_S = 30 * 60  # 30 minutes — matches frontend cap

    if session_id:
        await notify_session(session_id, {
            "type": "document_processing",
            "upload_id": upload_id,
            "filename": filename,
        })

    try:
        # Heartbeat-gate the enqueue. If the worker is down we surface
        # an error right away rather than sit in the poll loop for
        # 30 minutes and finally time out. Same check the main upload
        # endpoint uses.
        from api.routes.knowledge import _worker_is_alive
        if not await _worker_is_alive():
            raise RuntimeError("Document worker is unavailable")

        # Step 1 — Document row + enqueue, within a short-lived session.
        async with AsyncSessionLocal() as db:
            kb = await _get_or_create_default_kb(db)
            kb_id = kb.id

            # Re-upload of the same file into the same KB: the unique
            # constraint `uq_documents_file_hash_kb` fires on a second
            # INSERT and the auto-index path raises IntegrityError,
            # surfacing to the user as "Auto-index failed". Pre-check
            # for an existing doc with this hash and reuse it instead
            # — link the chat upload to the existing doc and let the
            # poll loop pick up its current state (likely already
            # `completed`, in which case the user gets an immediate
            # "document_ready" notification).
            from models.database import Document as _Doc
            existing = (await db.execute(
                select(_Doc)
                .where(_Doc.file_hash == file_hash)
                .where(_Doc.knowledge_base_id == kb_id)
            )).scalar_one_or_none()

            if existing is not None:
                doc_id = existing.id
                upload_row = (await db.execute(
                    select(ChatUpload).where(ChatUpload.id == upload_id)
                )).scalar_one_or_none()
                if upload_row:
                    upload_row.document_id = doc_id
                    upload_row.knowledge_base_id = kb_id
                    await db.commit()
                logger.info(
                    f"Chat upload {upload_id} matches existing doc {doc_id} "
                    f"in KB {kb_id} (hash={file_hash[:12]}…) — reusing"
                )
                # Skip enqueue; fall through to the poll loop which
                # will detect status=completed on the next iteration
                # and notify the session.
                doc = existing
            else:
                from services.rag_service import RAGService
                rag = RAGService(db)
                doc = await rag.create_document_record(
                    file_path=file_path,
                    knowledge_base_id=kb_id,
                    filename=filename,
                    file_hash=file_hash,
                )
                doc_id = doc.id

                # Link the chat upload to the pending doc up-front so
                # the UI can show "processing" without waiting for the
                # worker.
                upload_row = (await db.execute(
                    select(ChatUpload).where(ChatUpload.id == upload_id)
                )).scalar_one_or_none()
                if upload_row:
                    upload_row.document_id = doc_id
                    upload_row.knowledge_base_id = kb_id
                    await db.commit()

        # Only enqueue when we created a NEW document row. Re-uploads
        # that hit the existing-doc branch above already have a worker
        # task in some terminal state (or in flight) — the poll loop
        # picks up `completed`/`failed` directly. Re-enqueuing a
        # completed doc would burn worker cycles for no gain.
        if existing is None:
            queue = DocumentTaskQueue(redis_client=get_redis())
            await queue.enqueue({
                "document_id": doc_id,
                "force_ocr": False,
                "user_id": None,
            })
            logger.info(f"Chat upload {upload_id} enqueued as doc {doc_id} → KB {kb_id}")

        # Step 2 — poll for terminal state. Check first, sleep after,
        # so a worker that finishes in <2s notifies the UI immediately
        # instead of being punished by an unnecessary full interval.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _POLL_TIMEOUT_S
        while loop.time() < deadline:
            async with AsyncSessionLocal() as db:
                doc_row = (await db.execute(
                    select(ChatUpload.document_id, ChatUpload.knowledge_base_id)
                    .where(ChatUpload.id == upload_id)
                )).first()
                # Defensive: if the upload was deleted mid-flight, bail
                # silently — the worker will still process the doc row
                # and the user will see it in the knowledge list.
                if not doc_row:
                    return
                from models.database import Document as _Doc
                d = (await db.execute(
                    select(_Doc).where(_Doc.id == doc_id)
                )).scalar_one_or_none()
            if d is None:
                return
            if d.status == "completed":
                if session_id:
                    await notify_session(session_id, {
                        "type": "document_ready",
                        "upload_id": upload_id,
                        "filename": filename,
                        "document_id": doc_id,
                        "knowledge_base_id": kb_id,
                        "chunk_count": d.chunk_count or 0,
                    })
                logger.info(f"Auto-indexed chat upload {upload_id} → doc {doc_id} ({d.chunk_count} chunks)")
                return
            if d.status == "failed":
                raise RuntimeError(d.error_message or "Document processing failed in worker")
            # Non-terminal — wait and loop.
            await asyncio.sleep(_POLL_INTERVAL_S)

        # Poll cap without a terminal state — worker is stuck or slow.
        raise TimeoutError(f"Worker did not finish within {_POLL_TIMEOUT_S} s")

    except Exception as e:
        logger.error(f"Auto-index failed for upload {upload_id}: {e}")
        if session_id:
            await notify_session(session_id, {
                "type": "document_error",
                "upload_id": upload_id,
                "filename": filename,
                "error": str(e),
            })
