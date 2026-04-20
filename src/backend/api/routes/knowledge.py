"""
Knowledge API Routes

Endpoints für Dokument-Upload, Management und RAG-Suche.
With RPBAC permission checks for secure access control.
Pydantic schemas are defined in knowledge_schemas.py.
"""
import hashlib
import os
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Response, UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document, KnowledgeBase, User
from models.permissions import Permission, has_permission
from services.auth_service import get_optional_user
from services.database import get_db
from services.progress import DocumentProgress
from services.rag_service import RAGService
from services.redis_client import get_redis
from services.task_queue import DocumentTaskQueue
from utils.config import settings

# Worker liveness key. Written every 30 s by the document-worker pod with
# a 90 s TTL. If missing when the flag is on, we 503 the upload rather than
# enqueue into a stream nobody's consuming.
_WORKER_HEARTBEAT_KEY = "renfield:worker:document:heartbeat"


async def _worker_is_alive() -> bool:
    """Check the Redis heartbeat key. Returns True if a worker has
    refreshed it within the TTL window."""
    redis = get_redis()
    try:
        value = await redis.get(_WORKER_HEARTBEAT_KEY)
    except Exception as e:
        # Redis outage masks the worker's real state; treat as dead so we
        # fail loudly rather than silently enqueue into a broken Redis.
        logger.warning(f"heartbeat check failed: {e}; treating worker as unavailable")
        return False
    return value is not None


async def _augment_with_progress(
    doc: Document,
    resp_kwargs: dict,
    *,
    include_queue_position: bool,
) -> None:
    """Populate the new #388 progress fields on a DocumentResponse payload.

    Reads stage + pages from DocumentProgress (Redis), and — only for rows
    in pending state — computes a 1-indexed queue position via the
    DocumentTaskQueue's pending count. Mutates ``resp_kwargs`` in place.

    Degrades silently on Redis errors: the row still renders, just without
    live progress. We don't want a stale Redis to 500 the docs list.
    """
    redis = get_redis()
    try:
        progress = DocumentProgress(redis, doc.id)
        live = await progress.read()
        if live.get("stage"):
            resp_kwargs["stage"] = live["stage"]
        if live.get("pages"):
            resp_kwargs["pages"] = live["pages"]
    except Exception as e:
        logger.warning(f"progress read failed for doc {doc.id}: {e}")

    if include_queue_position and doc.status == "pending":
        try:
            queue = DocumentTaskQueue(redis_client=redis)
            pending = await queue.pending_count()
            # Approximation: the user's doc is somewhere in the PEL/stream;
            # without tracking entry-ids we report total backlog + 1 as an
            # upper-bound "your doc is at most this far back". The UI shows
            # "Platz <n>" in the badge — directional not exact.
            if pending > 0:
                resp_kwargs["queue_position"] = pending
            else:
                resp_kwargs["queue_position"] = 1
        except Exception as e:
            logger.warning(f"queue position read failed for doc {doc.id}: {e}")

# Import all schemas from separate file
from .knowledge_schemas import (
    DocumentResponse,
    KBPermissionCreate,
    KBPermissionResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    MoveDocumentsRequest,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchResultChunk,
    SearchResultDocument,
    StatsResponse,
)

router = APIRouter()


# =============================================================================
# Helper Functions
# =============================================================================

def get_rag_service(db: AsyncSession = Depends(get_db)) -> RAGService:
    """Dependency für RAG Service"""
    return RAGService(db)


async def check_kb_access(
    kb: KnowledgeBase,
    user: User | None,
    required_action: str = "read",  # read, write, delete
    db: AsyncSession = None
) -> bool:
    """
    Check if a user has access to a knowledge base.

    Access rules:
    1. Auth disabled → full access
    2. kb.all permission → full access
    3. Owner → full access
    4. Public KB → read access for users with kb.shared
    5. Explicit KBPermission → per-permission access
    6. kb.own permission → access to own KBs only
    """
    # Auth disabled = full access
    if not settings.auth_enabled:
        return True

    # No user = no access (when auth is enabled)
    if not user:
        return False

    user_perms = user.get_permissions()

    # Admin with kb.all has full access
    if has_permission(user_perms, Permission.KB_ALL):
        return True

    # Owner has full access
    if kb.owner_id == user.id:
        return True

    # Public KB: users with kb.shared can read
    if kb.is_public and required_action == "read":
        if has_permission(user_perms, Permission.KB_SHARED):
            return True

    # Check explicit grants via the atom_explicit_grants → chunks aggregation.
    if db:
        from services.kb_shares_service import get_user_kb_permission_level
        level = await get_user_kb_permission_level(db, kb.id, user.id)
        if level:
            perm_levels = {"read": 1, "write": 2, "admin": 3}
            required_level = perm_levels.get(required_action, 1)
            user_level = perm_levels.get(level, 0)
            if user_level >= required_level:
                return True

    return False


async def get_user_kb_permission(
    kb: KnowledgeBase,
    user: User | None,
    db: AsyncSession
) -> str | None:
    """
    Get the user's permission level on a KB.

    Returns: "owner", "admin", "write", "read", or None
    """
    if not settings.auth_enabled or not user:
        return "admin"  # Full access when auth disabled

    user_perms = user.get_permissions()

    # Admin with kb.all = admin level
    if has_permission(user_perms, Permission.KB_ALL):
        return "admin"

    # Owner = owner level
    if kb.owner_id == user.id:
        return "owner"

    # Check explicit grants via the atom_explicit_grants → chunks aggregation.
    from services.kb_shares_service import get_user_kb_permission_level
    level = await get_user_kb_permission_level(db, kb.id, user.id)
    if level:
        return level

    # Public KB + kb.shared = read
    if kb.is_public and has_permission(user_perms, Permission.KB_SHARED):
        return "read"

    return None


# =============================================================================
# Document Upload
# =============================================================================

@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    response: Response,
    file: UploadFile = File(...),
    knowledge_base_id: int | None = Query(None, description="Knowledge Base ID"),
    force_ocr: bool = Query(False, description="Force full-page OCR (ignores embedded text). Useful for scanned PDFs with garbled text layer."),
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Lädt ein Dokument hoch und indexiert es für RAG.

    Unterstützte Formate: PDF, DOCX, TXT, MD, HTML, PPTX, XLSX

    Requires: rag.manage permission or write access to KB
    """
    # Permission check
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_perms = user.get_permissions()

        # Check if user can upload to KBs
        if not has_permission(user_perms, Permission.RAG_MANAGE):
            # If not general RAG_MANAGE, check specific KB permission
            if knowledge_base_id:
                result = await db.execute(
                    select(KnowledgeBase).where(KnowledgeBase.id == knowledge_base_id)
                )
                kb = result.scalar_one_or_none()
                if kb and not await check_kb_access(kb, user, "write", db):
                    raise HTTPException(
                        status_code=403,
                        detail="No write access to this knowledge base"
                    )
            else:
                raise HTTPException(
                    status_code=403,
                    detail="Permission required: rag.manage"
                )
    # Validierung: Dateiformat — 415 Unsupported Media Type is the semantic
    # match. Frontend reads the structured detail to render allowed-format
    # copy; legacy clients that only read status codes also get the right
    # signal.
    extension = Path(file.filename).suffix.lower().lstrip('.')
    allowed = settings.allowed_extensions_list

    if extension not in allowed:
        raise HTTPException(
            status_code=415,
            detail={
                "message": f"Dateiformat '{extension}' nicht unterstützt.",
                "allowed": sorted(allowed),
                "received": extension,
            },
        )

    # Validierung: Dateigröße — 413 Content Too Large is the correct code.
    # Include max_mb in the detail so the frontend can show the limit
    # without hard-coding it.
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)

    max_size = settings.max_file_size_mb * 1024 * 1024
    if size > max_size:
        raise HTTPException(
            status_code=413,
            detail={
                "message": f"Datei zu groß ({size // 1024 // 1024} MB).",
                "max_mb": settings.max_file_size_mb,
                "received_mb": size // 1024 // 1024,
            },
        )

    # Datei-Inhalt lesen und SHA256-Hash berechnen
    content = await file.read()
    file_hash = hashlib.sha256(content).hexdigest()
    logger.info(f"📄 Datei-Hash berechnet: {file_hash[:16]}... ({file.filename})")

    # Duplikat-Prüfung: Existiert bereits ein Dokument mit diesem Hash in der gleichen Knowledge Base?
    existing_doc = await rag.db.execute(
        select(Document).where(
            Document.file_hash == file_hash,
            Document.knowledge_base_id == knowledge_base_id
        )
    )
    existing = existing_doc.scalar_one_or_none()

    if existing:
        logger.warning(f"⚠️ Duplikat erkannt: '{file.filename}' ist identisch mit '{existing.filename}' (ID: {existing.id})")
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Dieses Dokument existiert bereits in der Knowledge Base",
                "existing_document": {
                    "id": existing.id,
                    "filename": existing.filename,
                    "uploaded_at": existing.created_at.isoformat() if existing.created_at else None
                }
            }
        )

    # Upload-Verzeichnis erstellen
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Eindeutigen Dateinamen generieren
    safe_name = os.path.basename((file.filename or "unknown").replace("\x00", ""))
    unique_filename = f"{uuid.uuid4().hex}_{safe_name}"
    file_path = upload_dir / unique_filename

    # Datei speichern
    try:
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(content)

        logger.info(f"Datei gespeichert: {file_path}")

    except Exception as e:
        logger.error(f"Fehler beim Speichern der Datei: {e}")
        raise HTTPException(status_code=500, detail=f"Fehler beim Speichern: {e!s}")

    # Worker path (#388): create the Document row synchronously (cheap
    # INSERT so the client has an id to poll), enqueue on the Redis
    # Stream, return 202. The worker pod runs Docling out-of-process so
    # a large PDF can't OOM the API serving path.
    if not await _worker_is_alive():
        # Nobody's consuming the stream — don't silently queue into the
        # void. Clean up the saved file so the next retry doesn't race
        # with an orphan on disk, and surface a 503 with a retry CTA.
        if file_path.exists():
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"failed to clean up orphan upload {file_path}: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Document worker unavailable",
                "retryable": True,
            },
        )

    try:
        doc = await rag.create_document_record(
            file_path=str(file_path),
            knowledge_base_id=knowledge_base_id,
            filename=file.filename,
            file_hash=file_hash,
        )
    except IntegrityError as ie:
        # Distinguish the concurrent-upload race (unique-constraint
        # violation on our uq_documents_file_hash_kb index) from other
        # IntegrityErrors (FK, NOT NULL) which are genuinely 500-worthy
        # — we don't want to paper over those with a misleading 409.
        orig_err = str(ie.orig) if ie.orig else str(ie)
        is_hash_race = "uq_documents_file_hash_kb" in orig_err
        if not is_hash_race:
            await rag.db.rollback()
            if file_path.exists():
                try:
                    os.remove(file_path)
                except OSError as cleanup_err:
                    logger.warning(f"failed to clean up orphan upload {file_path}: {cleanup_err}")
            logger.error(f"Unexpected IntegrityError on Document insert: {orig_err}")
            raise HTTPException(status_code=500, detail="Database integrity error")

        # Concurrent-upload race: someone else committed the same
        # (file_hash, knowledge_base_id) pair between our SELECT-based
        # dup check and this INSERT. Convert to the same 409 response
        # the pre-insert check produces so the frontend just opens the
        # duplicate dialog either way. Clean up the orphan file and
        # fetch the winning row for the payload.
        if file_path.exists():
            try:
                os.remove(file_path)
            except OSError as cleanup_err:
                logger.warning(f"failed to clean up orphan upload {file_path}: {cleanup_err}")
        await rag.db.rollback()
        winner_q = await rag.db.execute(
            select(Document).where(
                Document.file_hash == file_hash,
                Document.knowledge_base_id == knowledge_base_id,
            )
        )
        winner = winner_q.scalar_one_or_none()
        logger.warning(
            f"Concurrent duplicate upload detected for hash {file_hash[:16]}... "
            f"(kb={knowledge_base_id}); returning 409 with winner id={winner.id if winner else 'unknown'}"
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Dieses Dokument existiert bereits in der Knowledge Base",
                "existing_document": {
                    "id": winner.id if winner else None,
                    "filename": winner.filename if winner else file.filename,
                    "uploaded_at": (
                        winner.created_at.isoformat()
                        if winner and winner.created_at
                        else None
                    ),
                },
            },
        )
    except Exception as e:
        if file_path.exists():
            try:
                os.remove(file_path)
            except OSError as cleanup_err:
                # Don't let cleanup mask the real DB error the user
                # needs to see. Log it and move on.
                logger.warning(f"failed to clean up orphan upload {file_path}: {cleanup_err}")
        logger.error(f"Fehler beim Anlegen des Document-Records: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    queue = DocumentTaskQueue(redis_client=get_redis())
    await queue.enqueue({
        "document_id": doc.id,
        "force_ocr": force_ocr,
        "user_id": user.id if user else None,
    })

    response.status_code = 202
    return DocumentResponse(
        id=doc.id,
        filename=doc.filename,
        title=doc.title,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status=doc.status,  # "pending"
        error_message=None,
        chunk_count=0,
        page_count=None,
        knowledge_base_id=doc.knowledge_base_id,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        processed_at=None,
    )


# =============================================================================
# Document Management
# =============================================================================

@router.get("/documents", response_model=list[DocumentResponse])
async def list_documents(
    knowledge_base_id: int | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Listet alle indexierten Dokumente auf"""
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_perms = user.get_permissions()
        if not has_permission(user_perms, Permission.KB_ALL):
            if knowledge_base_id:
                result = await db.execute(
                    select(KnowledgeBase).where(KnowledgeBase.id == knowledge_base_id)
                )
                kb = result.scalar_one_or_none()
                if kb and not await check_kb_access(kb, user, "read", db):
                    raise HTTPException(status_code=403, detail="No access to this knowledge base")
            # If no KB filter, only users with kb.own+ can list
            elif not has_permission(user_perms, Permission.KB_OWN):
                raise HTTPException(status_code=403, detail="Permission required: kb.own or higher")
    documents = await rag.list_documents(
        knowledge_base_id=knowledge_base_id,
        status=status,
        limit=limit,
        offset=offset
    )

    responses: list[DocumentResponse] = []
    for doc in documents:
        kwargs = _doc_to_response_kwargs(doc)
        # Queue position only meaningful on single-doc + batch polling
        # endpoints; the list view is a bulk screen and we don't want to
        # spam Redis with XPENDING on every listed row.
        await _augment_with_progress(doc, kwargs, include_queue_position=False)
        responses.append(DocumentResponse(**kwargs))
    return responses


def _doc_to_response_kwargs(doc: Document) -> dict:
    """Common Document → DocumentResponse mapping. Lives in one place so
    new fields land consistently across single-doc, list, and batch
    endpoints."""
    return {
        "id": doc.id,
        "filename": doc.filename,
        "title": doc.title,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
        "status": doc.status,
        "error_message": doc.error_message,
        "chunk_count": doc.chunk_count or 0,
        "page_count": doc.page_count,
        "knowledge_base_id": doc.knowledge_base_id,
        "created_at": doc.created_at.isoformat() if doc.created_at else "",
        "processed_at": doc.processed_at.isoformat() if doc.processed_at else None,
    }


@router.get("/documents/batch", response_model=list[DocumentResponse])
async def get_documents_batch(
    ids: str = Query(..., description="Comma-separated list of document ids (e.g. ?ids=1,2,3)"),
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Batch lookup of document status + live progress.

    Added for the polling frontend (#388): one request per poll interval
    instead of one-per-in-flight-doc. Permission check mirrors the single
    GET endpoint; documents the caller can't read are silently dropped.
    """
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be a comma-separated list of integers")
    if not id_list:
        return []
    # Cap the batch to avoid pathological URL-length / N+1 queries. 50 is
    # well above realistic per-user in-flight upload counts.
    if len(id_list) > 50:
        raise HTTPException(status_code=400, detail="Batch too large (max 50 ids)")

    result = await db.execute(select(Document).where(Document.id.in_(id_list)))
    documents = result.scalars().all()

    responses: list[DocumentResponse] = []
    for doc in documents:
        # Per-doc permission check
        if settings.auth_enabled:
            if not user:
                # Auth enforced at the route level for list endpoints; this
                # is belt-and-braces. Skip the row rather than 401ing the
                # whole batch.
                continue
            if doc.knowledge_base_id:
                kb_res = await db.execute(
                    select(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
                )
                kb = kb_res.scalar_one_or_none()
                if kb and not await check_kb_access(kb, user, "read", db):
                    continue
        kwargs = _doc_to_response_kwargs(doc)
        await _augment_with_progress(doc, kwargs, include_queue_position=True)
        responses.append(DocumentResponse(**kwargs))
    return responses


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Holt Details zu einem Dokument"""
    document = await rag.get_document(document_id)

    if not document:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")

    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if document.knowledge_base_id:
            result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == document.knowledge_base_id)
            )
            kb = result.scalar_one_or_none()
            if kb and not await check_kb_access(kb, user, "read", db):
                raise HTTPException(status_code=403, detail="No access to this document")

    kwargs = _doc_to_response_kwargs(document)
    await _augment_with_progress(document, kwargs, include_queue_position=True)
    return DocumentResponse(**kwargs)


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Löscht ein Dokument und alle zugehörigen Chunks"""
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        doc = await rag.get_document(document_id)
        if doc and doc.knowledge_base_id:
            result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
            )
            kb = result.scalar_one_or_none()
            if kb and not await check_kb_access(kb, user, "delete", db):
                raise HTTPException(status_code=403, detail="No delete access to this document")
    success = await rag.delete_document(document_id)

    if not success:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")

    return {"message": "Dokument erfolgreich gelöscht", "id": document_id}


@router.post("/documents/{document_id}/reindex", response_model=DocumentResponse)
async def reindex_document(
    document_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Re-indexiert ein Dokument (löscht alte Chunks und erstellt neue)"""
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        doc = await rag.get_document(document_id)
        if doc and doc.knowledge_base_id:
            result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
            )
            kb = result.scalar_one_or_none()
            if kb and not await check_kb_access(kb, user, "write", db):
                raise HTTPException(status_code=403, detail="No write access to this document")
    try:
        document = await rag.reindex_document(document_id)

        return DocumentResponse(
            id=document.id,
            filename=document.filename,
            title=document.title,
            file_type=document.file_type,
            file_size=document.file_size,
            status=document.status,
            error_message=document.error_message,
            chunk_count=document.chunk_count or 0,
            page_count=document.page_count,
            knowledge_base_id=document.knowledge_base_id,
            created_at=document.created_at.isoformat() if document.created_at else "",
            processed_at=document.processed_at.isoformat() if document.processed_at else None
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/documents/move")
async def move_documents(
    request: MoveDocumentsRequest,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Verschiebt Dokumente in eine andere Knowledge Base"""
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_perms = user.get_permissions()
        if not has_permission(user_perms, Permission.KB_OWN):
            raise HTTPException(status_code=403, detail="Permission required: kb.own or higher")

        # Prüfe Write-Zugriff auf Ziel-KB
        result = await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == request.target_knowledge_base_id)
        )
        target_kb = result.scalar_one_or_none()
        if target_kb and not await check_kb_access(target_kb, user, "write", db):
            raise HTTPException(status_code=403, detail="No write access to target knowledge base")

    try:
        moved = await rag.move_documents(request.document_ids, request.target_knowledge_base_id)
        return {
            "message": f"{moved} Dokument(e) verschoben",
            "moved_count": moved,
            "target_knowledge_base_id": request.target_knowledge_base_id
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Knowledge Base Management
# =============================================================================

@router.post("/bases", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Erstellt eine neue Knowledge Base.

    Requires: kb.own or higher permission
    """
    # Permission check
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_perms = user.get_permissions()
        if not has_permission(user_perms, Permission.KB_OWN):
            raise HTTPException(status_code=403, detail="Permission required: kb.own or higher")

    try:
        kb = await rag.create_knowledge_base(data.name, data.description)

        # Set owner if authenticated
        if user:
            kb.owner_id = user.id
        kb.is_public = data.is_public
        await db.commit()
        await db.refresh(kb)

        return KnowledgeBaseResponse(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            is_active=kb.is_active,
            is_public=kb.is_public,
            owner_id=kb.owner_id,
            owner_username=user.username if user else None,
            document_count=0,
            created_at=kb.created_at.isoformat() if kb.created_at else "",
            updated_at=kb.updated_at.isoformat() if kb.updated_at else "",
            permission="owner" if user else "admin"
        )

    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(status_code=400, detail=f"Knowledge Base '{data.name}' existiert bereits")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bases", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Listet Knowledge Bases auf, gefiltert nach Benutzer-Berechtigung.

    - kb.all: Alle KBs
    - kb.shared: Eigene + öffentliche + geteilte
    - kb.own: Nur eigene
    - kb.none: Keine
    """
    # Get all KBs first
    all_bases = await rag.list_knowledge_bases()

    # Filter by access if auth is enabled
    if settings.auth_enabled and user:
        user_perms = user.get_permissions()

        # kb.all = see everything
        if has_permission(user_perms, Permission.KB_ALL):
            accessible_bases = all_bases
        # kb.none = nothing
        elif has_permission(user_perms, Permission.KB_NONE):
            return []
        else:
            from services.kb_shares_service import list_user_shared_kb_ids
            user_kb_ids: set[int] = await list_user_shared_kb_ids(db, user.id)

            accessible_bases = []
            for kb in all_bases:
                # Own KB
                if kb.owner_id == user.id or (kb.is_public and has_permission(user_perms, Permission.KB_SHARED)) or kb.id in user_kb_ids:
                    accessible_bases.append(kb)
    elif settings.auth_enabled and not user:
        # Auth enabled but no user = no access
        return []
    else:
        # Auth disabled = full access
        accessible_bases = all_bases

    # Batch-load all owner usernames in a single query
    owner_ids = {kb.owner_id for kb in accessible_bases if kb.owner_id}
    owner_map: dict[int, str] = {}
    if owner_ids:
        owner_result = await db.execute(
            select(User.id, User.username).where(User.id.in_(owner_ids))
        )
        owner_map = {row.id: row.username for row in owner_result.all()}

    # Build response with user-specific info
    response = []
    for kb in accessible_bases:
        perm = await get_user_kb_permission(kb, user, db) if user else "admin"
        owner_username = owner_map.get(kb.owner_id) if kb.owner_id else None

        response.append(KnowledgeBaseResponse(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            is_active=kb.is_active,
            is_public=kb.is_public if hasattr(kb, 'is_public') else False,
            owner_id=kb.owner_id if hasattr(kb, 'owner_id') else None,
            owner_username=owner_username,
            document_count=getattr(kb, '_document_count', 0),
            created_at=kb.created_at.isoformat() if kb.created_at else "",
            updated_at=kb.updated_at.isoformat() if kb.updated_at else "",
            permission=perm
        ))

    return response


@router.get("/bases/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Holt eine Knowledge Base nach ID"""
    kb = await rag.get_knowledge_base(kb_id)

    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge Base nicht gefunden")

    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if not await check_kb_access(kb, user, "read", db):
            raise HTTPException(status_code=403, detail="No access to this knowledge base")

    return KnowledgeBaseResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        is_active=kb.is_active,
        document_count=len(kb.documents) if kb.documents else 0,
        created_at=kb.created_at.isoformat() if kb.created_at else "",
        updated_at=kb.updated_at.isoformat() if kb.updated_at else ""
    )


@router.delete("/bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Löscht eine Knowledge Base mit allen Dokumenten"""
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        kb = await rag.get_knowledge_base(kb_id)
        if kb and not await check_kb_access(kb, user, "delete", db):
            raise HTTPException(status_code=403, detail="No delete access to this knowledge base")
    success = await rag.delete_knowledge_base(kb_id)

    if not success:
        raise HTTPException(status_code=404, detail="Knowledge Base nicht gefunden")

    return {"message": "Knowledge Base erfolgreich gelöscht", "id": kb_id}


# =============================================================================
# RAG Search
# =============================================================================

@router.post("/search", response_model=SearchResponse)
async def search_knowledge(
    request: SearchRequest,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Sucht in der Wissensdatenbank.

    Gibt die relevantesten Chunks für eine Anfrage zurück.
    """
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_perms = user.get_permissions()
        if has_permission(user_perms, Permission.KB_NONE) and not has_permission(user_perms, Permission.KB_OWN):
            raise HTTPException(status_code=403, detail="No knowledge base access")
        if request.knowledge_base_id:
            result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == request.knowledge_base_id)
            )
            kb = result.scalar_one_or_none()
            if kb and not await check_kb_access(kb, user, "read", db):
                raise HTTPException(status_code=403, detail="No access to this knowledge base")
    results = await rag.search(
        query=request.query,
        top_k=request.top_k,
        knowledge_base_id=request.knowledge_base_id,
        similarity_threshold=request.similarity_threshold,
        user_id=user.id if user else None,
    )

    return SearchResponse(
        query=request.query,
        results=[
            SearchResult(
                chunk=SearchResultChunk(
                    id=r["chunk"]["id"],
                    content=r["chunk"]["content"],
                    chunk_index=r["chunk"]["chunk_index"],
                    page_number=r["chunk"]["page_number"],
                    section_title=r["chunk"]["section_title"],
                    chunk_type=r["chunk"]["chunk_type"]
                ),
                document=SearchResultDocument(
                    id=r["document"]["id"],
                    filename=r["document"]["filename"],
                    title=r["document"]["title"]
                ),
                similarity=r["similarity"]
            )
            for r in results
        ],
        count=len(results)
    )


@router.get("/search")
async def search_knowledge_get(
    q: str = Query(..., min_length=1, description="Suchanfrage"),
    top_k: int = Query(5, ge=1, le=20),
    knowledge_base_id: int | None = Query(None),
    threshold: float | None = Query(None, ge=0, le=1, description="Similarity threshold (0-1)"),
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Sucht in der Wissensdatenbank (GET-Variante).
    """
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_perms = user.get_permissions()
        if has_permission(user_perms, Permission.KB_NONE) and not has_permission(user_perms, Permission.KB_OWN):
            raise HTTPException(status_code=403, detail="No knowledge base access")
        if knowledge_base_id:
            result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == knowledge_base_id)
            )
            kb = result.scalar_one_or_none()
            if kb and not await check_kb_access(kb, user, "read", db):
                raise HTTPException(status_code=403, detail="No access to this knowledge base")
    results = await rag.search(
        query=q,
        top_k=top_k,
        knowledge_base_id=knowledge_base_id,
        similarity_threshold=threshold,
        user_id=user.id if user else None,
    )

    return {
        "query": q,
        "results": results,
        "count": len(results)
    }


@router.post("/documents/{document_id}/search")
async def search_in_document(
    document_id: int,
    query: str = Body(..., embed=True),
    top_k: int = Body(5, ge=1, le=20),
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Sucht nur innerhalb eines bestimmten Dokuments"""
    # Prüfe ob Dokument existiert
    doc = await rag.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")

    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if doc.knowledge_base_id:
            result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
            )
            kb = result.scalar_one_or_none()
            if kb and not await check_kb_access(kb, user, "read", db):
                raise HTTPException(status_code=403, detail="No access to this document")

    results = await rag.search_by_document(
        query=query,
        document_id=document_id,
        top_k=top_k
    )

    return {
        "document_id": document_id,
        "query": query,
        "results": results,
        "count": len(results)
    }


# =============================================================================
# Statistics
# =============================================================================

@router.get("/stats", response_model=StatsResponse)
async def get_knowledge_stats(
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user)
):
    """Gibt Statistiken über die Wissensdatenbank zurück"""
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_perms = user.get_permissions()
        if not has_permission(user_perms, Permission.KB_OWN):
            raise HTTPException(status_code=403, detail="Permission required: kb.own or higher")
    stats = await rag.get_stats()
    return StatsResponse(**stats)


# =============================================================================
# Model Status
# =============================================================================

@router.post("/reindex-fts")
async def reindex_fts(
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user)
):
    """
    Re-populates search_vector (tsvector) for all document chunks.

    Use after changing FTS config or to backfill after migration.
    Admin-only when auth is enabled.
    """
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_perms = user.get_permissions()
        if not has_permission(user_perms, Permission.KB_ALL):
            raise HTTPException(status_code=403, detail="Admin permission required: kb.all")

    result = await rag.reindex_fts()
    return result


@router.post("/rag-eval")
async def run_rag_evaluation(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
    test_file: str | None = None,
):
    """
    Run RAG quality assessment pipeline with LLM-as-Judge scoring.

    Returns context relevance, faithfulness, answer quality, and source accuracy.
    Admin-only when auth is enabled.
    """
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_perms = user.get_permissions()
        if not has_permission(user_perms, Permission.KB_ALL):
            raise HTTPException(status_code=403, detail="Admin permission required: kb.all")

    from services.rag_eval_service import RAGEvalService
    assessment_svc = RAGEvalService(db)
    test_cases = assessment_svc.load_test_cases(test_file)
    results = await assessment_svc.evaluate(test_cases)
    return results


@router.get("/models/status")
async def get_model_status():
    """Prüft, ob die für RAG benötigten Modelle verfügbar sind"""
    from services.ollama_service import OllamaService

    ollama = OllamaService()
    status = await ollama.ensure_rag_models_loaded()

    all_ready = all(status.values())

    return {
        "ready": all_ready,
        "models": status,
        "message": "Alle RAG-Modelle verfügbar" if all_ready else "Einige Modelle fehlen"
    }


# =============================================================================
# Knowledge Base Sharing
# =============================================================================

@router.get("/bases/{kb_id}/permissions", response_model=list[KBPermissionResponse])
async def list_kb_permissions(
    kb_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    List all permissions for a knowledge base. Owner or admin only.

    Lane C: aggregates atom_explicit_grants by user_id so one logical
    KB-share surfaces as one row even though it's stored per chunk.
    The `id` field in the response is the granted_to_user_id (same thing
    the revoke endpoint's {permission_id} path param now accepts).
    """
    kb = await rag.get_knowledge_base(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge Base not found")

    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_perm = await get_user_kb_permission(kb, user, db)
        if user_perm not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Only owner or admin can view permissions")

    from services.kb_shares_service import list_kb_shares
    shares = await list_kb_shares(db, kb_id)

    # Batch-load all referenced users
    all_user_ids: set[int] = set()
    for s in shares:
        all_user_ids.add(s["user_id"])
        if s["granted_by"]:
            all_user_ids.add(s["granted_by"])

    user_map: dict[int, User] = {}
    if all_user_ids:
        user_result = await db.execute(select(User).where(User.id.in_(all_user_ids)))
        user_map = {u.id: u for u in user_result.scalars().all()}

    response = []
    for s in shares:
        perm_user = user_map.get(s["user_id"])
        granter = user_map.get(s["granted_by"]) if s["granted_by"] else None
        response.append(KBPermissionResponse(
            id=s["user_id"],
            user_id=s["user_id"],
            username=perm_user.username if perm_user else "Unknown",
            permission=s["permission"],
            granted_by=s["granted_by"],
            granted_by_username=granter.username if granter else None,
            created_at=s["granted_at"].isoformat() if s["granted_at"] else "",
        ))
    return response


@router.post("/bases/{kb_id}/share", response_model=KBPermissionResponse)
async def share_knowledge_base(
    kb_id: int,
    data: KBPermissionCreate,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """Share a knowledge base with another user. Owner or admin only."""
    kb = await rag.get_knowledge_base(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge Base not found")

    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_perm = await get_user_kb_permission(kb, user, db)
        if user_perm not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Only owner or admin can share")

    target_result = await db.execute(select(User).where(User.id == data.user_id))
    target_user = target_result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found")

    if user and target_user.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot share with yourself")

    if kb.owner_id and target_user.id == kb.owner_id:
        raise HTTPException(status_code=400, detail="User is already the owner")

    from services.kb_shares_service import share_kb, list_kb_shares
    await share_kb(
        db, kb_id,
        target_user_id=data.user_id,
        permission_level=data.permission,
        granted_by=user.id if user else None,
    )

    # Fetch the aggregated share row to echo back
    shares = await list_kb_shares(db, kb_id)
    share_row = next((s for s in shares if s["user_id"] == data.user_id), None)
    granted_at = share_row["granted_at"].isoformat() if share_row and share_row["granted_at"] else ""

    return KBPermissionResponse(
        id=data.user_id,
        user_id=data.user_id,
        username=target_user.username,
        permission=data.permission,
        granted_by=user.id if user else None,
        granted_by_username=user.username if user else None,
        created_at=granted_at,
    )


@router.delete("/bases/{kb_id}/permissions/{permission_id}")
async def revoke_kb_permission(
    kb_id: int,
    permission_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Revoke a user's access to a knowledge base. Owner or admin only.

    Lane C: `permission_id` is now the `granted_to_user_id` (matches the
    `id` field returned by the list endpoint).
    """
    kb = await rag.get_knowledge_base(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge Base not found")

    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_perm = await get_user_kb_permission(kb, user, db)
        if user_perm not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Only owner or admin can revoke permissions")

    from services.kb_shares_service import revoke_kb_share
    removed = await revoke_kb_share(db, kb_id, target_user_id=permission_id)
    if removed == 0:
        raise HTTPException(status_code=404, detail="Permission not found")

    return {"message": "Permission revoked"}


@router.patch("/bases/{kb_id}/public")
async def set_kb_public(
    kb_id: int,
    is_public: bool = Body(..., embed=True),
    rag: RAGService = Depends(get_rag_service),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Set a knowledge base as public or private.

    Only owner or admin can change visibility.
    """
    kb = await rag.get_knowledge_base(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge Base not found")

    # Check access
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_perm = await get_user_kb_permission(kb, user, db)
        if user_perm not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Only owner or admin can change visibility")

    kb.is_public = is_public
    await db.commit()

    return {"message": f"Knowledge Base is now {'public' if is_public else 'private'}", "is_public": is_public}
