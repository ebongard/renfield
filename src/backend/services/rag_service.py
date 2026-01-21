"""
RAG Service - Retrieval Augmented Generation

Handles document ingestion, embedding generation, similarity search,
and context preparation for LLM queries.
"""
from typing import List, Dict, Any, Optional
from datetime import datetime
import os

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, text
from sqlalchemy.orm import selectinload
from loguru import logger

from models.database import (
    Document, DocumentChunk, KnowledgeBase,
    DOC_STATUS_PENDING, DOC_STATUS_PROCESSING, DOC_STATUS_COMPLETED, DOC_STATUS_FAILED,
    EMBEDDING_DIMENSION
)
from services.document_processor import DocumentProcessor
from utils.config import settings


class RAGService:
    """
    RAG Service für Dokument-basierte Anfragen.

    Bietet:
    - Dokument-Ingestion (Upload, Parsing, Chunking, Embedding)
    - Similarity Search (Vektor-basierte Suche)
    - Kontext-Generierung für LLM-Anfragen
    - Dokument- und Knowledge-Base-Management
    """

    def __init__(self, db: AsyncSession):
        """
        Initialisiert den RAG Service.

        Args:
            db: AsyncSession für Datenbankoperationen
        """
        self.db = db
        self.processor = DocumentProcessor()
        self._ollama_client = None

    async def _get_ollama_client(self):
        """Lazy initialization des Ollama Clients"""
        if self._ollama_client is None:
            import ollama
            self._ollama_client = ollama.AsyncClient(host=settings.ollama_url)
        return self._ollama_client

    # ==========================================================================
    # Embedding Generation
    # ==========================================================================

    async def get_embedding(self, text: str) -> List[float]:
        """
        Generiert Embedding für Text mit Ollama.

        Args:
            text: Text für Embedding

        Returns:
            Liste von Floats (768 Dimensionen für nomic-embed-text)
        """
        try:
            client = await self._get_ollama_client()
            response = await client.embeddings(
                model=settings.ollama_embed_model,
                prompt=text
            )
            # ollama>=0.4.0 uses Pydantic models with .embedding attribute
            return response.embedding
        except Exception as e:
            logger.error(f"Fehler beim Generieren des Embeddings: {e}")
            raise

    # ==========================================================================
    # Document Ingestion
    # ==========================================================================

    async def ingest_document(
        self,
        file_path: str,
        knowledge_base_id: Optional[int] = None,
        filename: Optional[str] = None
    ) -> Document:
        """
        Verarbeitet und indexiert ein Dokument.

        1. Dokument mit Docling parsen
        2. Chunks erstellen
        3. Embeddings generieren
        4. In Datenbank speichern

        Args:
            file_path: Pfad zur Dokumentdatei
            knowledge_base_id: Optional Knowledge Base ID
            filename: Optional Dateiname (falls anders als file_path)

        Returns:
            Document-Objekt mit Status
        """
        actual_filename = filename or os.path.basename(file_path)

        # Document-Eintrag erstellen
        doc = Document(
            file_path=file_path,
            filename=actual_filename,
            knowledge_base_id=knowledge_base_id,
            status=DOC_STATUS_PROCESSING
        )
        self.db.add(doc)
        await self.db.commit()
        await self.db.refresh(doc)

        logger.info(f"Dokument erstellt: ID={doc.id}, Datei={actual_filename}")

        try:
            # 1. Dokument verarbeiten
            result = await self.processor.process_document(file_path)

            if result["status"] == "failed":
                doc.status = DOC_STATUS_FAILED
                doc.error_message = result.get("error", "Unbekannter Fehler")
                await self.db.commit()
                logger.error(f"Dokumentverarbeitung fehlgeschlagen: {doc.error_message}")
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
            chunk_count = 0

            for chunk_data in chunks:
                text = chunk_data["text"]

                # Skip leere Chunks
                if not text or not text.strip():
                    continue

                # Embedding generieren
                try:
                    embedding = await self.get_embedding(text)
                except Exception as e:
                    logger.warning(f"Embedding-Fehler für Chunk {chunk_data['chunk_index']}: {e}")
                    continue

                chunk = DocumentChunk(
                    document_id=doc.id,
                    content=text,
                    embedding=embedding,
                    chunk_index=chunk_data["chunk_index"],
                    page_number=chunk_data["metadata"].get("page_number"),
                    section_title=", ".join(chunk_data["metadata"].get("headings", [])) or None,
                    chunk_type=chunk_data["metadata"].get("chunk_type", "paragraph"),
                    chunk_metadata=chunk_data["metadata"]
                )
                self.db.add(chunk)
                chunk_count += 1

            doc.chunk_count = chunk_count
            doc.status = DOC_STATUS_COMPLETED
            doc.processed_at = datetime.utcnow()

            await self.db.commit()
            await self.db.refresh(doc)

            logger.info(f"Dokument indexiert: ID={doc.id}, Chunks={chunk_count}")
            return doc

        except Exception as e:
            doc.status = DOC_STATUS_FAILED
            doc.error_message = str(e)
            await self.db.commit()
            logger.error(f"Fehler beim Indexieren: {e}")
            raise

    # ==========================================================================
    # Similarity Search
    # ==========================================================================

    async def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        knowledge_base_id: Optional[int] = None,
        similarity_threshold: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Sucht relevante Chunks für eine Anfrage.

        Args:
            query: Suchanfrage
            top_k: Anzahl der Ergebnisse (default: settings.rag_top_k)
            knowledge_base_id: Optional Knowledge Base Filter
            similarity_threshold: Minimum Similarity (default: settings.rag_similarity_threshold)

        Returns:
            Liste von {chunk, document, similarity}
        """
        top_k = top_k or settings.rag_top_k
        threshold = similarity_threshold or settings.rag_similarity_threshold

        # Query-Embedding erstellen
        try:
            query_embedding = await self.get_embedding(query)
        except Exception as e:
            logger.error(f"Fehler beim Query-Embedding: {e}")
            return []

        # Similarity Search mit pgvector
        # Cosine Distance: 0 = identisch, 2 = entgegengesetzt
        # Similarity = 1 - distance (für cosine)

        # Raw SQL für pgvector Operationen
        embedding_str = f"[{','.join(map(str, query_embedding))}]"

        # Build query dynamically to avoid asyncpg type inference issues with NULL
        kb_filter = "AND d.knowledge_base_id = :kb_id" if knowledge_base_id else ""

        sql = text(f"""
            SELECT
                dc.id,
                dc.document_id,
                dc.content,
                dc.chunk_index,
                dc.page_number,
                dc.section_title,
                dc.chunk_type,
                dc.chunk_metadata,
                d.filename,
                d.title as doc_title,
                1 - (dc.embedding <=> CAST(:embedding AS vector)) as similarity
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE d.status = 'completed'
            AND dc.embedding IS NOT NULL
            {kb_filter}
            ORDER BY dc.embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """)

        params = {"embedding": embedding_str, "limit": top_k}
        if knowledge_base_id:
            params["kb_id"] = knowledge_base_id

        result = await self.db.execute(sql, params)
        rows = result.fetchall()

        # Ergebnisse formatieren
        results = []
        for row in rows:
            similarity = float(row.similarity) if row.similarity else 0

            # Threshold-Filter
            if similarity < threshold:
                continue

            results.append({
                "chunk": {
                    "id": row.id,
                    "content": row.content,
                    "chunk_index": row.chunk_index,
                    "page_number": row.page_number,
                    "section_title": row.section_title,
                    "chunk_type": row.chunk_type,
                },
                "document": {
                    "id": row.document_id,
                    "filename": row.filename,
                    "title": row.doc_title or row.filename
                },
                "similarity": round(similarity, 4)
            })

        logger.debug(f"RAG Search: '{query[:50]}...' -> {len(results)} Ergebnisse")
        return results

    async def get_context(
        self,
        query: str,
        top_k: Optional[int] = None,
        knowledge_base_id: Optional[int] = None
    ) -> str:
        """
        Erstellt einen formatierten Kontext-String für das LLM.

        Args:
            query: Suchanfrage
            top_k: Anzahl der Chunks
            knowledge_base_id: Optional Knowledge Base Filter

        Returns:
            Formatierter Kontext-String mit Quellenangaben
        """
        results = await self.search(query, top_k, knowledge_base_id)

        if not results:
            return ""

        context_parts = []
        for i, result in enumerate(results, 1):
            chunk = result["chunk"]
            doc = result["document"]

            # Formatierte Quellenangabe
            source_info = f"[Quelle {i}: {doc['filename']}"
            if chunk["page_number"]:
                source_info += f", Seite {chunk['page_number']}"
            if chunk["section_title"]:
                source_info += f", {chunk['section_title']}"
            source_info += "]"

            context_parts.append(f"{source_info}\n{chunk['content']}")

        return "\n\n---\n\n".join(context_parts)

    # ==========================================================================
    # Document Management
    # ==========================================================================

    async def list_documents(
        self,
        knowledge_base_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Document]:
        """Listet Dokumente auf"""
        stmt = select(Document).order_by(Document.created_at.desc())

        if knowledge_base_id:
            stmt = stmt.where(Document.knowledge_base_id == knowledge_base_id)
        if status:
            stmt = stmt.where(Document.status == status)

        stmt = stmt.limit(limit).offset(offset)

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_document(self, document_id: int) -> Optional[Document]:
        """Holt ein Dokument nach ID"""
        stmt = select(Document).where(Document.id == document_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_document(self, document_id: int) -> bool:
        """Löscht ein Dokument und alle zugehörigen Chunks"""
        # Prüfe ob Dokument existiert
        doc = await self.get_document(document_id)
        if not doc:
            return False

        # Lösche auch die Datei
        try:
            if doc.file_path and os.path.exists(doc.file_path):
                os.remove(doc.file_path)
                logger.info(f"Datei gelöscht: {doc.file_path}")
        except Exception as e:
            logger.warning(f"Konnte Datei nicht löschen: {e}")

        # Lösche aus DB (Chunks werden durch cascade gelöscht)
        stmt = delete(Document).where(Document.id == document_id)
        result = await self.db.execute(stmt)
        await self.db.commit()

        logger.info(f"Dokument gelöscht: ID={document_id}")
        return result.rowcount > 0

    # ==========================================================================
    # Knowledge Base Management
    # ==========================================================================

    async def create_knowledge_base(
        self,
        name: str,
        description: Optional[str] = None
    ) -> KnowledgeBase:
        """Erstellt eine neue Knowledge Base"""
        kb = KnowledgeBase(name=name, description=description)
        self.db.add(kb)
        await self.db.commit()
        await self.db.refresh(kb)
        logger.info(f"Knowledge Base erstellt: ID={kb.id}, Name={name}")
        return kb

    async def list_knowledge_bases(self) -> List[KnowledgeBase]:
        """Listet alle Knowledge Bases auf"""
        stmt = (
            select(KnowledgeBase)
            .options(selectinload(KnowledgeBase.documents))
            .order_by(KnowledgeBase.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_knowledge_base(self, kb_id: int) -> Optional[KnowledgeBase]:
        """Holt eine Knowledge Base nach ID"""
        stmt = (
            select(KnowledgeBase)
            .options(selectinload(KnowledgeBase.documents))
            .where(KnowledgeBase.id == kb_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_knowledge_base(self, kb_id: int) -> bool:
        """Löscht eine Knowledge Base mit allen Dokumenten"""
        kb = await self.get_knowledge_base(kb_id)
        if not kb:
            return False

        # Lösche Dateien aller Dokumente
        for doc in kb.documents:
            try:
                if doc.file_path and os.path.exists(doc.file_path):
                    os.remove(doc.file_path)
            except Exception as e:
                logger.warning(f"Konnte Datei nicht löschen: {e}")

        # Lösche aus DB (Documents + Chunks werden durch cascade gelöscht)
        stmt = delete(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        result = await self.db.execute(stmt)
        await self.db.commit()

        logger.info(f"Knowledge Base gelöscht: ID={kb_id}")
        return result.rowcount > 0

    # ==========================================================================
    # Statistics
    # ==========================================================================

    async def get_stats(self) -> Dict[str, Any]:
        """Gibt Statistiken über die RAG-Datenbank zurück"""
        doc_count = await self.db.scalar(
            select(func.count(Document.id))
        )
        completed_docs = await self.db.scalar(
            select(func.count(Document.id)).where(Document.status == DOC_STATUS_COMPLETED)
        )
        chunk_count = await self.db.scalar(
            select(func.count(DocumentChunk.id))
        )
        kb_count = await self.db.scalar(
            select(func.count(KnowledgeBase.id))
        )

        return {
            "document_count": doc_count or 0,
            "completed_documents": completed_docs or 0,
            "chunk_count": chunk_count or 0,
            "knowledge_base_count": kb_count or 0,
            "embedding_model": settings.ollama_embed_model,
            "embedding_dimension": EMBEDDING_DIMENSION,
        }

    # ==========================================================================
    # Utility Methods
    # ==========================================================================

    async def reindex_document(self, document_id: int) -> Document:
        """
        Re-indexiert ein Dokument (löscht alte Chunks und erstellt neue).
        """
        doc = await self.get_document(document_id)
        if not doc:
            raise ValueError(f"Dokument {document_id} nicht gefunden")

        # Alte Chunks löschen
        stmt = delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        await self.db.execute(stmt)
        await self.db.commit()

        # Neu indexieren
        return await self.ingest_document(
            doc.file_path,
            doc.knowledge_base_id,
            doc.filename
        )

    async def search_by_document(
        self,
        query: str,
        document_id: int,
        top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Sucht nur innerhalb eines bestimmten Dokuments.
        """
        query_embedding = await self.get_embedding(query)
        embedding_str = f"[{','.join(map(str, query_embedding))}]"

        sql = text("""
            SELECT
                dc.id,
                dc.content,
                dc.chunk_index,
                dc.page_number,
                dc.section_title,
                dc.chunk_type,
                1 - (dc.embedding <=> CAST(:embedding AS vector)) as similarity
            FROM document_chunks dc
            WHERE dc.document_id = :doc_id
            AND dc.embedding IS NOT NULL
            ORDER BY dc.embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """)

        result = await self.db.execute(
            sql,
            {
                "embedding": embedding_str,
                "doc_id": document_id,
                "limit": top_k
            }
        )
        rows = result.fetchall()

        return [
            {
                "chunk": {
                    "id": row.id,
                    "content": row.content,
                    "chunk_index": row.chunk_index,
                    "page_number": row.page_number,
                    "section_title": row.section_title,
                    "chunk_type": row.chunk_type,
                },
                "similarity": round(float(row.similarity), 4)
            }
            for row in rows
        ]
