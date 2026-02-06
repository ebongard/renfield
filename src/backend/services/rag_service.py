"""
RAG Service - Retrieval Augmented Generation

Handles document ingestion, embedding generation, similarity search,
and context preparation for LLM queries.
"""
import os
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import (
    DOC_STATUS_COMPLETED,
    DOC_STATUS_FAILED,
    DOC_STATUS_PROCESSING,
    EMBEDDING_DIMENSION,
    Document,
    DocumentChunk,
    KnowledgeBase,
)
from services.document_processor import DocumentProcessor
from utils.config import settings
from utils.llm_client import get_default_client


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
            self._ollama_client = get_default_client()
        return self._ollama_client

    # ==========================================================================
    # Embedding Generation
    # ==========================================================================

    async def get_embedding(self, text: str) -> list[float]:
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
        knowledge_base_id: int | None = None,
        filename: str | None = None,
        file_hash: str | None = None
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
            file_hash: Optional SHA256 hash for duplicate detection

        Returns:
            Document-Objekt mit Status
        """
        actual_filename = filename or os.path.basename(file_path)

        # Document-Eintrag erstellen
        doc = Document(
            file_path=file_path,
            filename=actual_filename,
            knowledge_base_id=knowledge_base_id,
            file_hash=file_hash,
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

            # 3. Chunks mit Embeddings erstellen (batch insert)
            chunks = result["chunks"]
            chunk_objects = []

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

                chunk_objects.append(DocumentChunk(
                    document_id=doc.id,
                    content=text,
                    embedding=embedding,
                    chunk_index=chunk_data["chunk_index"],
                    page_number=chunk_data["metadata"].get("page_number"),
                    section_title=", ".join(chunk_data["metadata"].get("headings", [])) or None,
                    chunk_type=chunk_data["metadata"].get("chunk_type", "paragraph"),
                    chunk_metadata=chunk_data["metadata"]
                ))

            chunk_count = len(chunk_objects)
            if chunk_objects:
                self.db.add_all(chunk_objects)

            doc.chunk_count = chunk_count
            doc.status = DOC_STATUS_COMPLETED
            doc.processed_at = datetime.utcnow()

            await self.db.commit()

            # Populate search_vector for Full-Text Search (bulk update)
            fts_config = settings.rag_hybrid_fts_config
            await self.db.execute(
                text("""
                    UPDATE document_chunks
                    SET search_vector = to_tsvector(:fts_config, content)
                    WHERE document_id = :doc_id
                    AND search_vector IS NULL
                    AND content IS NOT NULL
                """),
                {"doc_id": doc.id, "fts_config": fts_config}
            )
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
        top_k: int | None = None,
        knowledge_base_id: int | None = None,
        similarity_threshold: float | None = None
    ) -> list[dict[str, Any]]:
        """
        Sucht relevante Chunks für eine Anfrage.

        Uses Hybrid Search (Dense + BM25 via RRF) when enabled,
        otherwise falls back to dense-only search.
        Optionally expands results with adjacent chunks (Context Window).

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

        # Hybrid Search or Dense-only
        if settings.rag_hybrid_enabled:
            candidate_k = top_k * 3  # Over-fetch for RRF fusion
            dense_results = await self._search_dense(query_embedding, candidate_k, knowledge_base_id)
            bm25_results = await self._search_bm25(query, candidate_k, knowledge_base_id)
            results = self._reciprocal_rank_fusion(dense_results, bm25_results, top_k)
            logger.info(
                f"📚 RAG Hybrid Search: query='{query[:50]}', kb_id={knowledge_base_id}, "
                f"dense={len(dense_results)}, bm25={len(bm25_results)}, fused={len(results)}"
            )
        else:
            results = await self._search_dense(query_embedding, top_k, knowledge_base_id, threshold)
            logger.info(
                f"📚 RAG Dense Search: query='{query[:50]}', kb_id={knowledge_base_id}, "
                f"threshold={threshold}, found={len(results)}"
            )

        # Context Window Expansion
        window_size = min(settings.rag_context_window, settings.rag_context_window_max)
        if window_size > 0 and results:
            results = await self._expand_context_window(results, window_size)

        return results

    # --------------------------------------------------------------------------
    # Dense Search (pgvector cosine similarity)
    # --------------------------------------------------------------------------

    async def _search_dense(
        self,
        query_embedding: list[float],
        top_k: int,
        knowledge_base_id: int | None = None,
        threshold: float | None = None
    ) -> list[dict[str, Any]]:
        """
        Dense vector search using pgvector cosine distance.

        Args:
            query_embedding: Pre-computed query embedding
            top_k: Number of results
            knowledge_base_id: Optional KB filter
            threshold: Minimum cosine similarity (only applied in non-hybrid mode)

        Returns:
            List of {chunk, document, similarity}
        """
        embedding_str = f"[{','.join(map(str, query_embedding))}]"
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

        results = []
        for row in rows:
            similarity = float(row.similarity) if row.similarity else 0

            if threshold and similarity < threshold:
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

        return results

    # --------------------------------------------------------------------------
    # BM25 Search (PostgreSQL Full-Text Search)
    # --------------------------------------------------------------------------

    async def _search_bm25(
        self,
        query: str,
        top_k: int,
        knowledge_base_id: int | None = None
    ) -> list[dict[str, Any]]:
        """
        BM25-style search using PostgreSQL Full-Text Search.

        Uses plainto_tsquery for natural language input and ts_rank_cd
        (Cover Density Ranking) which is better for shorter text segments.

        Args:
            query: Natural language search query
            top_k: Number of results
            knowledge_base_id: Optional KB filter

        Returns:
            List of {chunk, document, similarity} where similarity is ts_rank_cd score
        """
        fts_config = settings.rag_hybrid_fts_config
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
                ts_rank_cd(dc.search_vector, plainto_tsquery(:fts_config, :query)) as rank
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE d.status = 'completed'
            AND dc.search_vector IS NOT NULL
            AND dc.search_vector @@ plainto_tsquery(:fts_config, :query)
            {kb_filter}
            ORDER BY rank DESC
            LIMIT :limit
        """)

        params = {"query": query, "fts_config": fts_config, "limit": top_k}
        if knowledge_base_id:
            params["kb_id"] = knowledge_base_id

        result = await self.db.execute(sql, params)
        rows = result.fetchall()

        results = []
        for row in rows:
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
                "similarity": round(float(row.rank), 6)
            })

        return results

    # --------------------------------------------------------------------------
    # Reciprocal Rank Fusion (RRF)
    # --------------------------------------------------------------------------

    @staticmethod
    def _reciprocal_rank_fusion(
        dense_results: list[dict[str, Any]],
        bm25_results: list[dict[str, Any]],
        top_k: int
    ) -> list[dict[str, Any]]:
        """
        Combines dense and BM25 results using Reciprocal Rank Fusion.

        RRF score = sum(weight / (k + rank)) for each retriever.
        Rank-based (not score-based), robust to different score scales.

        Args:
            dense_results: Results from dense vector search
            bm25_results: Results from BM25 full-text search
            top_k: Number of final results

        Returns:
            Fused and re-ranked results
        """
        k = settings.rag_hybrid_rrf_k
        dense_weight = settings.rag_hybrid_dense_weight
        bm25_weight = settings.rag_hybrid_bm25_weight

        # Collect scores by chunk ID
        scores: dict[int, float] = {}
        chunk_data: dict[int, dict[str, Any]] = {}

        for rank, result in enumerate(dense_results):
            chunk_id = result["chunk"]["id"]
            scores[chunk_id] = scores.get(chunk_id, 0) + dense_weight / (k + rank + 1)
            chunk_data[chunk_id] = result

        for rank, result in enumerate(bm25_results):
            chunk_id = result["chunk"]["id"]
            scores[chunk_id] = scores.get(chunk_id, 0) + bm25_weight / (k + rank + 1)
            if chunk_id not in chunk_data:
                chunk_data[chunk_id] = result

        # Sort by fused score (descending) and take top_k
        sorted_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)[:top_k]

        results = []
        for chunk_id in sorted_ids:
            entry = chunk_data[chunk_id].copy()
            entry["similarity"] = round(scores[chunk_id], 6)
            results.append(entry)

        return results

    # --------------------------------------------------------------------------
    # Context Window Expansion
    # --------------------------------------------------------------------------

    async def _expand_context_window(
        self,
        results: list[dict[str, Any]],
        window_size: int
    ) -> list[dict[str, Any]]:
        """
        Expands each result with adjacent chunks from the same document.

        For each hit chunk with chunk_index=N, fetches chunks N-window..N+window
        and merges their content. Deduplicates when adjacent chunks are both hits.

        Args:
            results: Search results to expand
            window_size: Number of adjacent chunks per direction

        Returns:
            Results with expanded content
        """
        if not results:
            return results

        # Collect (document_id, chunk_index) pairs for all results
        hit_keys = set()
        for r in results:
            hit_keys.add((r["document"]["id"], r["chunk"]["chunk_index"]))

        expanded = []
        seen_chunks = set()  # Track already-included chunk IDs to avoid duplicates

        for result in results:
            chunk_id = result["chunk"]["id"]
            if chunk_id in seen_chunks:
                continue

            doc_id = result["document"]["id"]
            center_index = result["chunk"]["chunk_index"]
            min_index = max(0, center_index - window_size)
            max_index = center_index + window_size

            # Fetch adjacent chunks
            sql = text("""
                SELECT id, content, chunk_index, page_number, section_title, chunk_type
                FROM document_chunks
                WHERE document_id = :doc_id
                AND chunk_index >= :min_idx
                AND chunk_index <= :max_idx
                ORDER BY chunk_index ASC
            """)

            adj_result = await self.db.execute(sql, {
                "doc_id": doc_id,
                "min_idx": min_index,
                "max_idx": max_index
            })
            adjacent_rows = adj_result.fetchall()

            # Merge content from adjacent chunks
            merged_content_parts = []
            for row in adjacent_rows:
                if row.content:
                    merged_content_parts.append(row.content)
                # Mark all included chunks as seen
                seen_chunks.add(row.id)

            merged_content = "\n\n".join(merged_content_parts) if merged_content_parts else result["chunk"]["content"]

            # Build expanded result (preserve original metadata)
            expanded_result = {
                "chunk": {
                    "id": result["chunk"]["id"],
                    "content": merged_content,
                    "chunk_index": result["chunk"]["chunk_index"],
                    "page_number": result["chunk"]["page_number"],
                    "section_title": result["chunk"]["section_title"],
                    "chunk_type": result["chunk"]["chunk_type"],
                },
                "document": result["document"],
                "similarity": result["similarity"]
            }
            expanded.append(expanded_result)

        return expanded

    async def get_context(
        self,
        query: str,
        top_k: int | None = None,
        knowledge_base_id: int | None = None
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

    def format_context_from_results(self, results: list[dict]) -> str:
        """Format pre-fetched search results into context string without re-searching."""
        if not results:
            return ""
        context_parts = []
        for i, result in enumerate(results, 1):
            chunk = result["chunk"]
            doc = result["document"]
            source_info = f"[Quelle {i}: {doc['filename']}"
            if chunk.get("page_number"):
                source_info += f", Seite {chunk['page_number']}"
            if chunk.get("section_title"):
                source_info += f", {chunk['section_title']}"
            source_info += "]"
            context_parts.append(f"{source_info}\n{chunk['content']}")
        return "\n\n---\n\n".join(context_parts)

    # ==========================================================================
    # Document Management
    # ==========================================================================

    async def list_documents(
        self,
        knowledge_base_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[Document]:
        """Listet Dokumente auf"""
        stmt = select(Document).order_by(Document.created_at.desc())

        if knowledge_base_id:
            stmt = stmt.where(Document.knowledge_base_id == knowledge_base_id)
        if status:
            stmt = stmt.where(Document.status == status)

        stmt = stmt.limit(limit).offset(offset)

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_document(self, document_id: int) -> Document | None:
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

        # Lösche zuerst die Chunks (explizit, falls CASCADE nicht greift)
        chunk_stmt = delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        await self.db.execute(chunk_stmt)

        # Dann lösche das Dokument
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
        description: str | None = None
    ) -> KnowledgeBase:
        """Erstellt eine neue Knowledge Base"""
        kb = KnowledgeBase(name=name, description=description)
        self.db.add(kb)
        await self.db.commit()
        await self.db.refresh(kb)
        logger.info(f"Knowledge Base erstellt: ID={kb.id}, Name={name}")
        return kb

    async def list_knowledge_bases(self) -> list[KnowledgeBase]:
        """Listet alle Knowledge Bases auf (without eager-loading documents)"""
        # Use a count subquery instead of selectinload to avoid loading all documents
        doc_count_subq = (
            select(func.count(Document.id))
            .where(Document.knowledge_base_id == KnowledgeBase.id)
            .correlate(KnowledgeBase)
            .scalar_subquery()
            .label("document_count")
        )
        stmt = (
            select(KnowledgeBase, doc_count_subq)
            .order_by(KnowledgeBase.created_at.desc())
        )
        result = await self.db.execute(stmt)
        rows = result.all()

        # Attach document_count as a transient attribute
        kbs = []
        for kb, doc_count in rows:
            kb._document_count = doc_count
            kbs.append(kb)
        return kbs

    async def get_knowledge_base(self, kb_id: int) -> KnowledgeBase | None:
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

    async def get_stats(self) -> dict[str, Any]:
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

    async def reindex_fts(self) -> dict[str, Any]:
        """
        Re-populates search_vector for all document chunks.

        Useful after changing the FTS config (e.g. simple → german)
        or for backfilling after migration.

        Returns:
            Dict with updated_count
        """
        fts_config = settings.rag_hybrid_fts_config
        result = await self.db.execute(
            text("""
                UPDATE document_chunks
                SET search_vector = to_tsvector(:fts_config, content)
                WHERE content IS NOT NULL
            """),
            {"fts_config": fts_config}
        )
        await self.db.commit()
        updated = result.rowcount
        logger.info(f"🔄 FTS Reindex: updated {updated} chunks with config '{fts_config}'")
        return {"updated_count": updated, "fts_config": fts_config}

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
    ) -> list[dict[str, Any]]:
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
