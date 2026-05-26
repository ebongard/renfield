"""Lexical (keyword / name) retrieval for the polymorphic atom store.

Vector search is great for paraphrase queries ("what does the user
prefer to drink?" → "user likes tea") but bad for name and short-token
queries ("Jutta" → ...). Embedding similarity between a single token
and the corpus depends on the embedding model's behaviour at that
extreme; for qwen3-embedding:4b on the production corpus we measured
cosine ~0.0-0.5 between "jutta" and the literal sentence "Jutta mag
Maracujas und Ananas". Below `memory_retrieval_threshold` either way —
the user sees nothing.

This module adds a deterministic lexical retriever that complements
the vector path. It feeds into `polymorphic_atom_store` as an extra
source for the RRF merge. When the same atom appears in both lists
the RRF score doubles up, which is exactly the boost we want for
"this is both a semantic AND keyword match" cases.

Two methods, each returning results in the shape the existing
`_wrap_*` helpers in polymorphic_atom_store already understand:

  - ``search_chunks_lexical(query, asker_id, top_k)``     → RAG shape
  - ``search_memories_lexical(query, asker_id, top_k)``   → memory shape

Chunks use the existing ``document_chunks.search_vector`` tsvector
(populated at ingestion). Memories use ILIKE — `conversation_memories`
has no tsvector column today, and a per-user corpus rarely exceeds
~1000 rows, so the seq-scan cost is negligible. A future migration
could add tsvector here too, but it's not on the critical path.
"""
from __future__ import annotations

import re
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.circle_sql import (
    conversation_memories_circles_filter,
    document_chunks_circles_filter,
)
from utils.config import settings
from utils.content_quality import is_low_quality_text


# Drop short tokens (≤2 chars) and a bare-minimum German+English stop list.
# Lexical retrieval on "the" or "und" produces matches with no signal;
# this is just enough to keep the LIKE/tsquery patterns sharp without
# pulling in a heavyweight tokenizer.
_STOP_TOKENS = {
    "der", "die", "das", "und", "oder", "aber", "ist", "wer", "was",
    "wie", "wo", "wann", "warum", "mag", "den", "dem", "des",
    "the", "and", "or", "but", "is", "are", "who", "what",
    "how", "where", "when", "why", "of", "for", "in", "on", "at",
}


def _significant_tokens(query: str) -> list[str]:
    """Split ``query`` into tokens worth a lexical lookup.

    Strips short tokens and a small stop-word set. Returns an empty
    list when the cleaned query is too thin to be useful — caller is
    expected to short-circuit to [] in that case.
    """
    raw = re.findall(r"[A-Za-zÄÖÜäöüß0-9]{2,}", query or "")
    out = [t for t in raw if len(t) >= 3 and t.lower() not in _STOP_TOKENS]
    return out


class LexicalRetrieval:
    """Keyword/name retrieval for chunks (tsvector) and memories (ILIKE)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def search_chunks_lexical(
        self,
        query: str,
        *,
        asker_id: int | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """tsvector OR-match across `document_chunks.search_vector`.

        Returns results in the same shape as ``RAGRetrieval.search``
        so the existing ``_wrap_rag_results`` in polymorphic_atom_store
        can consume the output directly.

        Skips silently when the query has no significant tokens, when
        the asker isn't identified (caller will scope to the right
        user upstream), or when the backing dialect isn't Postgres
        (test harness sqlite).
        """
        tokens = _significant_tokens(query)
        if not tokens or asker_id is None:
            return []
        if self.db.bind is None or self.db.bind.dialect.name != "postgresql":
            return []

        fts_config = settings.rag_hybrid_fts_config
        circles_clause, circles_params = document_chunks_circles_filter(asker_id)
        or_query = " OR ".join(tokens)

        sql = text(f"""
            SELECT
                dc.id, dc.document_id, dc.content, dc.chunk_index,
                dc.page_number, dc.section_title, dc.chunk_type,
                dc.parent_chunk_id, dc.circle_tier,
                d.filename, d.title AS doc_title,
                d.atom_id AS doc_atom_id, d.circle_tier AS doc_circle_tier,
                ts_rank_cd(dc.search_vector, websearch_to_tsquery(:fts_config, :or_query)) AS rank
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
            WHERE d.status = 'completed'
              AND dc.search_vector IS NOT NULL
              AND dc.search_vector @@ websearch_to_tsquery(:fts_config, :or_query)
              AND {circles_clause}
            ORDER BY rank DESC
            LIMIT :limit
        """)
        params: dict[str, Any] = {
            "or_query": or_query,
            "fts_config": fts_config,
            "limit": top_k,
            **circles_params,
        }
        try:
            rows = (await self.db.execute(sql, params)).fetchall()
        except Exception as e:  # noqa: BLE001
            # Don't let a malformed query take the brain page down — we
            # have a vector retriever as the primary path.
            logger.warning(f"🔍 Lexical chunk search failed (ignored): {e}")
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            if is_low_quality_text(row.content):
                continue
            out.append({
                "chunk": {
                    "id": row.id,
                    "content": row.content,
                    "chunk_index": row.chunk_index,
                    "page_number": row.page_number,
                    "section_title": row.section_title,
                    "chunk_type": row.chunk_type,
                    "parent_chunk_id": row.parent_chunk_id,
                    "circle_tier": row.circle_tier or 0,
                },
                "document": {
                    "id": row.document_id,
                    "filename": row.filename,
                    "title": row.doc_title or row.filename,
                    "atom_id": row.doc_atom_id,
                    "circle_tier": row.doc_circle_tier or 0,
                },
                # ts_rank_cd output is unbounded; expose it as "similarity"
                # for shape compatibility but it's an unrelated scale —
                # only used as the snippet-tiebreaker in the wrappers.
                "similarity": round(float(row.rank), 6),
            })
        return out

    async def search_memories_lexical(
        self,
        query: str,
        *,
        asker_id: int | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """ILIKE OR-match across ``conversation_memories.content``.

        Returns results in the same shape as
        ``MemoryRetrieval.retrieve`` so ``_wrap_memory_results`` in
        polymorphic_atom_store can consume the output directly.

        Each token must appear (case-insensitive) for the row to match.
        That's a tighter recall than tsvector's OR but matches user
        intent for name queries: typing "Jutta Geburtstag" should
        prefer rows that mention BOTH.
        """
        tokens = _significant_tokens(query)
        if not tokens or asker_id is None:
            return []

        is_postgres = (
            self.db.bind is not None
            and self.db.bind.dialect.name == "postgresql"
        )

        # ILIKE is Postgres-only — sqlite test harness needs plain LIKE
        # (sqlite's LIKE is case-insensitive by default for ASCII).
        like_op = "ILIKE" if is_postgres else "LIKE"
        clauses = []
        params: dict[str, Any] = {"limit": top_k}
        for i, token in enumerate(tokens):
            param_name = f"tok_{i}"
            clauses.append(f"m.content {like_op} :{param_name}")
            params[param_name] = f"%{token}%"
        token_filter = " AND ".join(clauses)

        if is_postgres:
            circles_clause, circles_params = conversation_memories_circles_filter(asker_id)
            params.update(circles_params)
            sql = text(f"""
                SELECT
                    m.id, m.atom_id, m.user_id, m.content,
                    m.category, m.importance, m.confidence,
                    m.circle_tier, m.created_at, m.last_accessed_at
                FROM conversation_memories m
                WHERE m.is_active = TRUE
                  AND {token_filter}
                  AND {circles_clause}
                ORDER BY m.importance DESC, m.created_at DESC
                LIMIT :limit
            """)
        else:
            # Sqlite test path: no circle_sql tier_clause helpers, no
            # is_active assumption (some fixtures omit it). Owner-only
            # filter is enough for unit-test parity.
            params["asker_id"] = asker_id
            sql = text(f"""
                SELECT
                    m.id, m.atom_id, m.user_id, m.content,
                    m.category, m.importance, m.confidence,
                    m.circle_tier, m.created_at, m.last_accessed_at
                FROM conversation_memories m
                WHERE m.user_id = :asker_id
                  AND {token_filter}
                ORDER BY m.importance DESC, m.created_at DESC
                LIMIT :limit
            """)

        try:
            rows = (await self.db.execute(sql, params)).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🔍 Lexical memory search failed (ignored): {e}")
            return []

        return [
            {
                "id": row.id,
                "atom_id": row.atom_id,
                "user_id": row.user_id,
                "content": row.content,
                "category": row.category,
                "importance": float(row.importance) if row.importance is not None else 0.5,
                "confidence": float(row.confidence) if row.confidence is not None else 1.0,
                "circle_tier": row.circle_tier or 0,
                "created_at": row.created_at,
                # similarity unused but kept for shape parity with
                # MemoryRetrieval.retrieve.
                "similarity": 1.0,
            }
            for row in rows
        ]
