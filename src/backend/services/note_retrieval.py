"""Circle-filtered read access for :class:`Note` (Phase 4B).

Hybrid search over a note's title+body — the lexical FTS branch (``search_vector``)
RRF-fused with a dense embedding branch (halfvec cosine) — feeding the ``/brain``
RRF fusion via ``polymorphic_atom_store``. The result dict shape is what
``_wrap_note_results`` consumes. Mirrors the memory/RAG dense pattern + the
``DocumentFactRetrieval`` error contract: each query swallows input-shaped errors
→ ``[]`` so one bad query can't take /brain down, but re-raises
operational/structural errors so a lagging migration is visible. Dense is
best-effort (``notes_semantic_search_enabled``, Postgres-only, embed-if-reachable);
off ⇒ FTS-only (4B.1 behavior). Notes without an embedding are still found by FTS.
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import EMBEDDING_DIMENSION, TIER_PUBLIC
from services.circle_sql import notes_circles_filter
from services.fts_languages import build_tsquery_union_sql
from services.lexical_retrieval import _significant_tokens
from utils.config import settings

_SNIPPET_CHARS = 240
_RRF_K = 60  # reciprocal-rank-fusion constant (standard)

_NOTE_COLS = "n.id, n.atom_id, n.owner_user_id, n.project_id, n.title, n.body, n.circle_tier"


def _row_to_dict(row: Any, *, rank: float = 0.0) -> dict[str, Any]:
    body = row.body or ""
    return {
        "id": row.id,
        "atom_id": row.atom_id,
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "title": row.title,
        "snippet": body[:_SNIPPET_CHARS],
        "circle_tier": row.circle_tier or 0,
        "similarity": round(float(rank), 6),
    }


class NoteRetrieval:
    """Circle-filtered keyword search over notes."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _notes_circles_filter(
        user_id: int | None, enforce_circles: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """WHERE-fragment + params for circle access on ``notes`` (alias ``n``).

        AUTH off → bypass (unless federation ``enforce_circles``). Anonymous authed
        caller → public-tier only. Else the 4-branch atom filter."""
        if not settings.auth_enabled and not enforce_circles:
            return ("TRUE", {})
        if user_id is None:
            return ("n.circle_tier = :asker_id_pub", {"asker_id_pub": TIER_PUBLIC})
        return notes_circles_filter(user_id, peer_scoped=enforce_circles)

    def _is_postgres(self) -> bool:
        return self.db.bind is not None and self.db.bind.dialect.name == "postgresql"

    async def _fetch(self, sql: Any, params: dict[str, Any]) -> list[Any]:
        try:
            return (await self.db.execute(sql, params)).fetchall()
        except (OperationalError, ProgrammingError):
            logger.error("🔍 Note search: operational DB error — re-raising (not masking as empty)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🔍 Note search failed (ignored): {e}")
            return []

    async def search(
        self,
        query: str,
        *,
        asker_id: int | None,
        top_k: int,
        enforce_circles: bool = False,
    ) -> list[dict[str, Any]]:
        """Hybrid search over note title+body, circle-filtered: the FTS branch
        RRF-fused with a dense (embedding) branch. ``[]`` on thin query / error.

        Dense is best-effort — off when ``notes_semantic_search_enabled`` is
        False, on sqlite, or when the query can't be embedded; then this is
        FTS-only (4B.1 behavior). Notes without an embedding are still found by
        FTS, so RRF never drops them."""
        tokens = _significant_tokens(query)
        if not tokens:
            return []
        if not self._is_postgres():
            return await self._search_sqlite(tokens, asker_id, top_k, enforce_circles)

        # Over-fetch each branch so the fusion has candidates to reorder.
        fan = max(top_k * 3, top_k)
        fts_rows = await self._fts_search(tokens, asker_id, fan, enforce_circles)

        dense_rows: list[Any] = []
        if settings.notes_semantic_search_enabled:
            qemb = await self._embed_query(query)
            if qemb is not None:
                dense_rows = await self._dense_search(qemb, asker_id, fan, enforce_circles)

        if not dense_rows:
            return [_row_to_dict(r, rank=r.rank) for r in fts_rows[:top_k]]
        return self._rrf_fuse([fts_rows, dense_rows], top_k)

    async def _fts_search(
        self, tokens: list[str], asker_id: int | None, limit: int, enforce_circles: bool
    ) -> list[Any]:
        circles_clause, circles_params = self._notes_circles_filter(asker_id, enforce_circles)
        params: dict[str, Any] = {"limit": limit, "or_query": " OR ".join(tokens), **circles_params}
        tsq = build_tsquery_union_sql("or_query")
        sql = text(f"""
            SELECT {_NOTE_COLS},
                   CASE WHEN n.search_vector @@ ({tsq})
                        THEN ts_rank(n.search_vector, {tsq}) ELSE 0 END AS rank
            FROM notes n
            WHERE n.search_vector IS NOT NULL AND n.search_vector @@ ({tsq})
              AND {circles_clause}
            ORDER BY rank DESC, n.id DESC
            LIMIT :limit
        """)
        return await self._fetch(sql, params)

    async def _dense_search(
        self, query_embedding: list[float], asker_id: int | None, limit: int, enforce_circles: bool
    ) -> list[Any]:
        """Circle-filtered cosine over note embeddings via a halfvec cast (uses the
        halfvec HNSW index). `dim` is the trusted settings value → f-string safe."""
        circles_clause, circles_params = self._notes_circles_filter(asker_id, enforce_circles)
        dim = EMBEDDING_DIMENSION
        params: dict[str, Any] = {
            "limit": limit,
            "qemb": "[" + ",".join(map(str, query_embedding)) + "]",
            **circles_params,
        }
        sql = text(f"""
            SELECT {_NOTE_COLS},
                   1 - (n.embedding::halfvec({dim}) <=> CAST(:qemb AS halfvec({dim}))) AS rank
            FROM notes n
            WHERE n.embedding IS NOT NULL AND {circles_clause}
            ORDER BY n.embedding::halfvec({dim}) <=> CAST(:qemb AS halfvec({dim})) ASC
            LIMIT :limit
        """)
        return await self._fetch(sql, params)

    async def _embed_query(self, query: str) -> list[float] | None:
        """Embed the query text (best-effort — None if the embed model is down,
        so search degrades to FTS-only)."""
        try:
            from utils.llm_client import get_embed_client
            client = get_embed_client()
            resp = await client.embeddings(model=settings.ollama_embed_model, prompt=query)
            return resp.embedding
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🔍 Note search: query embed failed, FTS-only: {e}")
            return None

    def _rrf_fuse(self, ranked_lists: list[list[Any]], top_k: int) -> list[dict[str, Any]]:
        """Reciprocal-rank fusion of the branch result lists, keyed by note id.
        Score = Σ 1/(_RRF_K + rank) across the lists a note appears in."""
        scores: dict[int, float] = {}
        row_by_id: dict[int, Any] = {}
        for rows in ranked_lists:
            for rank, row in enumerate(rows, start=1):
                scores[row.id] = scores.get(row.id, 0.0) + 1.0 / (_RRF_K + rank)
                row_by_id.setdefault(row.id, row)
        ordered = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[:top_k]
        return [_row_to_dict(row_by_id[nid], rank=score) for nid, score in ordered]

    async def _search_sqlite(
        self, tokens: list[str], asker_id: int | None, top_k: int, enforce_circles: bool
    ) -> list[dict[str, Any]]:
        """Sqlite test fallback: token-OR LIKE over title+body with a match-count rank."""
        circles_clause, circles_params = self._notes_circles_filter(asker_id, enforce_circles)
        params: dict[str, Any] = {"limit": top_k, **circles_params}
        match_terms, count_terms = [], []
        for i, tok in enumerate(tokens):
            p = f"tok_{i}"
            params[p] = f"%{tok}%"
            fm = f"(n.title LIKE :{p} OR n.body LIKE :{p})"
            match_terms.append(fm)
            count_terms.append(f"CASE WHEN {fm} THEN 1 ELSE 0 END")
        or_clause = " OR ".join(match_terms)
        count_expr = " + ".join(count_terms)
        sql = text(f"""
            SELECT {_NOTE_COLS}, ({count_expr}) AS rank
            FROM notes n
            WHERE ({or_clause}) AND {circles_clause}
            ORDER BY rank DESC, n.id DESC
            LIMIT :limit
        """)
        rows = await self._fetch(sql, params)
        return [_row_to_dict(r, rank=float(r.rank)) for r in rows]
