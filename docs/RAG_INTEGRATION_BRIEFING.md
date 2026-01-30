# RAG-Integration Briefing für Renfield

> **Status (2026-01-30):** RAG ist vollständig implementiert inkl. Hybrid Search (Dense + BM25 via RRF), Context Window Retrieval, Knowledge Base Sharing (RPBAC) und Admin-Reindex-Endpoint. Dieses Dokument dient als historisches Planungsdokument.

## Projektübersicht

Renfield ist ein lokales Smart-Home-Assistenzsystem mit Sprach- und Chat-Interface. Es soll um eine RAG-Komponente (Retrieval-Augmented Generation) erweitert werden, um einen lokalen Wissensspeicher mit Dokumenten (PDF, DOCX, etc.) per Chat abfragen zu können.

**WICHTIG: Alle Daten müssen lokal bleiben - keine Cloud-Uploads!**

---

## Bestehendes System

### Tech-Stack
- **Backend:** Python 3.11, FastAPI, Uvicorn
- **ORM:** SQLAlchemy 2.0 (async mit asyncpg)
- **Datenbank:** PostgreSQL 16
- **Cache:** Redis 7
- **LLM:** Ollama (lokal)
- **Frontend:** React 18, Vite 5, Tailwind CSS
- **Deployment:** Docker Compose

### Relevante bestehende Dateien
```
src/backend/
├── api/routes/
│   ├── chat.py              # Chat-API
│   └── ...
├── services/
│   ├── ollama_service.py    # LLM-Interaktion (nutzt bereits LangChain)
│   └── ...
├── models/
│   └── database.py          # SQLAlchemy Models
├── utils/
│   └── config.py            # Pydantic Settings
├── main.py                  # FastAPI App + WebSocket
├── Dockerfile
└── requirements.txt
```

### Bestehende Dependencies (bereits installiert)
```
fastapi==0.109.0
sqlalchemy==2.0.25
asyncpg==0.29.0
ollama==0.1.6
langchain==0.1.0
langchain-community==0.0.13
```

### Bestehende Konfiguration (.env)
```
DATABASE_URL=postgresql://renfield:changeme@postgres:5432/renfield
REDIS_URL=redis://redis:6379
OLLAMA_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2:3b
```

### Bestehendes Chat-Interface
- WebSocket Endpoint: `/ws` mit Streaming-Support
- REST API: `/api/chat/` mit History, Search, Stats
- Datenbank-Modelle: `Conversation`, `Message`

---

## Anforderungen für RAG-Integration

### Funktionale Anforderungen
1. Dokumente hochladen (PDF, DOCX, TXT, MD, HTML)
2. Dokumente automatisch parsen und in Chunks aufteilen
3. Chunks vektorisieren und in Datenbank speichern
4. Bei Chat-Anfragen relevante Chunks abrufen
5. LLM-Antworten mit Dokumentenkontext anreichern
6. Quellenangaben in Antworten anzeigen
7. Dokumente verwalten (auflisten, löschen)

### Technische Anforderungen
1. **Docling** von IBM für Dokumentenextraktion (bessere Tabellen, Formeln, Layout)
2. **pgvector** Extension für PostgreSQL (Vektorspeicher)
3. **Multi-Modell-Support:** Verschiedene LLMs für verschiedene Aufgaben
4. Integration in bestehendes Chat-Interface
5. Vollständig lokal lauffähig

---

## Technische Spezifikation

### 1. Neue Dependencies

Füge zu `requirements.txt` hinzu:
```
# RAG & Document Processing
docling>=2.0.0
docling-core>=2.0.0
langchain-ollama>=0.1.0
langchain-postgres>=0.0.6
pgvector>=0.2.4

# Falls nicht schon vorhanden
pypdf>=4.0.0
python-docx>=1.1.0
```

### 2. Docker Compose Anpassung

Ersetze PostgreSQL Image mit pgvector-Version:
```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    # Rest bleibt gleich
```

### 3. Neue Umgebungsvariablen

Erweitere `.env.example`:
```bash
# Multi-Modell Konfiguration
OLLAMA_CHAT_MODEL=llama3.2:3b
OLLAMA_RAG_MODEL=llama3.1:8b
OLLAMA_EMBED_MODEL=nomic-embed-text
OLLAMA_INTENT_MODEL=llama3.2:3b

# RAG Einstellungen
RAG_CHUNK_SIZE=512
RAG_CHUNK_OVERLAP=50
RAG_TOP_K=5
RAG_SIMILARITY_THRESHOLD=0.7
```

### 4. Neue Datenbank-Modelle

Erstelle/erweitere `src/backend/models/database.py`:

```python
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime

class KnowledgeBase(Base):
    """Gruppierung von Dokumenten"""
    __tablename__ = "knowledge_bases"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan")


class Document(Base):
    """Hochgeladene Dokumente (Metadaten)"""
    __tablename__ = "documents"
    
    id = Column(Integer, primary_key=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)
    file_type = Column(String(50))  # pdf, docx, txt, etc.
    file_size = Column(Integer)  # in bytes
    
    # Processing Status
    status = Column(String(50), default="pending")  # pending, processing, completed, failed
    error_message = Column(Text)
    
    # Metadata
    title = Column(String(512))
    author = Column(String(255))
    page_count = Column(Integer)
    chunk_count = Column(Integer, default=0)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime)
    
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    """Text-Chunks mit Embedding-Vektor"""
    __tablename__ = "document_chunks"
    
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    
    # Content
    content = Column(Text, nullable=False)
    
    # Embedding Vector (768 dimensions for nomic-embed-text)
    embedding = Column(Vector(768))
    
    # Chunk Metadata
    chunk_index = Column(Integer)  # Position im Dokument
    page_number = Column(Integer)
    section_title = Column(String(512))
    chunk_type = Column(String(50))  # paragraph, table, code, formula, etc.
    
    # Additional Metadata (from Docling)
    metadata = Column(JSON)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    document = relationship("Document", back_populates="chunks")
```

### 5. Konfiguration erweitern

Erweitere `src/backend/utils/config.py`:

```python
class Settings(BaseSettings):
    # Bestehende Settings...
    
    # Multi-Modell Konfiguration
    ollama_chat_model: str = "llama3.2:3b"
    ollama_rag_model: str = "llama3.1:8b"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_intent_model: str = "llama3.2:3b"
    
    # RAG Einstellungen
    rag_chunk_size: int = 512
    rag_chunk_overlap: int = 50
    rag_top_k: int = 5
    rag_similarity_threshold: float = 0.7
    
    # Upload Einstellungen
    upload_dir: str = "/app/data/uploads"
    max_file_size_mb: int = 50
    allowed_extensions: list = ["pdf", "docx", "doc", "txt", "md", "html"]
```

### 6. Document Processor Service (Docling)

Erstelle `src/backend/services/document_processor.py`:

```python
"""
Document Processor Service using IBM Docling
Handles document parsing, chunking, and metadata extraction
"""
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from docling.document_converter import DocumentConverter
from docling.chunking import HybridChunker
from docling.datamodel.base_models import DocumentStream

from utils.config import get_settings

settings = get_settings()


class DocumentProcessor:
    """Prozessiert Dokumente mit Docling für RAG"""
    
    def __init__(self):
        self.converter = DocumentConverter()
        self.chunker = HybridChunker(
            tokenizer="sentence-transformers/all-MiniLM-L6-v2",
            max_tokens=settings.rag_chunk_size,
            overlap=settings.rag_chunk_overlap
        )
    
    async def process_document(self, file_path: str) -> Dict[str, Any]:
        """
        Verarbeitet ein Dokument und extrahiert strukturierte Chunks
        
        Returns:
            {
                "metadata": {...},
                "chunks": [{"text": ..., "metadata": {...}}, ...]
            }
        """
        try:
            # Dokument konvertieren
            result = self.converter.convert(file_path)
            doc = result.document
            
            # Metadaten extrahieren
            metadata = self._extract_metadata(doc, file_path)
            
            # Chunks erstellen mit Struktur-Erhaltung
            chunks = self._create_chunks(doc)
            
            return {
                "metadata": metadata,
                "chunks": chunks,
                "status": "completed"
            }
            
        except Exception as e:
            return {
                "metadata": {},
                "chunks": [],
                "status": "failed",
                "error": str(e)
            }
    
    def _extract_metadata(self, doc, file_path: str) -> Dict[str, Any]:
        """Extrahiert Dokument-Metadaten"""
        path = Path(file_path)
        
        return {
            "filename": path.name,
            "file_type": path.suffix.lower().lstrip('.'),
            "file_size": path.stat().st_size,
            "title": getattr(doc, 'title', None) or path.stem,
            "author": getattr(doc, 'author', None),
            "page_count": getattr(doc, 'page_count', None),
            "processed_at": datetime.utcnow().isoformat()
        }
    
    def _create_chunks(self, doc) -> List[Dict[str, Any]]:
        """Erstellt Chunks mit Docling HybridChunker"""
        chunks = []
        
        for idx, chunk in enumerate(self.chunker.chunk(doc)):
            chunk_data = {
                "text": chunk.text,
                "chunk_index": idx,
                "metadata": {
                    "headings": chunk.meta.headings if hasattr(chunk.meta, 'headings') else [],
                    "chunk_type": self._get_chunk_type(chunk),
                    "page_number": self._get_page_number(chunk),
                }
            }
            chunks.append(chunk_data)
        
        return chunks
    
    def _get_chunk_type(self, chunk) -> str:
        """Ermittelt den Chunk-Typ (paragraph, table, code, etc.)"""
        if hasattr(chunk.meta, 'doc_items') and chunk.meta.doc_items:
            return chunk.meta.doc_items[0].label
        return "paragraph"
    
    def _get_page_number(self, chunk) -> Optional[int]:
        """Ermittelt die Seitennummer des Chunks"""
        try:
            if hasattr(chunk.meta, 'doc_items') and chunk.meta.doc_items:
                prov = chunk.meta.doc_items[0].prov
                if prov:
                    return prov[0].page_no
        except:
            pass
        return None
    
    def get_supported_formats(self) -> List[str]:
        """Gibt unterstützte Dateiformate zurück"""
        return ["pdf", "docx", "doc", "pptx", "xlsx", "html", "md", "txt", "png", "jpg", "jpeg"]
```

### 7. RAG Service

Erstelle `src/backend/services/rag_service.py`:

```python
"""
RAG Service - Retrieval Augmented Generation
Handles document ingestion, embedding, and retrieval
"""
from typing import List, Dict, Any, Optional
from datetime import datetime
import asyncio

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload
from pgvector.sqlalchemy import Vector

from models.database import Document, DocumentChunk, KnowledgeBase
from services.document_processor import DocumentProcessor
from services.ollama_service import OllamaService
from utils.config import get_settings

settings = get_settings()


class RAGService:
    """RAG Service für Dokument-basierte Anfragen"""
    
    def __init__(self, db: AsyncSession, ollama: OllamaService):
        self.db = db
        self.ollama = ollama
        self.processor = DocumentProcessor()
    
    # ==================== Document Ingestion ====================
    
    async def ingest_document(
        self, 
        file_path: str, 
        knowledge_base_id: Optional[int] = None
    ) -> Document:
        """
        Verarbeitet und indexiert ein Dokument
        
        1. Dokument mit Docling parsen
        2. Chunks erstellen
        3. Embeddings generieren
        4. In Datenbank speichern
        """
        # Document-Eintrag erstellen
        doc = Document(
            file_path=file_path,
            filename=file_path.split('/')[-1],
            knowledge_base_id=knowledge_base_id,
            status="processing"
        )
        self.db.add(doc)
        await self.db.commit()
        await self.db.refresh(doc)
        
        try:
            # 1. Dokument verarbeiten
            result = await self.processor.process_document(file_path)
            
            if result["status"] == "failed":
                doc.status = "failed"
                doc.error_message = result.get("error", "Unknown error")
                await self.db.commit()
                return doc
            
            # 2. Metadaten aktualisieren
            metadata = result["metadata"]
            doc.title = metadata.get("title")
            doc.author = metadata.get("author")
            doc.file_type = metadata.get("file_type")
            doc.file_size = metadata.get("file_size")
            doc.page_count = metadata.get("page_count")
            
            # 3. Chunks mit Embeddings erstellen
            chunks = result["chunks"]
            for chunk_data in chunks:
                # Embedding generieren
                embedding = await self.ollama.get_embedding(chunk_data["text"])
                
                chunk = DocumentChunk(
                    document_id=doc.id,
                    content=chunk_data["text"],
                    embedding=embedding,
                    chunk_index=chunk_data["chunk_index"],
                    page_number=chunk_data["metadata"].get("page_number"),
                    section_title=", ".join(chunk_data["metadata"].get("headings", [])),
                    chunk_type=chunk_data["metadata"].get("chunk_type", "paragraph"),
                    metadata=chunk_data["metadata"]
                )
                self.db.add(chunk)
            
            doc.chunk_count = len(chunks)
            doc.status = "completed"
            doc.processed_at = datetime.utcnow()
            
            await self.db.commit()
            await self.db.refresh(doc)
            
            return doc
            
        except Exception as e:
            doc.status = "failed"
            doc.error_message = str(e)
            await self.db.commit()
            raise
    
    # ==================== Retrieval ====================
    
    async def search(
        self, 
        query: str, 
        top_k: int = None,
        knowledge_base_id: Optional[int] = None,
        similarity_threshold: float = None
    ) -> List[Dict[str, Any]]:
        """
        Sucht relevante Chunks für eine Anfrage
        
        Returns:
            List of {chunk, document, similarity_score}
        """
        top_k = top_k or settings.rag_top_k
        threshold = similarity_threshold or settings.rag_similarity_threshold
        
        # Query-Embedding erstellen
        query_embedding = await self.ollama.get_embedding(query)
        
        # Similarity Search mit pgvector
        # Cosine Distance: 1 - similarity, daher sortieren wir aufsteigend
        stmt = (
            select(
                DocumentChunk,
                DocumentChunk.embedding.cosine_distance(query_embedding).label("distance")
            )
            .join(Document)
            .where(Document.status == "completed")
        )
        
        if knowledge_base_id:
            stmt = stmt.where(Document.knowledge_base_id == knowledge_base_id)
        
        stmt = stmt.order_by("distance").limit(top_k)
        
        result = await self.db.execute(stmt)
        rows = result.all()
        
        # Ergebnisse formatieren
        results = []
        for chunk, distance in rows:
            similarity = 1 - distance  # Convert distance to similarity
            
            if similarity >= threshold:
                # Document laden
                doc_stmt = select(Document).where(Document.id == chunk.document_id)
                doc_result = await self.db.execute(doc_stmt)
                document = doc_result.scalar_one()
                
                results.append({
                    "chunk": {
                        "id": chunk.id,
                        "content": chunk.content,
                        "page_number": chunk.page_number,
                        "section_title": chunk.section_title,
                        "chunk_type": chunk.chunk_type
                    },
                    "document": {
                        "id": document.id,
                        "filename": document.filename,
                        "title": document.title
                    },
                    "similarity": round(similarity, 4)
                })
        
        return results
    
    async def get_context(
        self, 
        query: str, 
        top_k: int = None,
        knowledge_base_id: Optional[int] = None
    ) -> str:
        """
        Erstellt einen formatierten Kontext-String für das LLM
        """
        results = await self.search(query, top_k, knowledge_base_id)
        
        if not results:
            return ""
        
        context_parts = []
        for i, result in enumerate(results, 1):
            chunk = result["chunk"]
            doc = result["document"]
            
            context_parts.append(
                f"[Quelle {i}: {doc['filename']}"
                f"{f', Seite {chunk[\"page_number\"]}' if chunk['page_number'] else ''}"
                f"{f', {chunk[\"section_title\"]}' if chunk['section_title'] else ''}]\n"
                f"{chunk['content']}"
            )
        
        return "\n\n---\n\n".join(context_parts)
    
    # ==================== Document Management ====================
    
    async def list_documents(
        self, 
        knowledge_base_id: Optional[int] = None
    ) -> List[Document]:
        """Listet alle Dokumente auf"""
        stmt = select(Document).order_by(Document.created_at.desc())
        
        if knowledge_base_id:
            stmt = stmt.where(Document.knowledge_base_id == knowledge_base_id)
        
        result = await self.db.execute(stmt)
        return result.scalars().all()
    
    async def get_document(self, document_id: int) -> Optional[Document]:
        """Holt ein einzelnes Dokument"""
        stmt = select(Document).where(Document.id == document_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def delete_document(self, document_id: int) -> bool:
        """Löscht ein Dokument und alle zugehörigen Chunks"""
        stmt = delete(Document).where(Document.id == document_id)
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0
    
    # ==================== Knowledge Base Management ====================
    
    async def create_knowledge_base(self, name: str, description: str = None) -> KnowledgeBase:
        """Erstellt eine neue Knowledge Base"""
        kb = KnowledgeBase(name=name, description=description)
        self.db.add(kb)
        await self.db.commit()
        await self.db.refresh(kb)
        return kb
    
    async def list_knowledge_bases(self) -> List[KnowledgeBase]:
        """Listet alle Knowledge Bases auf"""
        stmt = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())
        result = await self.db.execute(stmt)
        return result.scalars().all()
    
    async def delete_knowledge_base(self, kb_id: int) -> bool:
        """Löscht eine Knowledge Base mit allen Dokumenten"""
        stmt = delete(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0
    
    # ==================== Statistics ====================
    
    async def get_stats(self) -> Dict[str, Any]:
        """Gibt Statistiken über die RAG-Datenbank zurück"""
        from sqlalchemy import func
        
        doc_count = await self.db.scalar(select(func.count(Document.id)))
        chunk_count = await self.db.scalar(select(func.count(DocumentChunk.id)))
        kb_count = await self.db.scalar(select(func.count(KnowledgeBase.id)))
        
        return {
            "document_count": doc_count or 0,
            "chunk_count": chunk_count or 0,
            "knowledge_base_count": kb_count or 0
        }
```

### 8. OllamaService erweitern

Erweitere `src/backend/services/ollama_service.py` um Multi-Modell-Support und Embedding-Funktion:

```python
# Füge diese Methoden zur bestehenden OllamaService Klasse hinzu:

class OllamaService:
    def __init__(self, settings):
        self.client = ollama.AsyncClient(host=settings.ollama_url)
        
        # Multi-Modell Konfiguration
        self.chat_model = settings.ollama_chat_model
        self.rag_model = settings.ollama_rag_model
        self.embed_model = settings.ollama_embed_model
        self.intent_model = settings.ollama_intent_model
    
    async def get_embedding(self, text: str) -> List[float]:
        """Generiert Embedding für Text mit nomic-embed-text"""
        response = await self.client.embeddings(
            model=self.embed_model,
            prompt=text
        )
        return response['embedding']
    
    async def chat_stream_with_rag(
        self, 
        message: str, 
        history: List[dict] = None,
        rag_context: str = None
    ):
        """
        Chat mit optionalem RAG-Kontext
        Nutzt das größere RAG-Modell wenn Kontext vorhanden
        """
        model = self.rag_model if rag_context else self.chat_model
        
        # System-Prompt mit RAG-Kontext
        system_prompt = self._build_rag_system_prompt(rag_context)
        
        messages = [{"role": "system", "content": system_prompt}]
        
        if history:
            messages.extend(history)
        
        messages.append({"role": "user", "content": message})
        
        async for chunk in await self.client.chat(
            model=model,
            messages=messages,
            stream=True
        ):
            if chunk.get('message', {}).get('content'):
                yield chunk['message']['content']
    
    def _build_rag_system_prompt(self, context: str = None) -> str:
        """Erstellt System-Prompt mit RAG-Kontext"""
        base_prompt = """Du bist ein hilfreicher Assistent. 
Beantworte Fragen präzise und freundlich."""
        
        if context:
            return f"""{base_prompt}

Nutze den folgenden Kontext aus der Wissensdatenbank um die Frage zu beantworten.
Wenn der Kontext die Frage nicht beantwortet, sage das ehrlich.
Zitiere relevante Quellen mit [Quelle X].

KONTEXT:
{context}

WICHTIG: Basiere deine Antwort auf dem Kontext. Erfinde keine Informationen."""
        
        return base_prompt
```

### 9. API Routes für Knowledge Management

Erstelle `src/backend/api/routes/knowledge.py`:

```python
"""
Knowledge API Routes
Endpoints für Dokument-Upload, Management und RAG-Suche
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
import aiofiles
import os
from pathlib import Path

from models.database import get_db
from services.rag_service import RAGService
from services.ollama_service import OllamaService
from utils.config import get_settings

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
settings = get_settings()


def get_rag_service(db: AsyncSession = Depends(get_db)) -> RAGService:
    ollama = OllamaService(settings)
    return RAGService(db, ollama)


# ==================== Document Upload ====================

@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    knowledge_base_id: Optional[int] = Query(None),
    rag: RAGService = Depends(get_rag_service)
):
    """
    Lädt ein Dokument hoch und indexiert es für RAG
    
    Unterstützte Formate: PDF, DOCX, TXT, MD, HTML
    """
    # Validierung
    extension = file.filename.split('.')[-1].lower()
    if extension not in settings.allowed_extensions:
        raise HTTPException(
            status_code=400, 
            detail=f"Dateiformat nicht unterstützt. Erlaubt: {settings.allowed_extensions}"
        )
    
    # Dateigröße prüfen
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    
    if size > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail=f"Datei zu groß. Maximum: {settings.max_file_size_mb}MB"
        )
    
    # Datei speichern
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    file_path = upload_dir / file.filename
    
    async with aiofiles.open(file_path, 'wb') as f:
        content = await file.read()
        await f.write(content)
    
    # Dokument verarbeiten und indexieren
    try:
        document = await rag.ingest_document(
            str(file_path), 
            knowledge_base_id
        )
        
        return {
            "id": document.id,
            "filename": document.filename,
            "status": document.status,
            "chunk_count": document.chunk_count,
            "message": "Dokument erfolgreich verarbeitet" if document.status == "completed" else document.error_message
        }
        
    except Exception as e:
        # Datei löschen bei Fehler
        if file_path.exists():
            os.remove(file_path)
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Document Management ====================

@router.get("/documents")
async def list_documents(
    knowledge_base_id: Optional[int] = Query(None),
    rag: RAGService = Depends(get_rag_service)
):
    """Listet alle indexierten Dokumente auf"""
    documents = await rag.list_documents(knowledge_base_id)
    
    return [
        {
            "id": doc.id,
            "filename": doc.filename,
            "title": doc.title,
            "file_type": doc.file_type,
            "status": doc.status,
            "chunk_count": doc.chunk_count,
            "page_count": doc.page_count,
            "created_at": doc.created_at.isoformat(),
            "processed_at": doc.processed_at.isoformat() if doc.processed_at else None
        }
        for doc in documents
    ]


@router.get("/documents/{document_id}")
async def get_document(
    document_id: int,
    rag: RAGService = Depends(get_rag_service)
):
    """Holt Details zu einem Dokument"""
    document = await rag.get_document(document_id)
    
    if not document:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
    
    return {
        "id": document.id,
        "filename": document.filename,
        "title": document.title,
        "author": document.author,
        "file_type": document.file_type,
        "file_size": document.file_size,
        "status": document.status,
        "error_message": document.error_message,
        "chunk_count": document.chunk_count,
        "page_count": document.page_count,
        "created_at": document.created_at.isoformat(),
        "processed_at": document.processed_at.isoformat() if document.processed_at else None
    }


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: int,
    rag: RAGService = Depends(get_rag_service)
):
    """Löscht ein Dokument und alle zugehörigen Chunks"""
    success = await rag.delete_document(document_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
    
    return {"message": "Dokument erfolgreich gelöscht"}


# ==================== Knowledge Base Management ====================

@router.post("/bases")
async def create_knowledge_base(
    name: str,
    description: Optional[str] = None,
    rag: RAGService = Depends(get_rag_service)
):
    """Erstellt eine neue Knowledge Base"""
    kb = await rag.create_knowledge_base(name, description)
    
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "created_at": kb.created_at.isoformat()
    }


@router.get("/bases")
async def list_knowledge_bases(
    rag: RAGService = Depends(get_rag_service)
):
    """Listet alle Knowledge Bases auf"""
    bases = await rag.list_knowledge_bases()
    
    return [
        {
            "id": kb.id,
            "name": kb.name,
            "description": kb.description,
            "is_active": kb.is_active,
            "created_at": kb.created_at.isoformat()
        }
        for kb in bases
    ]


@router.delete("/bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: int,
    rag: RAGService = Depends(get_rag_service)
):
    """Löscht eine Knowledge Base mit allen Dokumenten"""
    success = await rag.delete_knowledge_base(kb_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="Knowledge Base nicht gefunden")
    
    return {"message": "Knowledge Base erfolgreich gelöscht"}


# ==================== RAG Search ====================

@router.post("/search")
async def search_knowledge(
    query: str,
    top_k: int = Query(default=5, ge=1, le=20),
    knowledge_base_id: Optional[int] = Query(None),
    rag: RAGService = Depends(get_rag_service)
):
    """
    Sucht in der Wissensdatenbank
    
    Gibt die relevantesten Chunks für eine Anfrage zurück
    """
    results = await rag.search(query, top_k, knowledge_base_id)
    
    return {
        "query": query,
        "results": results,
        "count": len(results)
    }


# ==================== Statistics ====================

@router.get("/stats")
async def get_knowledge_stats(
    rag: RAGService = Depends(get_rag_service)
):
    """Gibt Statistiken über die Wissensdatenbank zurück"""
    return await rag.get_stats()
```

### 10. WebSocket Integration

Erweitere `src/backend/main.py` um RAG-Support im WebSocket:

```python
# Füge zum bestehenden WebSocket-Handler hinzu:

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    await websocket.accept()
    
    ollama = OllamaService(settings)
    rag_service = RAGService(db, ollama)
    
    try:
        while True:
            data = await websocket.receive_json()
            content = data.get("content", "")
            session_id = data.get("session_id")
            
            # NEU: RAG-Flag aus Message
            use_rag = data.get("use_rag", True)
            knowledge_base_id = data.get("knowledge_base_id")
            
            # Bestehende Intent-Erkennung...
            intent = await ollama.extract_intent(content, room_context=room_context)
            
            # Bestehende Action-Ausführung...
            if intent.get("intent") != "general.conversation":
                action_result = await executor.execute(intent)
            
            # NEU: RAG-Kontext abrufen wenn aktiviert
            rag_context = None
            rag_sources = []
            
            if use_rag:
                rag_results = await rag_service.search(content, knowledge_base_id=knowledge_base_id)
                if rag_results:
                    rag_context = await rag_service.get_context(content, knowledge_base_id=knowledge_base_id)
                    rag_sources = [
                        {
                            "document": r["document"]["filename"],
                            "page": r["chunk"]["page_number"],
                            "similarity": r["similarity"]
                        }
                        for r in rag_results
                    ]
            
            # Response streamen mit RAG-Kontext
            full_response = ""
            async for chunk in ollama.chat_stream_with_rag(
                content,
                history=messages,
                rag_context=rag_context
            ):
                full_response += chunk
                await websocket.send_json({
                    "type": "stream", 
                    "content": chunk
                })
            
            # NEU: Quellen mitsenden
            await websocket.send_json({
                "type": "complete",
                "content": full_response,
                "sources": rag_sources if use_rag else []
            })
            
            # Message speichern...
            
    except WebSocketDisconnect:
        pass
```

### 11. Datenbank-Migration

Erstelle ein Alembic-Migrations-Script oder führe direkt aus:

```sql
-- pgvector Extension aktivieren
CREATE EXTENSION IF NOT EXISTS vector;

-- Neue Tabellen erstellen (werden von SQLAlchemy erstellt)
-- Aber stelle sicher, dass der Vector-Index existiert:

CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding 
ON document_chunks 
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);
```

### 12. Frontend-Komponenten

Erstelle `src/frontend/src/pages/KnowledgePage.jsx`:

```jsx
import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';

const KnowledgePage = () => {
  const [documents, setDocuments] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [stats, setStats] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);

  // Dokumente laden
  const loadDocuments = useCallback(async () => {
    try {
      const response = await axios.get('/api/knowledge/documents');
      setDocuments(response.data);
    } catch (error) {
      console.error('Fehler beim Laden:', error);
    }
  }, []);

  // Statistiken laden
  const loadStats = useCallback(async () => {
    try {
      const response = await axios.get('/api/knowledge/stats');
      setStats(response.data);
    } catch (error) {
      console.error('Fehler beim Laden der Statistiken:', error);
    }
  }, []);

  useEffect(() => {
    loadDocuments();
    loadStats();
  }, [loadDocuments, loadStats]);

  // Datei-Upload
  const handleUpload = async (event) => {
    const file = event.target.files[0];
    if (!file) return;

    setUploading(true);
    const formData = new FormData();
    formData.append('file', file);

    try {
      await axios.post('/api/knowledge/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      });
      loadDocuments();
      loadStats();
    } catch (error) {
      console.error('Upload-Fehler:', error);
      alert(error.response?.data?.detail || 'Upload fehlgeschlagen');
    } finally {
      setUploading(false);
    }
  };

  // Dokument löschen
  const handleDelete = async (id) => {
    if (!confirm('Dokument wirklich löschen?')) return;

    try {
      await axios.delete(`/api/knowledge/documents/${id}`);
      loadDocuments();
      loadStats();
    } catch (error) {
      console.error('Lösch-Fehler:', error);
    }
  };

  // Suche
  const handleSearch = async () => {
    if (!searchQuery.trim()) return;

    try {
      const response = await axios.post('/api/knowledge/search', null, {
        params: { query: searchQuery, top_k: 5 }
      });
      setSearchResults(response.data.results);
    } catch (error) {
      console.error('Such-Fehler:', error);
    }
  };

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Wissensdatenbank</h1>

      {/* Statistiken */}
      {stats && (
        <div className="grid grid-cols-3 gap-4 mb-6">
          <div className="bg-blue-100 p-4 rounded-lg">
            <div className="text-3xl font-bold">{stats.document_count}</div>
            <div className="text-gray-600">Dokumente</div>
          </div>
          <div className="bg-green-100 p-4 rounded-lg">
            <div className="text-3xl font-bold">{stats.chunk_count}</div>
            <div className="text-gray-600">Chunks</div>
          </div>
          <div className="bg-purple-100 p-4 rounded-lg">
            <div className="text-3xl font-bold">{stats.knowledge_base_count}</div>
            <div className="text-gray-600">Knowledge Bases</div>
          </div>
        </div>
      )}

      {/* Upload */}
      <div className="mb-6 p-4 border-2 border-dashed border-gray-300 rounded-lg">
        <input
          type="file"
          onChange={handleUpload}
          accept=".pdf,.docx,.doc,.txt,.md,.html"
          disabled={uploading}
          className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
        />
        {uploading && <p className="mt-2 text-blue-600">Wird verarbeitet...</p>}
      </div>

      {/* Suche */}
      <div className="mb-6 flex gap-2">
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="In Dokumenten suchen..."
          className="flex-1 p-2 border rounded"
          onKeyPress={(e) => e.key === 'Enter' && handleSearch()}
        />
        <button
          onClick={handleSearch}
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          Suchen
        </button>
      </div>

      {/* Suchergebnisse */}
      {searchResults.length > 0 && (
        <div className="mb-6 p-4 bg-gray-50 rounded-lg">
          <h3 className="font-bold mb-2">Suchergebnisse:</h3>
          {searchResults.map((result, idx) => (
            <div key={idx} className="mb-3 p-3 bg-white rounded border">
              <div className="text-sm text-gray-500 mb-1">
                {result.document.filename}
                {result.chunk.page_number && ` • Seite ${result.chunk.page_number}`}
                {` • ${Math.round(result.similarity * 100)}% Relevanz`}
              </div>
              <div className="text-gray-800">{result.chunk.content}</div>
            </div>
          ))}
        </div>
      )}

      {/* Dokumentenliste */}
      <div className="bg-white rounded-lg shadow">
        <table className="w-full">
          <thead className="bg-gray-50">
            <tr>
              <th className="p-3 text-left">Dokument</th>
              <th className="p-3 text-left">Typ</th>
              <th className="p-3 text-left">Status</th>
              <th className="p-3 text-left">Chunks</th>
              <th className="p-3 text-left">Erstellt</th>
              <th className="p-3 text-left">Aktionen</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((doc) => (
              <tr key={doc.id} className="border-t">
                <td className="p-3">{doc.title || doc.filename}</td>
                <td className="p-3">{doc.file_type}</td>
                <td className="p-3">
                  <span className={`px-2 py-1 rounded text-sm ${
                    doc.status === 'completed' ? 'bg-green-100 text-green-800' :
                    doc.status === 'processing' ? 'bg-yellow-100 text-yellow-800' :
                    'bg-red-100 text-red-800'
                  }`}>
                    {doc.status}
                  </span>
                </td>
                <td className="p-3">{doc.chunk_count}</td>
                <td className="p-3">{new Date(doc.created_at).toLocaleDateString()}</td>
                <td className="p-3">
                  <button
                    onClick={() => handleDelete(doc.id)}
                    className="text-red-600 hover:text-red-800"
                  >
                    Löschen
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default KnowledgePage;
```

### 13. ChatPage erweitern

Erweitere `src/frontend/src/pages/ChatPage.jsx`:

```jsx
// Füge zum bestehenden Chat-Interface hinzu:

const [useRag, setUseRag] = useState(true);
const [ragSources, setRagSources] = useState([]);

// Im WebSocket message handler:
const handleMessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.type === 'complete') {
    // Quellen speichern
    if (data.sources && data.sources.length > 0) {
      setRagSources(data.sources);
    }
  }
  // ... rest of handler
};

// Beim Senden:
const sendMessage = () => {
  ws.send(JSON.stringify({
    content: message,
    session_id: sessionId,
    use_rag: useRag,  // NEU
  }));
};

// Im JSX:
<div className="flex items-center gap-2 mb-2">
  <label className="flex items-center gap-2 text-sm">
    <input
      type="checkbox"
      checked={useRag}
      onChange={(e) => setUseRag(e.target.checked)}
      className="rounded"
    />
    Wissensdatenbank nutzen
  </label>
</div>

{/* Quellenanzeige nach Antwort */}
{ragSources.length > 0 && (
  <div className="mt-2 text-xs text-gray-500">
    <span className="font-semibold">Quellen: </span>
    {ragSources.map((src, idx) => (
      <span key={idx}>
        {src.document}
        {src.page && ` (S.${src.page})`}
        {idx < ragSources.length - 1 && ', '}
      </span>
    ))}
  </div>
)}
```

---

## Implementierungsreihenfolge

1. **PostgreSQL auf pgvector umstellen** (`docker-compose.yml`)
2. **Dependencies installieren** (`requirements.txt`)
3. **Konfiguration erweitern** (`utils/config.py`, `.env`)
4. **Datenbank-Modelle erstellen** (`models/database.py`)
5. **Document Processor implementieren** (`services/document_processor.py`)
6. **RAG Service implementieren** (`services/rag_service.py`)
7. **OllamaService erweitern** (`services/ollama_service.py`)
8. **API Routes erstellen** (`api/routes/knowledge.py`)
9. **Routes in main.py registrieren**
10. **WebSocket erweitern** (`main.py`)
11. **Datenbank-Migration ausführen**
12. **Frontend: KnowledgePage erstellen**
13. **Frontend: ChatPage erweitern**
14. **Ollama-Modelle laden** (`nomic-embed-text`, optional `llama3.1:8b`)
15. **Tests schreiben und ausführen**

---

## Wichtige Hinweise

### Lokal bleiben
- Alle Modelle laufen über Ollama lokal
- Keine externen API-Calls für LLM oder Embeddings
- Dokumente werden lokal gespeichert

### Docling
- Docling lädt beim ersten Aufruf Modelle herunter (~1GB)
- Kann CPU-intensiv sein, ggf. GPU nutzen
- Unterstützt: PDF, DOCX, PPTX, XLSX, HTML, MD, TXT, Bilder

### Performance-Tipps
- Für große Dokumente: Background-Task für Verarbeitung
- pgvector Index für schnelle Suche
- Chunk-Size und Overlap anpassen für beste Ergebnisse

### Modelle laden (einmalig)
```bash
docker exec renfield-ollama ollama pull nomic-embed-text
docker exec renfield-ollama ollama pull llama3.1:8b  # Optional für bessere RAG-Antworten
```
