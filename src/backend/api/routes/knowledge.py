"""
Knowledge API Routes

Endpoints für Dokument-Upload, Management und RAG-Suche.
With RPBAC permission checks for secure access control.
Pydantic schemas are defined in knowledge_schemas.py.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import aiofiles
import os
import hashlib
from pathlib import Path
from loguru import logger

from services.database import get_db
from services.rag_service import RAGService
from services.auth_service import get_optional_user
from models.database import Document, KnowledgeBase, User, KBPermission
from models.permissions import Permission, has_permission
from utils.config import settings

# Import all schemas from separate file
from .knowledge_schemas import (
    KnowledgeBaseCreate, KnowledgeBaseResponse,
    KBPermissionCreate, KBPermissionResponse,
    DocumentResponse,
    SearchRequest, SearchResultChunk, SearchResultDocument, SearchResult, SearchResponse,
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
    user: Optional[User],
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

    # Check explicit KBPermission
    if db:
        result = await db.execute(
            select(KBPermission).where(
                KBPermission.knowledge_base_id == kb.id,
                KBPermission.user_id == user.id
            )
        )
        perm = result.scalar_one_or_none()
        if perm:
            # Permission levels: read < write < admin
            perm_levels = {"read": 1, "write": 2, "admin": 3}
            required_level = perm_levels.get(required_action, 1)
            user_level = perm_levels.get(perm.permission, 0)
            if user_level >= required_level:
                return True

    return False


async def get_user_kb_permission(
    kb: KnowledgeBase,
    user: Optional[User],
    db: AsyncSession
) -> Optional[str]:
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

    # Check explicit permission
    result = await db.execute(
        select(KBPermission).where(
            KBPermission.knowledge_base_id == kb.id,
            KBPermission.user_id == user.id
        )
    )
    perm = result.scalar_one_or_none()
    if perm:
        return perm.permission

    # Public KB + kb.shared = read
    if kb.is_public and has_permission(user_perms, Permission.KB_SHARED):
        return "read"

    return None


# =============================================================================
# Document Upload
# =============================================================================

@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    knowledge_base_id: Optional[int] = Query(None, description="Knowledge Base ID"),
    rag: RAGService = Depends(get_rag_service),
    user: Optional[User] = Depends(get_optional_user),
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
    # Validierung: Dateiformat
    extension = Path(file.filename).suffix.lower().lstrip('.')
    allowed = settings.allowed_extensions_list

    if extension not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Dateiformat '{extension}' nicht unterstützt. Erlaubt: {', '.join(allowed)}"
        )

    # Validierung: Dateigröße
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)

    max_size = settings.max_file_size_mb * 1024 * 1024
    if size > max_size:
        raise HTTPException(
            status_code=400,
            detail=f"Datei zu groß ({size // 1024 // 1024}MB). Maximum: {settings.max_file_size_mb}MB"
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
                "message": f"Dieses Dokument existiert bereits in der Knowledge Base",
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
    import uuid
    unique_filename = f"{uuid.uuid4().hex}_{file.filename}"
    file_path = upload_dir / unique_filename

    # Datei speichern
    try:
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(content)

        logger.info(f"Datei gespeichert: {file_path}")

    except Exception as e:
        logger.error(f"Fehler beim Speichern der Datei: {e}")
        raise HTTPException(status_code=500, detail=f"Fehler beim Speichern: {str(e)}")

    # Dokument verarbeiten und indexieren
    try:
        document = await rag.ingest_document(
            str(file_path),
            knowledge_base_id=knowledge_base_id,
            filename=file.filename,
            file_hash=file_hash
        )

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

    except Exception as e:
        # Datei bei Fehler löschen
        if file_path.exists():
            os.remove(file_path)
        logger.error(f"Fehler beim Indexieren: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Document Management
# =============================================================================

@router.get("/documents", response_model=List[DocumentResponse])
async def list_documents(
    knowledge_base_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    rag: RAGService = Depends(get_rag_service)
):
    """Listet alle indexierten Dokumente auf"""
    documents = await rag.list_documents(
        knowledge_base_id=knowledge_base_id,
        status=status,
        limit=limit,
        offset=offset
    )

    return [
        DocumentResponse(
            id=doc.id,
            filename=doc.filename,
            title=doc.title,
            file_type=doc.file_type,
            file_size=doc.file_size,
            status=doc.status,
            error_message=doc.error_message,
            chunk_count=doc.chunk_count or 0,
            page_count=doc.page_count,
            knowledge_base_id=doc.knowledge_base_id,
            created_at=doc.created_at.isoformat() if doc.created_at else "",
            processed_at=doc.processed_at.isoformat() if doc.processed_at else None
        )
        for doc in documents
    ]


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: int,
    rag: RAGService = Depends(get_rag_service)
):
    """Holt Details zu einem Dokument"""
    document = await rag.get_document(document_id)

    if not document:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")

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


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: int,
    rag: RAGService = Depends(get_rag_service)
):
    """Löscht ein Dokument und alle zugehörigen Chunks"""
    success = await rag.delete_document(document_id)

    if not success:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")

    return {"message": "Dokument erfolgreich gelöscht", "id": document_id}


@router.post("/documents/{document_id}/reindex", response_model=DocumentResponse)
async def reindex_document(
    document_id: int,
    rag: RAGService = Depends(get_rag_service)
):
    """Re-indexiert ein Dokument (löscht alte Chunks und erstellt neue)"""
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


# =============================================================================
# Knowledge Base Management
# =============================================================================

@router.post("/bases", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    rag: RAGService = Depends(get_rag_service),
    user: Optional[User] = Depends(get_optional_user),
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


@router.get("/bases", response_model=List[KnowledgeBaseResponse])
async def list_knowledge_bases(
    rag: RAGService = Depends(get_rag_service),
    user: Optional[User] = Depends(get_optional_user),
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
            accessible_bases = []
            for kb in all_bases:
                # Own KB
                if kb.owner_id == user.id:
                    accessible_bases.append(kb)
                # Public KB (for kb.shared users)
                elif kb.is_public and has_permission(user_perms, Permission.KB_SHARED):
                    accessible_bases.append(kb)
                else:
                    # Check explicit permission
                    result = await db.execute(
                        select(KBPermission).where(
                            KBPermission.knowledge_base_id == kb.id,
                            KBPermission.user_id == user.id
                        )
                    )
                    if result.scalar_one_or_none():
                        accessible_bases.append(kb)
    elif settings.auth_enabled and not user:
        # Auth enabled but no user = no access
        return []
    else:
        # Auth disabled = full access
        accessible_bases = all_bases

    # Build response with user-specific info
    response = []
    for kb in accessible_bases:
        perm = await get_user_kb_permission(kb, user, db) if user else "admin"

        # Get owner username
        owner_username = None
        if kb.owner_id:
            result = await db.execute(select(User).where(User.id == kb.owner_id))
            owner = result.scalar_one_or_none()
            if owner:
                owner_username = owner.username

        response.append(KnowledgeBaseResponse(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            is_active=kb.is_active,
            is_public=kb.is_public if hasattr(kb, 'is_public') else False,
            owner_id=kb.owner_id if hasattr(kb, 'owner_id') else None,
            owner_username=owner_username,
            document_count=len(kb.documents) if kb.documents else 0,
            created_at=kb.created_at.isoformat() if kb.created_at else "",
            updated_at=kb.updated_at.isoformat() if kb.updated_at else "",
            permission=perm
        ))

    return response


@router.get("/bases/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: int,
    rag: RAGService = Depends(get_rag_service)
):
    """Holt eine Knowledge Base nach ID"""
    kb = await rag.get_knowledge_base(kb_id)

    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge Base nicht gefunden")

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
    rag: RAGService = Depends(get_rag_service)
):
    """Löscht eine Knowledge Base mit allen Dokumenten"""
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
    rag: RAGService = Depends(get_rag_service)
):
    """
    Sucht in der Wissensdatenbank.

    Gibt die relevantesten Chunks für eine Anfrage zurück.
    """
    results = await rag.search(
        query=request.query,
        top_k=request.top_k,
        knowledge_base_id=request.knowledge_base_id,
        similarity_threshold=request.similarity_threshold
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
    knowledge_base_id: Optional[int] = Query(None),
    threshold: Optional[float] = Query(None, ge=0, le=1, description="Similarity threshold (0-1)"),
    rag: RAGService = Depends(get_rag_service)
):
    """
    Sucht in der Wissensdatenbank (GET-Variante).
    """
    results = await rag.search(
        query=q,
        top_k=top_k,
        knowledge_base_id=knowledge_base_id,
        similarity_threshold=threshold
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
    rag: RAGService = Depends(get_rag_service)
):
    """Sucht nur innerhalb eines bestimmten Dokuments"""
    # Prüfe ob Dokument existiert
    doc = await rag.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")

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
    rag: RAGService = Depends(get_rag_service)
):
    """Gibt Statistiken über die Wissensdatenbank zurück"""
    stats = await rag.get_stats()
    return StatsResponse(**stats)


# =============================================================================
# Model Status
# =============================================================================

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

@router.get("/bases/{kb_id}/permissions", response_model=List[KBPermissionResponse])
async def list_kb_permissions(
    kb_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: Optional[User] = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    List all permissions for a knowledge base.

    Only owner or admin can view permissions.
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
            raise HTTPException(status_code=403, detail="Only owner or admin can view permissions")

    # Get all permissions
    result = await db.execute(
        select(KBPermission).where(KBPermission.knowledge_base_id == kb_id)
    )
    permissions = result.scalars().all()

    response = []
    for perm in permissions:
        # Get user info
        user_result = await db.execute(select(User).where(User.id == perm.user_id))
        perm_user = user_result.scalar_one_or_none()

        # Get granter info
        granter_username = None
        if perm.granted_by:
            granter_result = await db.execute(select(User).where(User.id == perm.granted_by))
            granter = granter_result.scalar_one_or_none()
            if granter:
                granter_username = granter.username

        response.append(KBPermissionResponse(
            id=perm.id,
            user_id=perm.user_id,
            username=perm_user.username if perm_user else "Unknown",
            permission=perm.permission,
            granted_by=perm.granted_by,
            granted_by_username=granter_username,
            created_at=perm.created_at.isoformat() if perm.created_at else ""
        ))

    return response


@router.post("/bases/{kb_id}/share", response_model=KBPermissionResponse)
async def share_knowledge_base(
    kb_id: int,
    data: KBPermissionCreate,
    rag: RAGService = Depends(get_rag_service),
    user: Optional[User] = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Share a knowledge base with another user.

    Only owner or admin can share.
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
            raise HTTPException(status_code=403, detail="Only owner or admin can share")

    # Check target user exists
    target_result = await db.execute(select(User).where(User.id == data.user_id))
    target_user = target_result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found")

    # Can't share with yourself
    if user and target_user.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot share with yourself")

    # Can't share with owner
    if kb.owner_id and target_user.id == kb.owner_id:
        raise HTTPException(status_code=400, detail="User is already the owner")

    # Check if permission already exists
    existing_result = await db.execute(
        select(KBPermission).where(
            KBPermission.knowledge_base_id == kb_id,
            KBPermission.user_id == data.user_id
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing:
        # Update existing permission
        existing.permission = data.permission
        existing.granted_by = user.id if user else None
        await db.commit()
        await db.refresh(existing)
        perm = existing
    else:
        # Create new permission
        perm = KBPermission(
            knowledge_base_id=kb_id,
            user_id=data.user_id,
            permission=data.permission,
            granted_by=user.id if user else None
        )
        db.add(perm)
        await db.commit()
        await db.refresh(perm)

    return KBPermissionResponse(
        id=perm.id,
        user_id=perm.user_id,
        username=target_user.username,
        permission=perm.permission,
        granted_by=perm.granted_by,
        granted_by_username=user.username if user else None,
        created_at=perm.created_at.isoformat() if perm.created_at else ""
    )


@router.delete("/bases/{kb_id}/permissions/{permission_id}")
async def revoke_kb_permission(
    kb_id: int,
    permission_id: int,
    rag: RAGService = Depends(get_rag_service),
    user: Optional[User] = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Revoke a user's access to a knowledge base.

    Only owner or admin can revoke permissions.
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
            raise HTTPException(status_code=403, detail="Only owner or admin can revoke permissions")

    # Get permission
    result = await db.execute(
        select(KBPermission).where(
            KBPermission.id == permission_id,
            KBPermission.knowledge_base_id == kb_id
        )
    )
    perm = result.scalar_one_or_none()

    if not perm:
        raise HTTPException(status_code=404, detail="Permission not found")

    await db.delete(perm)
    await db.commit()

    return {"message": "Permission revoked"}


@router.patch("/bases/{kb_id}/public")
async def set_kb_public(
    kb_id: int,
    is_public: bool = Body(..., embed=True),
    rag: RAGService = Depends(get_rag_service),
    user: Optional[User] = Depends(get_optional_user),
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
