"""Ranked hybrid DOCUMENT search — returns Document rows (not chunks).

Powers ``GET /api/knowledge/documents?q=`` so a document is reachable by NAME or
CONTENT regardless of the 100-newest recency window the plain list shows. Three
signals propose candidates, reciprocal-rank-fusion merges them, ONE circle
visibility gate is applied to the fused ids (D2 — search is circle-correct), and
the surviving ids are fetched as ``Document`` rows in ranked order:

    query
      │
      ├── NAME    documents.search_vector FTS (ts_rank) + ILIKE partial fallback
      ├── FACTS   DocumentFactRetrieval.search  (Schicht-A facts → doc ids)
      └── CHUNKS  RAGRetrieval.search           (semantic chunks → best per doc)
                         │
                    RRF fuse (k = rag_hybrid_rrf_k)
                         │
             circle-visibility gate (single enforcement point)
                         │
             fetch Documents by id, ranked order, [offset:offset+limit]

Signals over-propose freely; the circle gate on the FUSED ids is the one place
visibility is enforced, so the NAME signal (FTS + ILIKE, not circle-filtered on
its own) can never leak a hidden document. Facts/chunks are additionally
circle-filtered in their own services — a strict subset, so they pass the gate.

Empty/blank query → ``[]`` (the caller falls back to the recency list). Any
single signal that errors degrades to the others (best-effort).
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document
from services.circle_sql import document_chunks_circles_filter
from services.fts_languages import build_tsquery_union_sql
from utils.config import settings

# Per-signal candidate cap fed into RRF (generous: RRF needs only relative order,
# and a KB is at most a few thousand docs).
_CANDIDATES = 200


def _is_postgres(db: AsyncSession) -> bool:
    bind = db.get_bind()
    return bool(bind is not None and bind.dialect.name == "postgresql")


async def _name_candidates(
    db: AsyncSession, q: str, kb_id: int | None, status: str | None
) -> list[int]:
    """Documents whose generated_title/title/filename match — FTS-ranked
    (Postgres) with an ILIKE partial-token fallback appended, or ILIKE-only on
    sqlite. NOT circle-filtered here (the fused-id gate enforces visibility)."""
    fts_ids: list[int] = []
    if _is_postgres(db):
        try:
            tsq = build_tsquery_union_sql("q")  # websearch_to_tsquery union on :q
            params: dict[str, Any] = {"q": q, "n": _CANDIDATES}
            where = [f"d.search_vector @@ ({tsq})"]
            if kb_id is not None:
                where.append("d.knowledge_base_id = :kb"); params["kb"] = kb_id
            if status:
                where.append("d.status = :status"); params["status"] = status
            sql = text(
                "SELECT d.id FROM documents d "
                f"WHERE {' AND '.join(where)} "
                f"ORDER BY ts_rank(d.search_vector, ({tsq})) DESC LIMIT :n"
            )
            fts_ids = [r[0] for r in (await db.execute(sql, params)).fetchall()]
        except Exception as e:  # noqa: BLE001 — a signal never fails the search
            logger.warning(f"document_search: name FTS failed: {e}")
            fts_ids = []
    ilike_ids = await _name_ilike(db, q, kb_id, status)
    seen = set(fts_ids)
    return fts_ids + [i for i in ilike_ids if i not in seen]


async def _name_ilike(
    db: AsyncSession, q: str, kb_id: int | None, status: str | None
) -> list[int]:
    like = f"%{q}%"
    stmt = select(Document.id).where(
        or_(
            Document.generated_title.ilike(like),
            Document.title.ilike(like),
            Document.filename.ilike(like),
        )
    )
    if kb_id is not None:
        stmt = stmt.where(Document.knowledge_base_id == kb_id)
    if status:
        stmt = stmt.where(Document.status == status)
    stmt = stmt.order_by(Document.created_at.desc()).limit(_CANDIDATES)
    return [r[0] for r in (await db.execute(stmt)).all()]


async def _fact_candidates(
    db: AsyncSession, q: str, asker_id: int | None, enforce_circles: bool
) -> list[int]:
    try:
        from services.document_fact_retrieval import DocumentFactRetrieval

        hits = await DocumentFactRetrieval(db).search(
            q, asker_id=asker_id, top_k=_CANDIDATES, enforce_circles=enforce_circles
        )
        return _dedup_doc_ids(hits)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"document_search: fact signal failed: {e}")
        return []


async def _chunk_candidates(
    db: AsyncSession, q: str, asker_id: int | None, kb_id: int | None
) -> list[int]:
    try:
        from services.rag_retrieval import RAGRetrieval

        hits = await RAGRetrieval(db).search(
            q, top_k=_CANDIDATES, knowledge_base_id=kb_id, user_id=asker_id
        )
        return _dedup_doc_ids(hits)  # best-first by similarity; first hit per doc
    except Exception as e:  # noqa: BLE001
        logger.warning(f"document_search: chunk signal failed: {e}")
        return []


def _dedup_doc_ids(hits: list[dict[str, Any]]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for h in hits:
        did = h.get("document_id")
        if isinstance(did, int) and did not in seen:
            seen.add(did); out.append(did)
    return out


def _rrf(ranked_lists: list[list[int]], k: int) -> list[int]:
    """Reciprocal-rank fusion: score = Σ 1/(k + rank) across the ranked id lists."""
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst):
            # 1-based rank, matching rag_retrieval._reciprocal_rank_fusion.
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


async def _visible_ids(db: AsyncSession, ids: list[int], asker_id: int | None) -> set[int]:
    """The subset of ``ids`` the asker may see under circles — the single
    enforcement gate (D2). Postgres only; on sqlite / auth-off the caller skips
    the gate entirely (see search_documents)."""
    if not ids:
        return set()
    clause, params = document_chunks_circles_filter(
        asker_id if asker_id is not None else 0, chunk_alias="d", doc_alias="d", kb_alias="kb"
    )
    params["ids"] = ids
    sql = text(
        "SELECT d.id FROM documents d "
        "LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id "
        f"WHERE d.id = ANY(:ids) AND ({clause})"
    )
    rows = (await db.execute(sql, params)).fetchall()
    return {r[0] for r in rows}


async def search_documents(
    db: AsyncSession,
    query: str,
    *,
    asker_id: int | None,
    knowledge_base_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    enforce_circles: bool = True,
) -> list[Document]:
    """Ranked hybrid document search. Returns ``Document`` rows in RRF order,
    sliced to ``[offset:offset+limit]``. Empty query → ``[]`` (caller lists by
    recency instead)."""
    q = (query or "").strip()
    if not q:
        return []

    # The NAME signal (FTS + ILIKE) is NOT circle-filtered on its own — its safety
    # depends on the fused-id circle gate below, which is Postgres-only. So only
    # let NAME contribute when either circles aren't enforced OR we can run the
    # gate (Postgres). This makes a leak structurally impossible even in an
    # (unsupported) auth-on-against-sqlite config. FACTS/CHUNKS are circle-filtered
    # in-service, so they always contribute.
    gate_available = (not enforce_circles) or _is_postgres(db)
    name_ids = await _name_candidates(db, q, knowledge_base_id, status) if gate_available else []
    fact_ids = await _fact_candidates(db, q, asker_id, enforce_circles)
    chunk_ids = await _chunk_candidates(db, q, asker_id, knowledge_base_id)

    fused = _rrf([name_ids, fact_ids, chunk_ids], k=settings.rag_hybrid_rrf_k)
    if not fused:
        return []

    # Single circle-visibility gate over the fused ids (Postgres only; auth-off /
    # sqlite sees everything). This is the one place NAME-signal visibility is
    # enforced, so the un-circle-filtered FTS/ILIKE can't leak a hidden document.
    if enforce_circles and _is_postgres(db):
        visible = await _visible_ids(db, fused, asker_id)
        fused = [did for did in fused if did in visible]

    window = fused[offset : offset + limit]
    if not window:
        return []
    # The single chokepoint: exclude superseded docs here (a KB near-dup loser
    # resolved as 'supersede', #1170) regardless of which signal proposed the id.
    rows = (
        await db.execute(
            select(Document).where(
                Document.id.in_(window),
                Document.superseded_by_document_id.is_(None),
            )
        )
    ).scalars().all()
    by_id = {d.id: d for d in rows}
    ordered: list[Document] = []
    for did in window:
        d = by_id.get(did)
        if d is None:
            continue
        # A fact/chunk hit can live in another KB/status — re-apply at fetch.
        if knowledge_base_id is not None and d.knowledge_base_id != knowledge_base_id:
            continue
        if status and d.status != status:
            continue
        ordered.append(d)
    return ordered
