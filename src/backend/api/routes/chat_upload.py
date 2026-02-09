"""
Chat Upload API Routes

Upload documents directly in chat for quick text extraction.
No RAG indexing — just extract text and store metadata.
"""
import hashlib
import os
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import UPLOAD_STATUS_COMPLETED, UPLOAD_STATUS_FAILED, ChatUpload
from services.database import get_db
from services.document_processor import DocumentProcessor
from utils.config import settings

from .chat_upload_schemas import ChatUploadResponse

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
):
    """
    Upload a document in chat for quick text extraction.

    Returns extracted text preview and metadata.
    No RAG indexing or chunking — just raw text extraction.
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
    )
    db.add(upload)
    await db.commit()
    await db.refresh(upload)

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
