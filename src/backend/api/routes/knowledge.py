"""
Knowledge API Routes

Endpoints für Dokument-Upload, Management und RAG-Suche.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
import aiofiles
import os
from pathlib import Path
from loguru import logger

from services.database import get_db
from services.rag_service import RAGService
from utils.config import settings

router = APIRouter()


# =============================================================================
# Pydantic Models
# =============================================================================

class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class KnowledgeBaseResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    is_active: bool
    document_count: int = 0
    created_at: str
    updated_at: str


class DocumentResponse(BaseModel):
    id: int
    filename: str
    title: Optional[str]
    file_type: Optional[str]
    file_size: Optional[int]
    status: str
    error_message: Optional[str]
    chunk_count: int
    page_count: Optional[int]
    knowledge_base_id: Optional[int]
    created_at: str
    processed_at: Optional[str]


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    knowledge_base_id: Optional[int] = None
    similarity_threshold: Optional[float] = Field(default=None, ge=0, le=1)


class SearchResultChunk(BaseModel):
    id: int
    content: str
    chunk_index: int
    page_number: Optional[int]
    section_title: Optional[str]
    chunk_type: str


class SearchResultDocument(BaseModel):
    id: int
    filename: str
    title: Optional[str]


class SearchResult(BaseModel):
    chunk: SearchResultChunk
    document: SearchResultDocument
    similarity: float


class SearchResponse(BaseModel):
    query: str
    results: List[SearchResult]
    count: int


class StatsResponse(BaseModel):
    document_count: int
    completed_documents: int
    chunk_count: int
    knowledge_base_count: int
    embedding_model: str
    embedding_dimension: int


# =============================================================================
# Helper Functions
# =============================================================================

def get_rag_service(db: AsyncSession = Depends(get_db)) -> RAGService:
    """Dependency für RAG Service"""
    return RAGService(db)


# =============================================================================
# Document Upload
# =============================================================================

@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    knowledge_base_id: Optional[int] = Query(None, description="Knowledge Base ID"),
    rag: RAGService = Depends(get_rag_service)
):
    """
    Lädt ein Dokument hoch und indexiert es für RAG.

    Unterstützte Formate: PDF, DOCX, TXT, MD, HTML, PPTX, XLSX
    """
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
            content = await file.read()
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
            filename=file.filename
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
    rag: RAGService = Depends(get_rag_service)
):
    """Erstellt eine neue Knowledge Base"""
    try:
        kb = await rag.create_knowledge_base(data.name, data.description)

        return KnowledgeBaseResponse(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            is_active=kb.is_active,
            document_count=0,
            created_at=kb.created_at.isoformat() if kb.created_at else "",
            updated_at=kb.updated_at.isoformat() if kb.updated_at else ""
        )

    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(status_code=400, detail=f"Knowledge Base '{data.name}' existiert bereits")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bases", response_model=List[KnowledgeBaseResponse])
async def list_knowledge_bases(
    rag: RAGService = Depends(get_rag_service)
):
    """Listet alle Knowledge Bases auf"""
    bases = await rag.list_knowledge_bases()

    return [
        KnowledgeBaseResponse(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            is_active=kb.is_active,
            document_count=len(kb.documents) if kb.documents else 0,
            created_at=kb.created_at.isoformat() if kb.created_at else "",
            updated_at=kb.updated_at.isoformat() if kb.updated_at else ""
        )
        for kb in bases
    ]


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
