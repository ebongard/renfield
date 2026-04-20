"""
RAG Retrieval — extracted from rag_service.py for circles v1.

Holds ALL retrieval-side concerns (search, rerank, parent-child resolution,
context-window expansion, formatting). Document ingestion + KB management
remain in rag_service.RAGService.

ASCII data flow:

    query (str)
       │
       ▼
    get_embedding(query) ────────────┐
       │                              │ (on embedding failure)
       ▼                              │
    settings.rag_hybrid_enabled?      ▼
       ├── True  → _search_dense + _search_bm25 → _reciprocal_rank_fusion
       │
       └── False → _search_dense only
                                      │
                                      ▼
                               _search_bm25 only ◄────── BM25 fallback
                                      │
                                      ▼
                               results (top_k)
                                      │
                          ┌───────────┼─────────────────┐
                          ▼           ▼                 ▼
                       _rerank   _resolve_parents  _expand_context_window
                       (if enabled)  (if parent-child)  (else, if window>0)
                                      │
                                      ▼
                               final results

Behavioral parity with the legacy RAGService inline retrieval is mandatory
(see tests/backend/test_rag_retrieval_extract.py for the regression suite).

Lane A1 of the second-brain-circles eng-review plan. After regression-stable,
Lane A2 (kg_retrieval.py) and Lane A3 (memory_retrieval.py) follow the same
pattern. Once all three land + the CIRCLES_USE_NEW_RAG flag is removed, these
modules become the natural backing for AtomService.PolymorphicAtomStore (v1
schema work, Lane B).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from utils.config import settings
from utils.llm_client import get_embed_client


class RAGRetrieval:
    """
    Stateless-ish retrieval service over a single AsyncSession.

    Same dependency shape as RAGService but scoped to read-side only:
      - db: AsyncSession (queries; never writes)
      - lazy ollama embed client (for query embeddings + reranking)

    Public surface (mirrors the methods that were in RAGService):
      - get_embedding(text) -> list[float]
      - search(query, top_k?, knowledge_base_id?, similarity_threshold?) -> list[dict]
      - get_context(query, top_k?, knowledge_base_id?) -> str
      - format_context_from_results(results) -> str
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self._ollama_client = None

    async def _get_ollama_client(self):
        """Lazy initialization of the embedding/reranker Ollama client."""
        if self._ollama_client is None:
            self._ollama_client = get_embed_client()
        return self._ollama_client

    # ==========================================================================
    # Embedding (query-side; ingestion-side lives in RAGService)
    # ==========================================================================

    async def get_embedding(self, text: str) -> list[float]:
        """Generate an embedding for the given query text."""
        client = await self._get_ollama_client()
        response = await asyncio.wait_for(
            client.embeddings(model=settings.ollama_embed_model, prompt=text),
            timeout=settings.rag_embedding_timeout,
        )
        return response.embedding

    # ==========================================================================
    # Main entry point
    # ==========================================================================

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        knowledge_base_id: int | None = None,
        similarity_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search relevant chunks for a query.

        Hybrid (dense + BM25 via RRF) by default; falls back to BM25-only
        when embedding generation fails. Optional rerank pass + either
        parent-child resolution or context-window expansion.

        Returns: list of {chunk, document, similarity}
        """
        top_k = top_k or settings.rag_top_k
        threshold = similarity_threshold or settings.rag_similarity_threshold

        try:
            query_embedding = await self.get_embedding(query)
        except Exception as e:
            logger.warning(f"Embedding fehlgeschlagen, Fallback auf BM25-only: {e}")
            query_embedding = None

        if query_embedding is None:
            results = await self._search_bm25(query, top_k, knowledge_base_id)
            logger.info(
                f"📚 RAG BM25-only Fallback: query='{query[:50]}', kb_id={knowledge_base_id}, "
                f"found={len(results)}"
            )
        elif settings.rag_hybrid_enabled:
            candidate_k = top_k * 3
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

        if results:
            results = await self._rerank(query, results)

        if settings.rag_parent_child_enabled and results:
            results = await self._resolve_parents(results)
        else:
            window_size = min(settings.rag_context_window, settings.rag_context_window_max)
            if window_size > 0 and results:
                results = await self._expand_context_window(results, window_size)

        return results

    # ==========================================================================
    # Dense vector search (pgvector cosine distance)
    # ==========================================================================

    async def _search_dense(
        self,
        query_embedding: list[float],
        top_k: int,
        knowledge_base_id: int | None = None,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
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

        params: dict[str, Any] = {"embedding": embedding_str, "limit": top_k}
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
                    "title": row.doc_title or row.filename,
                },
                "similarity": round(similarity, 4),
            })
        return results

    # ==========================================================================
    # BM25 search (PostgreSQL Full-Text Search)
    # ==========================================================================

    async def _search_bm25(
        self,
        query: str,
        top_k: int,
        knowledge_base_id: int | None = None,
    ) -> list[dict[str, Any]]:
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

        params: dict[str, Any] = {"or_query": or_query, "fts_config": fts_config, "limit": top_k}
        if knowledge_base_id:
            params["kb_id"] = knowledge_base_id

        result = await self.db.execute(sql, params)
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
                    "parent_chunk_id": getattr(row, "parent_chunk_id", None),
                },
                "document": {
                    "id": row.document_id,
                    "filename": row.filename,
                    "title": row.doc_title or row.filename,
                },
                "similarity": round(float(row.rank), 6),
            }
            for row in rows
        ]

    # ==========================================================================
    # Reciprocal Rank Fusion (RRF)
    # ==========================================================================

    @staticmethod
    def _reciprocal_rank_fusion(
        dense_results: list[dict[str, Any]],
        bm25_results: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """RRF score = sum(weight / (k + rank)) per retriever; rank-based, score-scale agnostic."""
        k = settings.rag_hybrid_rrf_k
        dense_weight = settings.rag_hybrid_dense_weight
        bm25_weight = settings.rag_hybrid_bm25_weight

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

        sorted_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)[:top_k]

        results = []
        for chunk_id in sorted_ids:
            entry = chunk_data[chunk_id].copy()
            entry["similarity"] = round(scores[chunk_id], 6)
            results.append(entry)
        return results

    # ==========================================================================
    # Reranking (dedicated model scores query-chunk pairs)
    # ==========================================================================

    async def _rerank(self, query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Rerank results using a dedicated reranker model via Ollama embeddings.

        Computes query and chunk embeddings with the reranker model, then scores
        by cosine similarity. Second-pass relevance check with a different model
        than storage embeddings.
        """
        rerank_top_k = settings.rag_rerank_top_k
        if not settings.rag_rerank_enabled or not results:
            return results[:rerank_top_k]

        try:
            client = await self._get_ollama_client()
            model = settings.rag_rerank_model

            q_resp = await asyncio.wait_for(
                client.embeddings(model=model, prompt=query),
                timeout=settings.rag_embedding_timeout,
            )
            q_emb = q_resp.embedding

            sem = asyncio.Semaphore(5)

            async def _score(r):
                content = r["chunk"]["content"][:1000]  # Cap for speed
                async with sem:
                    c_resp = await asyncio.wait_for(
                        client.embeddings(model=model, prompt=content),
                        timeout=settings.rag_embedding_timeout,
                    )
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
    # Parent-Child resolution (small chunks for retrieval, large for context)
    # ==========================================================================

    async def _resolve_parents(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace child chunk content with parent content, deduplicate by parent."""
        if not results:
            return results

        child_parent_map: dict[int, list[dict[str, Any]]] = {}
        for r in results:
            pid = r["chunk"].get("parent_chunk_id")
            if pid:
                child_parent_map.setdefault(pid, []).append(r)

        if not child_parent_map:
            return results

        parent_ids = list(child_parent_map.keys())
        stmt = text("""
            SELECT id, content, page_number, section_title
            FROM document_chunks
            WHERE id = ANY(:parent_ids)
        """)
        rows = (await self.db.execute(stmt, {"parent_ids": parent_ids})).fetchall()
        parents = {row.id: row for row in rows}

        resolved = []
        seen_parents = set()
        for r in results:
            pid = r["chunk"].get("parent_chunk_id")
            if pid and pid in parents:
                if pid in seen_parents:
                    continue
                seen_parents.add(pid)
                parent = parents[pid]
                r["chunk"]["content"] = parent.content
                r["chunk"]["page_number"] = parent.page_number
                r["chunk"]["section_title"] = parent.section_title
                r["chunk"]["chunk_type"] = "parent"
            resolved.append(r)
        return resolved

    # ==========================================================================
    # Context-window expansion (adjacent chunks merged into each result)
    # ==========================================================================

    async def _expand_context_window(
        self,
        results: list[dict[str, Any]],
        window_size: int,
    ) -> list[dict[str, Any]]:
        """
        Expand each result with adjacent chunks from the same document.

        For each hit chunk with chunk_index=N, fetches chunks N-window..N+window
        and merges their content. Deduplicates when adjacent chunks are both hits.
        Single batched SQL query instead of one per result.
        """
        if not results:
            return results

        ranges = []
        for result in results:
            doc_id = result["document"]["id"]
            center_index = result["chunk"]["chunk_index"]
            min_index = max(0, center_index - window_size)
            max_index = center_index + window_size
            ranges.append((doc_id, min_index, max_index))

        conditions = []
        params: dict[str, Any] = {}
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

        rows_by_doc: dict[int, list[Any]] = defaultdict(list)
        for row in all_rows:
            rows_by_doc[row.document_id].append(row)

        expanded = []
        seen_chunks: set[int] = set()
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

            merged_content = (
                "\n\n".join(merged_content_parts)
                if merged_content_parts
                else result["chunk"]["content"]
            )

            expanded.append({
                "chunk": {
                    "id": result["chunk"]["id"],
                    "content": merged_content,
                    "chunk_index": result["chunk"]["chunk_index"],
                    "page_number": result["chunk"]["page_number"],
                    "section_title": result["chunk"]["section_title"],
                    "chunk_type": result["chunk"]["chunk_type"],
                },
                "document": result["document"],
                "similarity": result["similarity"],
            })
        return expanded

    # ==========================================================================
    # Context formatting (search results -> prompt-ready string)
    # ==========================================================================

    async def get_context(
        self,
        query: str,
        top_k: int | None = None,
        knowledge_base_id: int | None = None,
    ) -> str:
        """Search + format into a prompt-ready context string with source attribution."""
        results = await self.search(query, top_k, knowledge_base_id)
        return self.format_context_from_results(results)

    def format_context_from_results(self, results: list[dict[str, Any]]) -> str:
        """Format pre-fetched search results into context string without re-searching.

        Kept as instance method (not @staticmethod) to match the original
        RAGService.format_context_from_results signature exactly. The body
        does not use self; the instance binding is preserved for API parity
        and to avoid breaking any subclass that overrides it.
        """
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
