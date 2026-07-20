"""Circle-filtered read access for :class:`Note` (Phase 4B.1).

Keyword FTS over a note's title+body (``search_vector``), feeding the ``/brain``
RRF fusion via ``polymorphic_atom_store``. 4B.1 is FTS-only (no embedding yet);
the result dict shape is what ``_wrap_note_results`` consumes. Mirrors
``DocumentFactRetrieval`` (the keyword-only atom-source template): each query
swallows input-shaped errors → ``[]`` so one bad query can't take /brain down,
but re-raises operational/structural errors so a lagging migration is visible.
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import TIER_PUBLIC
from services.circle_sql import notes_circles_filter
from services.fts_languages import build_tsquery_union_sql
from services.lexical_retrieval import _significant_tokens
from utils.config import settings

_SNIPPET_CHARS = 240

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
        """FTS over note title+body, circle-filtered. ``[]`` on thin query / error."""
        tokens = _significant_tokens(query)
        if not tokens:
            return []
        if not self._is_postgres():
            return await self._search_sqlite(tokens, asker_id, top_k, enforce_circles)

        circles_clause, circles_params = self._notes_circles_filter(asker_id, enforce_circles)
        params: dict[str, Any] = {"limit": top_k, "or_query": " OR ".join(tokens), **circles_params}
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
        rows = await self._fetch(sql, params)
        return [_row_to_dict(r, rank=r.rank) for r in rows]

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
