"""Read access for Schicht A ``document_facts`` — keyword FTS + identifier ILIKE.

Schicht A extraction writes structured facts (``document_facts``) but nothing
read them: they were write-only until this module. Facts are short structured
strings — a Steuernummer (``114/5876/5293``), an issuer (``Finanzverwaltung
NRW``), a legal action (``Widerspruch``). You find them by exact token, not
fuzzy concept, so this is keyword retrieval (FTS), NOT embeddings — the source
chunk is already embedded and surfaces via RAG.

Three access shapes, each circle-access-filtered through the parent Document:

  - ``search(query, asker_id, top_k)``    → FTS over ``search_vector`` ∪ an
    identifier-ILIKE branch (Postgres tokenizes ``114/5876/5293`` unreliably,
    so exact-identifier lookup goes through ``normalized_value ILIKE``). Feeds
    the ``/brain`` RRF fusion via ``polymorphic_atom_store``.
  - ``facts_for_document(document_id, asker_id)`` → all facts of one doc (the
    future per-document panel; the route gates access separately).
  - ``obligations(asker_id, due_before, limit)`` → the obligation agenda
    (bills + Behörde deadlines), ordered by printed Frist.

Results mirror the dict shape the ``_wrap_document_fact_results`` helper in
``polymorphic_atom_store`` consumes. Each query swallows its own exceptions and
returns ``[]`` so a malformed input never takes the brain page down (the RRF
gather is resilient — facts are one source among several).
"""
from __future__ import annotations

import re
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import OBLIGATION_MILESTONE_CONFIRMED, TIER_PUBLIC
from services.circle_sql import document_facts_circles_filter
from services.fts_languages import build_tsquery_union_sql
from services.lexical_retrieval import _significant_tokens
from utils.config import settings


# Rank floor for rows that match ONLY via the identifier-ILIKE branch (no FTS
# hit). Small positive constant so an exact-identifier match always sorts above
# nothing but below any genuine FTS relevance score.
_ILIKE_RANK = 0.01


def _identifier_tokens(query: str) -> list[str]:
    """Whitespace-split tokens that look like an identifier (contain a digit
    or a ``/``).

    The ILIKE branch is gated on these so prose queries ("Finanzamt") stay on
    the FTS-only hot path — no leading-wildcard ``ILIKE '%…%'`` seq-scan unless
    the query actually carries an identifier-shaped token. Surrounding
    punctuation is stripped but digits, letters, ``/``, ``.`` and ``-`` are
    kept (Steuernummer, IBAN, dates, file numbers). Tokens shorter than 3 chars
    after cleaning are dropped as too noisy.

    The cleaned tokens are bound as ``ILIKE`` parameters (``%token%``), never
    interpolated, so there is no injection surface even though the cleaning
    regex is permissive.
    """
    out: list[str] = []
    for tok in (query or "").split():
        if any(c.isdigit() for c in tok) or "/" in tok:
            cleaned = re.sub(r"[^0-9A-Za-zÄÖÜäöüß/.\-]", "", tok)
            # Require an alphanumeric char so all-punctuation runs ("//////")
            # don't survive the length gate and build a no-op ILIKE.
            if len(cleaned) >= 3 and any(c.isalnum() for c in cleaned):
                out.append(cleaned)
    return out


# Shared SELECT column list — identical projection across all three queries so
# the row→dict conversion is uniform. ``df`` is the document_facts alias.
_FACT_COLS = """
    df.id, df.document_id, df.atom_id, df.category, df.kind, df.value,
    df.normalized_value, df.excerpt, df.obligation_date, df.amount_value,
    df.amount_currency, df.legal_gate, df.payment_method, df.confidence,
    df.source, df.circle_tier, df.tier_overridden
"""


def _row_to_dict(row: Any, *, rank: float = 0.0) -> dict[str, Any]:
    """Convert a fact row to a JSON-safe dict.

    ``obligation_date`` → ISO string (or None); ``amount_value`` → float (it
    arrives as Decimal from NUMERIC). ``similarity`` carries the rank for RRF
    tie-breaking / shape parity with the other retrievers.
    """
    return {
        "id": row.id,
        "document_id": row.document_id,
        "atom_id": row.atom_id,
        "category": row.category,
        "kind": row.kind,
        "value": row.value,
        "normalized_value": row.normalized_value,
        "excerpt": row.excerpt,
        "obligation_date": row.obligation_date.isoformat() if row.obligation_date else None,
        "amount_value": float(row.amount_value) if row.amount_value is not None else None,
        "amount_currency": row.amount_currency,
        "legal_gate": bool(row.legal_gate),
        "payment_method": row.payment_method,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "circle_tier": row.circle_tier or 0,
        "tier_overridden": bool(getattr(row, "tier_overridden", False)),
        "source": row.source,
        "similarity": round(float(rank), 6),
        # Present only on the obligations() query (LEFT-derived EXISTS); other
        # queries don't select it, so default False via getattr.
        "confirmed": bool(getattr(row, "confirmed", False)),
    }


class DocumentFactRetrieval:
    """Circle-filtered read access for Schicht A document facts."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------ #
    # Circle filter gate (mirrors RAGRetrieval._chunk_circles_filter)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _facts_circles_filter(
        user_id: int | None, enforce_circles: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """WHERE-fragment + params for circle access on ``document_facts``.

        AUTH_ENABLED=false → full bypass, UNLESS ``enforce_circles`` (federation),
        which keeps the peer-scoped filter active. Anonymous authed caller
        (``user_id is None`` while auth is on) → public-tier only.
        Authenticated caller (or ``enforce_circles``) → the parent-Document OR via
        ``circle_sql`` (peer-scoped drops the owner + explicit-grant branches).
        """
        if not settings.auth_enabled and not enforce_circles:
            return ("TRUE", {})
        if user_id is None:
            return ("df.circle_tier = :asker_id_pub", {"asker_id_pub": TIER_PUBLIC})
        return document_facts_circles_filter(user_id, peer_scoped=enforce_circles)

    def _is_postgres(self) -> bool:
        return (
            self.db.bind is not None
            and self.db.bind.dialect.name == "postgresql"
        )

    async def _fetch(self, sql: Any, params: dict[str, Any], label: str) -> list[Any]:
        """Execute + fetchall, distinguishing operational from input failures.

        Operational/structural errors (DB down, a column missing because the
        migration lagged a rolling deploy) must NOT be masked as an empty
        corpus — that makes a dark feature indistinguishable from "factless".
        We log them at ERROR and re-raise: the routes surface a 500 (correct
        outage signal) and the /brain RRF gather degrades just this one source
        while the ERROR log keeps the failure visible. Other (input-shaped)
        errors stay swallowed → [] so a single bad query can't take /brain down.
        """
        try:
            return (await self.db.execute(sql, params)).fetchall()
        except (OperationalError, ProgrammingError):
            logger.error(f"🔍 {label}: operational DB error — re-raising (not masking as empty)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"🔍 {label} failed (ignored): {e}")
            return []

    # ------------------------------------------------------------------ #
    # search — FTS ∪ identifier-ILIKE
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        *,
        asker_id: int | None,
        top_k: int,
        enforce_circles: bool = False,
    ) -> list[dict[str, Any]]:
        """Keyword search over facts: FTS on ``search_vector`` plus an
        identifier-ILIKE branch (added only for identifier-shaped queries).

        ``enforce_circles`` (federation): keep peer-scoped circle filtering even
        with auth off. Returns ``[]`` for thin queries, on any DB error, and for
        the no-significant-token / no-identifier-token case.
        """
        fts_tokens = _significant_tokens(query)
        ident_tokens = _identifier_tokens(query)
        if not fts_tokens and not ident_tokens:
            return []

        if not self._is_postgres():
            return await self._search_sqlite(
                fts_tokens, ident_tokens, asker_id, top_k, enforce_circles
            )

        circles_clause, circles_params = self._facts_circles_filter(asker_id, enforce_circles)
        params: dict[str, Any] = {"limit": top_k, **circles_params}
        match_parts: list[str] = []
        rank_terms: list[str] = []

        if fts_tokens:
            # Union websearch_to_tsquery across FTS_LANGUAGES (same multilingual
            # pattern as the chunk/memory paths). ts_rank (NOT ts_rank_cd):
            # fact values are short single tokens, so cover-density adds noise.
            params["or_query"] = " OR ".join(fts_tokens)
            tsq = build_tsquery_union_sql("or_query")
            match_parts.append(f"(df.search_vector IS NOT NULL AND df.search_vector @@ ({tsq}))")
            rank_terms.append(
                f"CASE WHEN df.search_vector @@ ({tsq}) "
                f"THEN ts_rank(df.search_vector, {tsq}) ELSE 0 END"
            )

        if ident_tokens:
            # Exact-identifier lookup against the whitespace-collapsed
            # normalized_value. Postgres FTS tokenizes '114/5876/5293'
            # unreliably, so this branch is the dependable identifier path.
            ilike_parts = []
            for i, tok in enumerate(ident_tokens):
                p = f"ident_{i}"
                params[p] = f"%{tok}%"
                ilike_parts.append(f"df.normalized_value ILIKE :{p}")
            ilike_sql = "(" + " OR ".join(ilike_parts) + ")"
            match_parts.append(ilike_sql)
            rank_terms.append(f"CASE WHEN {ilike_sql} THEN {_ILIKE_RANK} ELSE 0 END")

        match_clause = " OR ".join(match_parts)
        rank_expr = f"GREATEST({', '.join(rank_terms)})" if len(rank_terms) > 1 else rank_terms[0]

        sql = text(f"""
            SELECT {_FACT_COLS}, {rank_expr} AS rank
            FROM document_facts df
            JOIN documents d ON df.document_id = d.id
            LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
            WHERE d.status = 'completed'
              AND ({match_clause})
              AND {circles_clause}
            ORDER BY rank DESC, df.id DESC
            LIMIT :limit
        """)

        rows = await self._fetch(sql, params, "Document-fact search")
        return [_row_to_dict(r, rank=r.rank) for r in rows]

    async def _search_sqlite(
        self,
        fts_tokens: list[str],
        ident_tokens: list[str],
        asker_id: int | None,
        top_k: int,
        enforce_circles: bool = False,
    ) -> list[dict[str, Any]]:
        """Sqlite test-harness fallback: token-OR LIKE with a match-count rank.

        No tsvector on sqlite — match all tokens (prose + identifier) against
        value / normalized_value / excerpt with a CASE-sum rank (rows matching
        more distinct tokens rank higher). The circle filter reduces to the
        auth-off bypass in the unit suite (unless ``enforce_circles``); if auth
        is on the same clause runs (sqlite is permissive about the json cast).
        """
        tokens = list(dict.fromkeys([*fts_tokens, *ident_tokens]))
        if not tokens:
            return []
        circles_clause, circles_params = self._facts_circles_filter(asker_id, enforce_circles)
        params: dict[str, Any] = {"limit": top_k, **circles_params}
        match_terms: list[str] = []
        count_terms: list[str] = []
        for i, tok in enumerate(tokens):
            p = f"tok_{i}"
            params[p] = f"%{tok}%"
            field_match = (
                f"(df.value LIKE :{p} OR df.normalized_value LIKE :{p} "
                f"OR df.excerpt LIKE :{p})"
            )
            match_terms.append(field_match)
            count_terms.append(f"CASE WHEN {field_match} THEN 1 ELSE 0 END")
        or_clause = " OR ".join(match_terms)
        count_expr = " + ".join(count_terms)
        sql = text(f"""
            SELECT {_FACT_COLS}, ({count_expr}) AS rank
            FROM document_facts df
            JOIN documents d ON df.document_id = d.id
            LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
            WHERE ({or_clause})
              AND {circles_clause}
            ORDER BY rank DESC, df.id DESC
            LIMIT :limit
        """)
        rows = await self._fetch(sql, params, "Document-fact sqlite search")
        return [_row_to_dict(r, rank=r.rank) for r in rows]

    # ------------------------------------------------------------------ #
    # facts_for_document — all facts of one document
    # ------------------------------------------------------------------ #
    async def facts_for_document(
        self,
        document_id: int,
        *,
        asker_id: int | None,
    ) -> list[dict[str, Any]]:
        """All circle-visible facts for one document, ordered category, kind.

        The ROUTE gates per-document access separately (404/403 on the parent
        Document); this query still applies the circle filter as defense in
        depth so a direct service caller can't over-read.
        """
        circles_clause, circles_params = self._facts_circles_filter(asker_id)
        params: dict[str, Any] = {"doc_id": document_id, **circles_params}
        sql = text(f"""
            SELECT {_FACT_COLS}
            FROM document_facts df
            JOIN documents d ON df.document_id = d.id
            LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
            WHERE df.document_id = :doc_id
              AND {circles_clause}
            ORDER BY df.category, df.kind, df.id
        """)
        rows = await self._fetch(sql, params, "facts_for_document")
        return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # obligations — the agenda query
    # ------------------------------------------------------------------ #
    async def obligations(
        self,
        *,
        asker_id: int | None,
        due_before: Any = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Circle-visible obligation facts with a printed Frist, soonest first.

        ``category='obligation'`` AND ``obligation_date IS NOT NULL`` (uses the
        partial index). Optional ``due_before`` (date / ISO string) caps the
        horizon. ``ORDER BY obligation_date ASC, df.id`` — the agenda surfaces
        the nearest deadline first. ``offset`` pages further into that stable
        order for the agenda's "Mehr laden" (the ``(obligation_date, id)`` sort
        is total, so offset paging never skips or repeats a row).
        """
        circles_clause, circles_params = self._facts_circles_filter(asker_id)
        # The asker's per-user Bestätigt state (milestone='confirmed'). Bind -1
        # for anonymous (no user) so the EXISTS never matches.
        confirm_uid = asker_id if asker_id is not None else -1
        params: dict[str, Any] = {
            "limit": limit, "offset": max(0, offset),
            "confirm_uid": confirm_uid, "confirmed_ms": OBLIGATION_MILESTONE_CONFIRMED,
            **circles_params,
        }
        due_filter = ""
        if due_before is not None:
            params["due_before"] = due_before
            due_filter = "AND df.obligation_date <= :due_before"
        sql = text(f"""
            SELECT {_FACT_COLS},
                   EXISTS (
                     SELECT 1 FROM obligation_acknowledgements oa
                     WHERE oa.document_fact_id = df.id
                       AND oa.user_id = :confirm_uid
                       AND oa.milestone = :confirmed_ms
                   ) AS confirmed
            FROM document_facts df
            JOIN documents d ON df.document_id = d.id
            LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
            WHERE df.category = 'obligation'
              AND df.obligation_date IS NOT NULL
              {due_filter}
              AND {circles_clause}
            ORDER BY df.obligation_date ASC, df.id
            LIMIT :limit OFFSET :offset
        """)
        rows = await self._fetch(sql, params, "obligations")
        return [_row_to_dict(r) for r in rows]

    async def is_visible(self, fact_id: int, asker_id: int | None) -> bool:
        """Whether ``fact_id`` is circle-visible to the asker — gates the
        confirm/reopen routes (404 on not-visible, same existence-oracle defense
        as the single-atom GET)."""
        circles_clause, circles_params = self._facts_circles_filter(asker_id)
        sql = text(f"""
            SELECT 1
            FROM document_facts df
            JOIN documents d ON df.document_id = d.id
            LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
            WHERE df.id = :fact_id
              AND {circles_clause}
            LIMIT 1
        """)
        rows = await self._fetch(sql, {"fact_id": fact_id, **circles_params}, "is_visible")
        return len(rows) > 0
