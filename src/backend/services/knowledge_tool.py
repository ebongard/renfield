"""
Knowledge Base Tool — Platform-owned agent tool.

Standalone `internal.knowledge_search` implementation that runs on the
platform (not ha_glue). Exposes a minimal tool definition + async handler
so the agent loop can call it without depending on the ha_glue internal
tools stack.

Phase 1 Week 4 cleanup: before this module existed, `_knowledge_search`
lived inside `services/internal_tools.py::InternalToolService` along
with ~17 ha-glue-only methods (room resolution, media playback,
presence, radio). The whole file was pinned on the W4.2 platform →
ha_glue boundary allowlist. Splitting knowledge_search out lets the
rest of `InternalToolService` move into `ha_glue/services/` cleanly,
and removes the last pending entry from ALLOWED_IMPORTERS.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from utils.config import settings

# Tool definition — registered with the agent tool registry by
# `services/agent_tools.py::_register_internal_tools()`. The name
# keeps the `internal.` prefix so existing agent prompts and routing
# rules stay valid without a coordinated rename.
KNOWLEDGE_TOOL: dict = {
    "internal.knowledge_search": {
        "description": (
            "Search the user's local knowledge base (uploaded documents, "
            "invoices, contracts) by semantic similarity. Returns matching "
            "text passages AND precise extracted facts (Steuernummer, IBAN, "
            "issuer, obligations/Fristen) with source document info."
        ),
        "parameters": {
            "query": "Search query (required)",
            "top_k": "Maximum number of results to return (optional, default: from server config)",
        },
    },
}

# Facts are short, precise, and few — a small cap keeps the injected context
# tight and never lets the fact block crowd out the retrieved passages.
_FACT_SEARCH_TOP_K = 6


def _format_fact_line(fact: dict[str, Any], doc_title: str) -> str:
    """One human-readable line for a Schicht A fact, for the injected context.

    ``kind`` (e.g. ``steuernummer``, ``zahlungsfrist``) is already a meaningful
    label; fall back to ``category`` then a generic word. Date + amount are
    appended when present so an obligation reads as a due-date, not a bare value.
    """
    label = (fact.get("kind") or fact.get("category") or "Fakt").strip()
    value = (fact.get("value") or fact.get("normalized_value") or "").strip()
    parts = [f"{label}: {value}" if value else label]
    if fact.get("obligation_date"):
        parts.append(f"Frist {fact['obligation_date']}")
    amount = fact.get("amount_value")
    if amount is not None:
        currency = fact.get("amount_currency") or ""
        parts.append(f"Betrag {amount} {currency}".strip())
    return f"- {', '.join(parts)} — Quelle: {doc_title}"


async def _retrieve_facts(
    db: Any, query: str, user_id_int: int | None
) -> tuple[list[dict], dict[Any, dict]]:
    """Circle-filtered Schicht A fact search + source-document titles.

    Best-effort throughout: the chunk results are the primary answer, so a fact
    query or title-lookup failure degrades to ``([], {})`` / generic titles and
    never fails the knowledge search. Returns the fact rows plus a
    ``document_id → {title, filename, tier}`` map for the fact provenance chips.
    """
    try:
        from services.document_fact_retrieval import DocumentFactRetrieval

        facts = await DocumentFactRetrieval(db).search(
            query, asker_id=user_id_int, top_k=_FACT_SEARCH_TOP_K,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"knowledge_search: fact retrieval failed (ignored): {e}")
        return [], {}

    doc_ids = {f["document_id"] for f in facts if f.get("document_id") is not None}
    doc_meta: dict[Any, dict] = {}
    if doc_ids:
        try:
            doc_meta = await _visible_document_meta(db, doc_ids, user_id_int)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"knowledge_search: fact source-title lookup failed (ignored): {e}"
            )
    return facts, doc_meta


async def _visible_document_meta(
    db: Any, doc_ids: set, user_id_int: int | None
) -> dict[Any, dict]:
    """``document_id → {title, filename, tier}`` for ONLY the documents the asker
    may see themselves — CIRCLE-FILTERED, not a raw id lookup.

    A per-fact tier override (``tier_overridden``) is the one place "fact visible"
    and "document visible" diverge: a fact re-tiered to ``public`` on an otherwise
    private document is returned to an outsider by ``document_facts_circles_filter``
    (it keys the tier branches on ``df.circle_tier``), but the parent document's
    ``title``/``filename`` (user-supplied, may name people / case numbers) is NOT
    the outsider's to see. Filtering the title lookup through the DOCUMENT's own
    circle policy keeps the leaked-metadata window closed; a non-visible source
    document simply falls back to a generic ``Dokument {id}`` label with no chip.
    """
    from sqlalchemy import text

    from models.database import TIER_PUBLIC
    from services.circle_sql import circles_filter_clause, circles_filter_params

    if not settings.auth_enabled:
        doc_clause, doc_params = "TRUE", {}
    elif user_id_int is None:
        # Anonymous authed caller → public documents only.
        doc_clause, doc_params = "d.circle_tier = :doc_pub", {"doc_pub": TIER_PUBLIC}
    else:
        doc_clause = circles_filter_clause(
            table_alias="d",
            owner_col="owner_id",
            tier_col="circle_tier",
            source_table_value="documents",
            owner_table_alias="kb",
            source_id_expr="d.id",
            owner_atom_id_expr="d.atom_id",
        )
        doc_params = circles_filter_params(user_id_int, source_table_value="documents")

    sql = text(f"""
        SELECT d.id AS id, d.generated_title AS generated_title, d.title AS title,
               d.filename AS filename, d.circle_tier AS circle_tier
        FROM documents d
        LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
        WHERE d.id = ANY(:ids) AND ({doc_clause})
    """)
    rows = (await db.execute(sql, {"ids": list(doc_ids), **doc_params})).all()
    return {
        r.id: {
            "title": r.generated_title or r.title or r.filename or f"Dokument {r.id}",
            "filename": r.filename or "",
            "tier": r.circle_tier,
        }
        for r in rows
    }


async def knowledge_search(params: dict) -> dict:
    """Search the local knowledge base (RAG) by semantic similarity.

    Pure platform operation — hits `services.rag_service.RAGService`
    with a fresh DB session. No ha_glue dependencies.
    """
    query = (params.get("query") or "").strip()
    if not query:
        return {
            "success": False,
            "message": "Parameter 'query' is required",
            "action_taken": False,
        }

    top_k = None
    if params.get("top_k"):
        try:
            top_k = int(params["top_k"])
        except (ValueError, TypeError):
            pass

    try:
        from services.database import AsyncSessionLocal
        from services.rag_service import RAGService

        # FastMCP injects `user_id` into the params dict from the auth context;
        # passing it through pins the RAG search to that user's circle reach.
        user_id_raw = params.get("user_id")
        try:
            user_id_int = int(user_id_raw) if user_id_raw is not None else None
        except (TypeError, ValueError):
            user_id_int = None

        async with AsyncSessionLocal() as db:
            rag = RAGService(db)
            results = await rag.search(query=query, top_k=top_k, user_id=user_id_int)

            # Schicht A document facts — precise extracted values (Steuernummer,
            # IBAN, issuer, obligation Fristen) that the chunk path can't answer
            # crisply ("what's my Steuernummer" retrieves the passage, not the
            # normalized identifier). Same circle reach as the RAG search
            # (DocumentFactRetrieval applies the parent-Document 4-branch filter),
            # so a returned fact is one the asker may see (the parent-document
            # title lookup below is separately circle-filtered — a fact can be
            # visible via a tier override while its document is not). Gated on the
            # extractor flag: with it off no facts exist, so we skip the query
            # entirely; the context/message strings are then identical to the
            # chunk-only path (the data dict carries facts_count=0 + facts=[]
            # regardless — additive, no existing consumer breaks).
            facts: list[dict] = []
            doc_meta: dict[Any, dict] = {}
            if settings.schicht_a_extraction_enabled:
                facts, doc_meta = await _retrieve_facts(db, query, user_id_int)

        context_parts: list[str] = []
        # Structured per-document provenance for the chat "source chips" UI.
        # rag.search already circle-filtered by user_id, so these only contain
        # documents the asker may see — no extra permission check needed.
        # Deduped by document_id (one chip per source document, not per chunk).
        sources: list[dict] = []
        seen_doc_ids: set = set()
        for r in results:
            doc = r.get("document") if isinstance(r.get("document"), dict) else {}
            content = (
                r.get("chunk", {}).get("content", "")
                if isinstance(r.get("chunk"), dict)
                else r.get("content", "")
            )
            source = doc.get("filename", "") or r.get("filename", "")
            if content:
                chunk_cap = settings.knowledge_context_chunk_chars
                context_parts.append(f"[{source}] {content[:chunk_cap]}")

            doc_id = doc.get("id")
            if doc_id is not None and doc_id not in seen_doc_ids:
                seen_doc_ids.add(doc_id)
                sources.append({
                    "document_id": doc_id,
                    "filename": source,
                    "title": doc.get("title") or source or f"Dokument {doc_id}",
                    "tier": doc.get("circle_tier"),
                })

        # Fold facts into the context (a FAKTEN block the agent prefers when it
        # needs the precise value) + into the provenance chips (fact-only source
        # documents that produced no chunk hit still get a chip).
        fact_lines: list[str] = []
        fact_struct: list[dict] = []
        for f in facts:
            did = f.get("document_id")
            title = (doc_meta.get(did) or {}).get("title") or f"Dokument {did}"
            fact_lines.append(_format_fact_line(f, title))
            fact_struct.append({
                "document_id": did,
                "category": f.get("category"),
                "kind": f.get("kind"),
                "value": f.get("value") or f.get("normalized_value"),
                "obligation_date": f.get("obligation_date"),
                "amount_value": f.get("amount_value"),
                "amount_currency": f.get("amount_currency"),
            })
            # Chip a fact-source document ONLY when it is in doc_meta, i.e. the
            # asker may see the document itself. A tier-overridden fact on an
            # otherwise-private document is visible without the document being
            # visible — chipping it would leak the private title/filename (and
            # deep-link to a doc the asker gets 403 on). The fact still appears in
            # the context under a generic "Dokument {id}" Quelle.
            if did is not None and did not in seen_doc_ids and did in doc_meta:
                seen_doc_ids.add(did)
                meta = doc_meta[did]
                sources.append({
                    "document_id": did,
                    "filename": meta.get("filename", ""),
                    "title": meta.get("title") or f"Dokument {did}",
                    "tier": meta.get("tier"),
                })

        if results or facts:
            # Prefix the passages block only when facts share the context, so the
            # flag-off / facts-empty output stays byte-identical to before.
            if fact_lines:
                blocks = ["FAKTEN (präzise extrahierte Werte):\n" + "\n".join(fact_lines)]
                if context_parts:
                    blocks.append("PASSAGEN:\n" + "\n\n".join(context_parts))
                context = "\n\n".join(blocks)
            else:
                context = "\n\n".join(context_parts)

            message = f"Knowledge base results ({len(results)} hits"
            message += f", {len(facts)} facts)" if facts else ")"
            return {
                "success": True,
                "message": message,
                "action_taken": True,
                "data": {
                    "query": query,
                    "results_count": len(results),
                    "facts_count": len(facts),
                    "context": context,
                    "sources": sources,
                    "facts": fact_struct,
                },
            }
        return {
            "success": True,
            "message": f"No results in knowledge base for: {query}",
            "action_taken": True,
            "empty_result": True,
            "data": {"query": query, "results_count": 0},
        }
    except Exception as e:
        logger.error(f"Error in knowledge_search: {e}")
        return {
            "success": False,
            "message": f"Knowledge base search error: {e!s}",
            "action_taken": False,
        }
