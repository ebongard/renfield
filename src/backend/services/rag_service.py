"""
RAG Service - Retrieval Augmented Generation

Handles document ingestion, embedding generation, similarity search,
and context preparation for LLM queries.
"""
import asyncio
import os
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Keep strong references to fire-and-forget background tasks so they are not
# garbage-collected before they finish (asyncio only holds weak refs).
_background_tasks: set[asyncio.Task] = set()
from sqlalchemy.orm import selectinload

from models.database import (
    DOC_STATUS_COMPLETED,
    DOC_STATUS_FAILED,
    DOC_STATUS_PENDING,
    DOC_STATUS_PROCESSING,
    EMBEDDING_DIMENSION,
    Document,
    DocumentChunk,
    KnowledgeBase,
)
from services.document_processor import DocumentProcessor
from utils.config import settings
from utils.llm_client import get_embed_client

if TYPE_CHECKING:  # pragma: no cover - imports only needed for type hints
    from services.progress import DocumentProgress


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
            self._ollama_client = get_embed_client()
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
            Liste von Floats (Dimensionen je nach Embedding-Modell)

        Raises:
            asyncio.TimeoutError: wenn Ollama nicht innerhalb rag_embedding_timeout antwortet
            Exception: bei Verbindungsproblemen zu Ollama
        """
        try:
            client = await self._get_ollama_client()
            response = await asyncio.wait_for(
                client.embeddings(
                    model=settings.ollama_embed_model,
                    prompt=text
                ),
                timeout=settings.rag_embedding_timeout,
            )
            # ollama>=0.4.0 uses Pydantic models with .embedding attribute
            return response.embedding
        except asyncio.TimeoutError:
            logger.error(f"Embedding-Timeout nach {settings.rag_embedding_timeout}s")
            raise
        except Exception as e:
            logger.error(f"Fehler beim Generieren des Embeddings: {e}")
            raise

    # ==========================================================================
    # Contextual Retrieval (LLM-generated context prefix per chunk)
    # ==========================================================================

    async def _generate_context_prefix(self, chunk_text: str, doc_summary: str) -> str | None:
        """Generate a 1-2 sentence context prefix for a chunk using LLM.

        The prefix describes what the chunk is about and where it comes from,
        so the embedding captures document-level context (Anthropic's Contextual
        Retrieval approach, ~49% fewer retrieval failures).
        """
        if not settings.rag_contextual_retrieval:
            return None

        prompt = (
            "Beschreibe in 1-2 kurzen Sätzen, worum es in diesem Textabschnitt geht "
            "und aus welchem Dokument er stammt. Nur die Beschreibung, keine Einleitung.\n\n"
            f"Dokument: {doc_summary[:500]}\n\n"
            f"Textabschnitt:\n{chunk_text[:800]}\n\n"
            "Kontext:"
        )
        try:
            from services.llm_client import get_chat_client
            client = get_chat_client()
            model = settings.rag_contextual_model or settings.ollama_chat_model
            response = await asyncio.wait_for(
                client.generate(model=model, prompt=prompt, options={"temperature": 0.1, "num_predict": 80}),
                timeout=settings.rag_embedding_timeout,
            )
            prefix = response.response.strip()
            return prefix if prefix else None
        except Exception as e:
            logger.warning(f"Context-Prefix-Generierung fehlgeschlagen: {e}")
            return None

    async def _contextualize_chunks(self, chunks: list[dict], doc_summary: str) -> list[dict]:
        """Add contextual prefixes to chunks for better embedding quality."""
        if not settings.rag_contextual_retrieval:
            return chunks

        sem = asyncio.Semaphore(3)  # Limit concurrent LLM calls

        async def _add_prefix(chunk_data):
            text = chunk_data.get("text", "")
            if not text or not text.strip():
                return chunk_data
            async with sem:
                prefix = await self._generate_context_prefix(text, doc_summary)
            if prefix:
                chunk_data.setdefault("metadata", {})["context_prefix"] = prefix
                chunk_data["text_for_embedding"] = f"{prefix}\n---\n{text}"
            else:
                chunk_data["text_for_embedding"] = text
            return chunk_data

        return await asyncio.gather(*[_add_prefix(cd) for cd in chunks])

    # ==========================================================================
    # Document Ingestion
    # ==========================================================================

    async def create_document_record(
        self,
        file_path: str,
        knowledge_base_id: int | None = None,
        filename: str | None = None,
        file_hash: str | None = None,
    ) -> Document:
        """Insert a ``Document`` row with status=pending and return it.

        Used by the upload route to commit the row inside the HTTP request
        so the client immediately has an id to poll. The actual Docling +
        embedding work runs later, either inline (legacy, via the wrapper
        ``ingest_document``) or asynchronously in the document-worker
        (``process_existing_document``, #388).
        """
        actual_filename = filename or os.path.basename(file_path)
        doc = Document(
            file_path=file_path,
            filename=actual_filename,
            knowledge_base_id=knowledge_base_id,
            file_hash=file_hash,
            status=DOC_STATUS_PENDING,
        )
        self.db.add(doc)
        await self.db.commit()
        await self.db.refresh(doc)
        logger.info(f"Dokument erstellt: ID={doc.id}, Datei={actual_filename}, status=pending")
        return doc

    async def process_existing_document(
        self,
        document_id: int,
        force_ocr: bool = False,
        user_id: int | None = None,
        progress: "DocumentProgress | None" = None,
    ) -> None:
        """Run the ingestion pipeline on an already-persisted Document.

        Transitions the row through pending → processing → completed/failed
        and publishes optional live progress (stage + page counters) to
        Redis via ``DocumentProgress`` for the frontend poll. Returns ``None``
        on both success and a handled Docling failure (row is updated in
        either case); re-raises for unexpected Python exceptions after
        marking the row failed, so the caller can log and the task queue
        can leave the entry un-ACKed for reclaim.
        """
        doc = await self.db.get(Document, document_id)
        if doc is None:
            raise ValueError(f"Document {document_id} not found")

        doc.status = DOC_STATUS_PROCESSING
        await self.db.commit()

        try:
            # Stage 1: parsing — Docling reads the file, OCRs if needed,
            # produces chunks and metadata.
            if progress is not None:
                await progress.set_stage("parsing")
            result = await self.processor.process_document(doc.file_path, force_ocr=force_ocr)

            if result["status"] == "failed":
                doc.status = DOC_STATUS_FAILED
                doc.error_message = result.get("error", "Unbekannter Fehler")
                await self.db.commit()
                logger.error(f"Dokumentverarbeitung fehlgeschlagen: {doc.error_message}")
                return

            # Metadata from the parsed doc.
            metadata = result["metadata"]
            doc.title = metadata.get("title")
            doc.author = metadata.get("author")
            doc.file_type = metadata.get("file_type")
            doc.file_size = metadata.get("file_size")
            doc.page_count = metadata.get("page_count")

            # Stage 2: chunking + contextual-retrieval prefix generation.
            if progress is not None:
                await progress.set_stage("chunking")
            chunks = result["chunks"]
            doc_summary = f"{doc.title or doc.filename}"
            if chunks:
                doc_summary += f" — {chunks[0]['text'][:300]}" if chunks[0].get("text") else ""
            chunks = await self._contextualize_chunks(chunks, doc_summary)

            # Stage 3: embedding generation + DB inserts.
            if progress is not None:
                await progress.set_stage("embedding")
            sem = asyncio.Semaphore(5)
            if settings.rag_parent_child_enabled:
                chunk_objects = await self._ingest_parent_child(doc.id, chunks, sem)
            else:
                chunk_objects = await self._ingest_flat(doc.id, chunks, sem)

            chunk_count = len(chunk_objects)
            if chunk_objects:
                self.db.add_all(chunk_objects)

            doc.chunk_count = chunk_count
            doc.status = DOC_STATUS_COMPLETED
            doc.processed_at = datetime.now(UTC).replace(tzinfo=None)
            await self.db.commit()

            # Populate search_vector for Full-Text Search (bulk update).
            fts_config = settings.rag_hybrid_fts_config
            await self.db.execute(
                text(
                    """
                    UPDATE document_chunks
                    SET search_vector = to_tsvector(:fts_config, content)
                    WHERE document_id = :doc_id
                    AND search_vector IS NULL
                    AND content IS NOT NULL
                    """
                ),
                {"doc_id": doc.id, "fts_config": fts_config},
            )
            await self.db.commit()
            await self.db.refresh(doc)

            # Fire KG extraction hook (fire-and-forget).
            # Skip table/code/formula chunks: Docling flattens table cells into
            # repetitive "field = value. field = value." text that confuses the
            # LLM and produces hallucinated entities. Entity-rich information
            # (names, addresses, organisations) is in text/paragraph chunks.
            _KG_SKIP_TYPES = {"table", "code", "formula"}
            kg_chunks = [
                co.content
                for co in chunk_objects
                if co.content and co.chunk_type not in _KG_SKIP_TYPES
            ]
            if kg_chunks:
                from utils.hooks import run_hooks

                _task = asyncio.create_task(
                    run_hooks(
                        "post_document_ingest",
                        chunks=kg_chunks,
                        document_id=doc.id,
                        user_id=user_id,
                    )
                )
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)

            logger.info(f"Dokument indexiert: ID={doc.id}, Chunks={chunk_count}")

        except Exception as e:
            doc.status = DOC_STATUS_FAILED
            doc.error_message = str(e)
            await self.db.commit()
            logger.error(f"Fehler beim Indexieren: {e}")
            raise

    async def ingest_document(
        self,
        file_path: str,
        knowledge_base_id: int | None = None,
        filename: str | None = None,
        file_hash: str | None = None,
        user_id: int | None = None,
        force_ocr: bool = False,
    ) -> Document:
        """Back-compat wrapper: create the Document row + process inline.

        Used by the legacy upload path (while ``DOCUMENT_WORKER_ENABLED``
        is false), the chat-upload routes, and ``reindex_document``. In
        the worker world these are two separate steps: the upload
        endpoint calls ``create_document_record`` and enqueues; the
        worker calls ``process_existing_document``.

        **Lifecycle note.** The returned Document is identical in shape
        and final state to what the pre-split implementation produced.
        Internally, however, the row now passes through two commits
        (``pending`` → ``processing`` → ``completed``/``failed``)
        instead of one (``processing`` → ``completed``/``failed``).
        External observers polling mid-ingest may briefly see
        ``status=pending`` where they previously would have seen
        ``processing``. This is intentional: the same three-state
        lifecycle serves both inline and worker paths.
        """
        doc = await self.create_document_record(
            file_path=file_path,
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            file_hash=file_hash,
        )
        await self.process_existing_document(
            document_id=doc.id,
            force_ocr=force_ocr,
            user_id=user_id,
        )
        await self.db.refresh(doc)
        return doc

    # --------------------------------------------------------------------------
    # Ingestion Strategies
    # --------------------------------------------------------------------------

    async def _ingest_flat(self, doc_id: int, chunks: list[dict], sem: asyncio.Semaphore) -> list[DocumentChunk]:
        """Original flat chunking: each chunk gets an embedding."""

        async def _embed_chunk(chunk_data):
            text_content = chunk_data["text"]
            if not text_content or not text_content.strip():
                return None
            # Use contextualized text for embedding if available
            embed_text = chunk_data.get("text_for_embedding", text_content)
            async with sem:
                try:
                    embedding = await self.get_embedding(embed_text)
                except Exception as e:
                    logger.warning(f"Embedding-Fehler für Chunk {chunk_data['chunk_index']}: {e}")
                    return None
            return DocumentChunk(
                document_id=doc_id,
                content=text_content,  # Store original text for display
                embedding=embedding,
                chunk_index=chunk_data["chunk_index"],
                page_number=chunk_data["metadata"].get("page_number"),
                section_title=", ".join(chunk_data["metadata"].get("headings", [])) or None,
                chunk_type=chunk_data["metadata"].get("chunk_type", "paragraph"),
                chunk_metadata=chunk_data["metadata"],
            )

        results = await asyncio.gather(*[_embed_chunk(cd) for cd in chunks])
        return [r for r in results if r is not None]

    async def _ingest_parent_child(self, doc_id: int, chunks: list[dict], sem: asyncio.Semaphore) -> list[DocumentChunk]:
        """Parent-child chunking: small embedded children reference larger context parents."""
        # Group consecutive child chunks into parents
        children_per_parent = max(1, settings.rag_parent_chunk_size // max(settings.rag_child_chunk_size, 1))
        all_objects: list[DocumentChunk] = []

        for group_start in range(0, len(chunks), children_per_parent):
            group = chunks[group_start:group_start + children_per_parent]
            if not group:
                continue

            # Create parent chunk (concatenated text, no embedding)
            parent_text = "\n\n".join(c["text"] for c in group if c["text"] and c["text"].strip())
            if not parent_text.strip():
                continue

            first_meta = group[0]["metadata"]
            parent = DocumentChunk(
                document_id=doc_id,
                content=parent_text,
                embedding=None,  # Parents are not embedded
                chunk_index=group_start,
                page_number=first_meta.get("page_number"),
                section_title=", ".join(first_meta.get("headings", [])) or None,
                chunk_type="parent",
                chunk_metadata={"child_count": len(group)},
            )
            self.db.add(parent)
            await self.db.flush()  # Get parent.id for children

            # Create child chunks with embeddings
            async def _embed_child(chunk_data, parent_id):
                text_content = chunk_data["text"]
                if not text_content or not text_content.strip():
                    return None
                embed_text = chunk_data.get("text_for_embedding", text_content)
                async with sem:
                    try:
                        embedding = await self.get_embedding(embed_text)
                    except Exception as e:
                        logger.warning(f"Embedding-Fehler für Child-Chunk {chunk_data['chunk_index']}: {e}")
                        return None
                return DocumentChunk(
                    document_id=doc_id,
                    content=text_content,  # Store original text for display
                    embedding=embedding,
                    parent_chunk_id=parent_id,
                    chunk_index=chunk_data["chunk_index"],
                    page_number=chunk_data["metadata"].get("page_number"),
                    section_title=", ".join(chunk_data["metadata"].get("headings", [])) or None,
                    chunk_type=chunk_data["metadata"].get("chunk_type", "paragraph"),
                    chunk_metadata=chunk_data["metadata"],
                )

            child_results = await asyncio.gather(*[_embed_child(cd, parent.id) for cd in group])
            children = [r for r in child_results if r is not None]

            all_objects.append(parent)
            all_objects.extend(children)

        return all_objects

    # --------------------------------------------------------------------------
    # Parent Resolution (for parent-child search)
    # --------------------------------------------------------------------------

    async def _resolve_parents(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace child chunk content with parent content, deduplicate by parent."""
        if not results:
            return results

        # Collect parent IDs from child results
        child_parent_map = {}
        for r in results:
            pid = r["chunk"].get("parent_chunk_id")
            if pid:
                child_parent_map.setdefault(pid, []).append(r)

        if not child_parent_map:
            return results  # No parent-child chunks, return as-is

        # Fetch all parents in one query
        parent_ids = list(child_parent_map.keys())
        stmt = text("""
            SELECT id, content, page_number, section_title
            FROM document_chunks
            WHERE id = ANY(:parent_ids)
        """)
        rows = (await self.db.execute(stmt, {"parent_ids": parent_ids})).fetchall()
        parents = {row.id: row for row in rows}

        # Deduplicate: keep highest-scoring child per parent
        resolved = []
        seen_parents = set()
        for r in results:
            pid = r["chunk"].get("parent_chunk_id")
            if pid and pid in parents:
                if pid in seen_parents:
                    continue  # Skip duplicate parent
                seen_parents.add(pid)
                parent = parents[pid]
                r["chunk"]["content"] = parent.content
                r["chunk"]["page_number"] = parent.page_number
                r["chunk"]["section_title"] = parent.section_title
                r["chunk"]["chunk_type"] = "parent"
            resolved.append(r)

        return resolved

    # --------------------------------------------------------------------------
    # Reranking (dedicated model scores query-chunk pairs)
    # --------------------------------------------------------------------------

    async def _rerank(self, query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rerank results using a dedicated reranker model via Ollama embeddings.

        Computes query and chunk embeddings with the reranker model, then scores
        by cosine similarity. This provides a second-pass relevance check with
        a different model than the storage embeddings.
        """
        rerank_top_k = settings.rag_rerank_top_k
        if not settings.rag_rerank_enabled or not results:
            return results[:rerank_top_k]

        try:
            client = await self._get_ollama_client()
            model = settings.rag_rerank_model

            # Get query embedding from reranker model
            q_resp = await asyncio.wait_for(
                client.embeddings(model=model, prompt=query),
                timeout=settings.rag_embedding_timeout,
            )
            q_emb = q_resp.embedding

            # Score each candidate chunk
            scored = []
            sem = asyncio.Semaphore(5)

            async def _score(r):
                content = r["chunk"]["content"][:1000]  # Cap for speed
                async with sem:
                    c_resp = await asyncio.wait_for(
                        client.embeddings(model=model, prompt=content),
                        timeout=settings.rag_embedding_timeout,
                    )
                # Cosine similarity
                c_emb = c_resp.embedding
                dot = sum(a * b for a, b in zip(q_emb, c_emb))
                norm_q = sum(a * a for a in q_emb) ** 0.5
                norm_c = sum(a * a for a in c_emb) ** 0.5
                sim = dot / (norm_q * norm_c) if norm_q and norm_c else 0
                return (sim, r)

            scored = await asyncio.gather(*[_score(r) for r in results])
            scored.sort(key=lambda x: x[0], reverse=True)

            reranked = [r for _, r in scored[:rerank_top_k]]
            logger.info(
                f"📚 RAG Reranking: model={model}, input={len(results)}, "
                f"output={len(reranked)}, top_score={scored[0][0]:.4f}"
            )
            return reranked

        except Exception as e:
            logger.warning(f"Reranking fehlgeschlagen, verwende Original-Reihenfolge: {e}")
            return results[:rerank_top_k]

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

        # Query-Embedding erstellen (mit BM25-Fallback bei Fehler)
        try:
            query_embedding = await self.get_embedding(query)
        except Exception as e:
            logger.warning(f"Embedding fehlgeschlagen, Fallback auf BM25-only: {e}")
            query_embedding = None

        if query_embedding is None:
            # BM25-only Fallback wenn Embedding-Modell nicht erreichbar
            results = await self._search_bm25(query, top_k, knowledge_base_id)
            logger.info(
                f"📚 RAG BM25-only Fallback: query='{query[:50]}', kb_id={knowledge_base_id}, "
                f"found={len(results)}"
            )
        elif settings.rag_hybrid_enabled:
            # Hybrid Search (Dense + BM25 via RRF)
            candidate_k = top_k * 3  # Over-fetch for RRF fusion
            dense_results = await self._search_dense(
                query_embedding, candidate_k, knowledge_base_id, threshold
            )
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

        # Reranking (second-pass relevance scoring with dedicated model)
        if results:
            results = await self._rerank(query, results)

        # Parent-Child Resolution OR Context Window Expansion (mutually exclusive)
        if settings.rag_parent_child_enabled and results:
            results = await self._resolve_parents(results)
        else:
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
            threshold: Minimum cosine similarity filter

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
                dc.parent_chunk_id,
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
                    "parent_chunk_id": getattr(row, "parent_chunk_id", None),
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

        Uses websearch_to_tsquery with OR matching so any query term can match.
        ts_rank_cd (Cover Density Ranking) ranks results by term coverage,
        so chunks containing more query terms rank higher.

        Args:
            query: Natural language search query
            top_k: Number of results
            knowledge_base_id: Optional KB filter

        Returns:
            List of {chunk, document, similarity} where similarity is ts_rank_cd score
        """
        fts_config = settings.rag_hybrid_fts_config
        kb_filter = "AND d.knowledge_base_id = :kb_id" if knowledge_base_id else ""

        # OR-match: any query term can match; ts_rank_cd ranks by coverage
        or_query = " OR ".join(query.split())

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
                dc.parent_chunk_id,
                d.filename,
                d.title as doc_title,
                ts_rank_cd(dc.search_vector, websearch_to_tsquery(:fts_config, :or_query)) as rank
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE d.status = 'completed'
            AND dc.search_vector IS NOT NULL
            AND dc.search_vector @@ websearch_to_tsquery(:fts_config, :or_query)
            {kb_filter}
            ORDER BY rank DESC
            LIMIT :limit
        """)

        params = {"or_query": or_query, "fts_config": fts_config, "limit": top_k}
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
                    "parent_chunk_id": getattr(row, "parent_chunk_id", None),
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

        Uses a single batched SQL query instead of one query per result.

        Args:
            results: Search results to expand
            window_size: Number of adjacent chunks per direction

        Returns:
            Results with expanded content
        """
        if not results:
            return results

        # Collect ranges for batch query
        ranges = []
        for result in results:
            doc_id = result["document"]["id"]
            center_index = result["chunk"]["chunk_index"]
            min_index = max(0, center_index - window_size)
            max_index = center_index + window_size
            ranges.append((doc_id, min_index, max_index))

        # Build single batch query with OR conditions
        conditions = []
        params = {}
        for i, (doc_id, min_idx, max_idx) in enumerate(ranges):
            conditions.append(
                f"(document_id = :doc_{i} AND chunk_index >= :min_{i} AND chunk_index <= :max_{i})"
            )
            params[f"doc_{i}"] = doc_id
            params[f"min_{i}"] = min_idx
            params[f"max_{i}"] = max_idx

        sql = text(f"""
            SELECT id, content, chunk_index, page_number, section_title, chunk_type, document_id
            FROM document_chunks
            WHERE {" OR ".join(conditions)}
            ORDER BY document_id, chunk_index ASC
        """)

        batch_result = await self.db.execute(sql, params)
        all_rows = batch_result.fetchall()

        # Group by document_id
        rows_by_doc = defaultdict(list)
        for row in all_rows:
            rows_by_doc[row.document_id].append(row)

        # Process results using grouped data
        expanded = []
        seen_chunks = set()

        for result in results:
            chunk_id = result["chunk"]["id"]
            if chunk_id in seen_chunks:
                continue

            doc_id = result["document"]["id"]
            center_index = result["chunk"]["chunk_index"]
            min_index = max(0, center_index - window_size)
            max_index = center_index + window_size
            adjacent_rows = rows_by_doc.get(doc_id, [])

            merged_content_parts = []
            for row in adjacent_rows:
                if min_index <= row.chunk_index <= max_index:
                    if row.content:
                        merged_content_parts.append(row.content)
                    seen_chunks.add(row.id)

            merged_content = "\n\n".join(merged_content_parts) if merged_content_parts else result["chunk"]["content"]

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

        # Lösche FK-Referenzen aus chat_uploads
        await self.db.execute(
            text("UPDATE chat_uploads SET document_id = NULL WHERE document_id = :doc_id"),
            {"doc_id": document_id}
        )

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

    async def move_documents(
        self,
        document_ids: list[int],
        target_kb_id: int
    ) -> int:
        """
        Verschiebt Dokumente in eine andere Knowledge Base.

        Args:
            document_ids: Liste der Dokument-IDs
            target_kb_id: Ziel-Knowledge-Base-ID

        Returns:
            Anzahl tatsächlich verschobener Dokumente

        Raises:
            ValueError: wenn Ziel-KB nicht existiert oder inaktiv ist
        """
        # Validiere Ziel-KB
        target_kb = await self.get_knowledge_base(target_kb_id)
        if not target_kb:
            raise ValueError(f"Knowledge Base {target_kb_id} nicht gefunden")
        if not target_kb.is_active:
            raise ValueError(f"Knowledge Base '{target_kb.name}' ist nicht aktiv")

        # Lade alle Dokumente
        stmt = select(Document).where(Document.id.in_(document_ids))
        result = await self.db.execute(stmt)
        docs = list(result.scalars().all())

        if not docs:
            raise ValueError("Keine der angegebenen Dokumente gefunden")

        # Verschiebe nur Dokumente die nicht bereits in der Ziel-KB sind
        moved = 0
        for doc in docs:
            if doc.knowledge_base_id != target_kb_id:
                doc.knowledge_base_id = target_kb_id
                moved += 1

        if moved > 0:
            await self.db.commit()
            logger.info(
                f"📦 {moved} Dokument(e) nach KB '{target_kb.name}' (ID={target_kb_id}) verschoben"
            )

        return moved

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
