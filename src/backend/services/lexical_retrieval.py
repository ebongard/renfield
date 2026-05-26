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
from services.fts_languages import FTS_LANGUAGES, build_tsquery_union_sql
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

    Security note: the regex below only emits word characters
    (German alphabet + digits), so the tokens are safe to pre-join
    with " OR " for ``websearch_to_tsquery``. No metachars (``"``,
    ``-``, ``(``) can survive tokenization, so the websearch syntax
    accepts the joined string verbatim. If this regex ever loosens
    to allow punctuation, switch to ``to_tsquery`` with explicit
    ``|`` operators and bound-per-token parameters.
    """
    raw = re.findall(r"[A-Za-zÄÖÜäöüß0-9]{2,}", query or "")
    out = [t for t in raw if len(t) >= 3 and t.lower() not in _STOP_TOKENS]
    return out


def _check_fts_config_at_startup() -> None:
    """Emit a one-shot WARNING if ``settings.rag_hybrid_fts_config`` is
    not one of the languages indexed by the GENERATED column.

    The pc20260528 migration unions ``to_tsvector`` across all
    ``FTS_LANGUAGES`` (DE / EN / FR / IT / ES / NL) on the indexing
    side, and the memory retriever unions ``websearch_to_tsquery``
    across the same set on the query side. So a deployment that picks
    one of those configs for chunk-side queries (``rag_hybrid_fts_config``,
    used by ``search_chunks_lexical`` against the older
    ``document_chunks.search_vector`` column whose contract is
    single-config) is fine. Setting it to an unindexed language is
    almost certainly a config typo — warn loudly at startup.

    Adding a 7th language: update ``FTS_LANGUAGES`` AND write a
    follow-up migration that rebuilds the generated column. This
    warning catches the case where someone updates the config without
    the migration.
    """
    cfg = settings.rag_hybrid_fts_config
    if cfg not in FTS_LANGUAGES:
        logger.warning(
            f"🔍 FTS config mismatch: rag_hybrid_fts_config={cfg!r} is "
            f"not in FTS_LANGUAGES={FTS_LANGUAGES}. Chunk-side lexical "
            f"queries (search_chunks_lexical) will run against an "
            f"unindexed config and return 0 results. See "
            f"services/fts_languages.py and pc20260528 migration."
        )


_check_fts_config_at_startup()


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
        """FTS search over ``conversation_memories.search_vector``.

        Postgres path: ``websearch_to_tsquery`` + ``ts_rank``, unioned
        across all ``FTS_LANGUAGES`` (DE / EN / FR / IT / ES / NL) on
        both sides. The GENERATED ``search_vector`` is itself a union
        of ``to_tsvector`` across the same 6 configs, so any
        language-specific stemmer that matches a term contributes a
        match. Rare proper nouns ("Jutta") rank higher than frequent
        function-words ("gerne") automatically via IDF — exactly what
        natural-language queries need. Result: "Was mag Jutta gerne
        essen?" surfaces the Maracujas memory (Jutta-rank dominates).

        Sqlite test path: no tsvector available. Falls back to
        token-OR LIKE with a per-row match-count ranking that
        approximates IDF (rows matching more distinct tokens rank
        higher). Different semantics than the Postgres path; the
        test that matters for shape-parity (returns memory-shape
        dicts) holds.

        Returns results in the same shape as
        ``MemoryRetrieval.retrieve`` so ``_wrap_memory_results`` in
        polymorphic_atom_store consumes the output directly.
        """
        tokens = _significant_tokens(query)
        if not tokens or asker_id is None:
            return []

        is_postgres = (
            self.db.bind is not None
            and self.db.bind.dialect.name == "postgresql"
        )

        if is_postgres:
            # websearch_to_tsquery accepts user input verbatim and
            # never raises on malformed input (unlike to_tsquery).
            # Default semantic is OR between bare tokens, which is
            # what we want for natural-language queries. We union the
            # tsquery across all FTS_LANGUAGES so each stemmer gets a
            # chance to recognize the term.
            or_query = " OR ".join(tokens)
            tsquery_union = build_tsquery_union_sql("or_query")
            circles_clause, circles_params = conversation_memories_circles_filter(asker_id)
            params: dict[str, Any] = {
                "or_query": or_query,
                "limit": top_k,
                **circles_params,
            }
            sql = text(f"""
                SELECT
                    m.id, m.atom_id, m.user_id, m.content,
                    m.category, m.importance, m.confidence,
                    m.circle_tier, m.created_at, m.last_accessed_at,
                    ts_rank(
                        m.search_vector,
                        {tsquery_union}
                    ) AS rank
                FROM conversation_memories m
                WHERE m.is_active = TRUE
                  AND m.search_vector IS NOT NULL
                  AND m.search_vector @@ ({tsquery_union})
                  AND {circles_clause}
                ORDER BY rank DESC, m.importance DESC, m.created_at DESC
                LIMIT :limit
            """)
        else:
            # Sqlite test path. No tsvector → use a token-OR LIKE
            # with a per-row match-count proxy for IDF ranking
            # (rows matching more distinct tokens rank higher).
            #
            # We compute the count via a CASE-sum so each token is
            # bound once. Sqlite's LIKE is case-insensitive for ASCII
            # by default — same case-folding behavior as ILIKE for the
            # German-ASCII subset our tests use.
            params = {"limit": top_k, "asker_id": asker_id}
            match_terms = []
            count_terms = []
            for i, token in enumerate(tokens):
                p = f"tok_{i}"
                match_terms.append(f"m.content LIKE :{p}")
                count_terms.append(
                    f"CASE WHEN m.content LIKE :{p} THEN 1 ELSE 0 END"
                )
                params[p] = f"%{token}%"
            or_clause = " OR ".join(match_terms)
            count_expr = " + ".join(count_terms)
            sql = text(f"""
                SELECT
                    m.id, m.atom_id, m.user_id, m.content,
                    m.category, m.importance, m.confidence,
                    m.circle_tier, m.created_at, m.last_accessed_at,
                    ({count_expr}) AS rank
                FROM conversation_memories m
                WHERE m.user_id = :asker_id
                  AND ({or_clause})
                ORDER BY rank DESC, m.importance DESC, m.created_at DESC
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
                # `similarity` retained for shape parity with
                # MemoryRetrieval.retrieve; we surface the rank
                # so downstream RRF can use it as a tie-breaker.
                "similarity": float(row.rank) if row.rank is not None else 0.0,
            }
            for row in rows
        ]
