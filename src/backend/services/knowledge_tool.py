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

from loguru import logger

# Tool definition — registered with the agent tool registry by
# `services/agent_tools.py::_register_internal_tools()`. The name
# keeps the `internal.` prefix so existing agent prompts and routing
# rules stay valid without a coordinated rename.
KNOWLEDGE_TOOL: dict = {
    "internal.knowledge_search": {
        "description": (
            "Search the user's local knowledge base (uploaded documents, "
            "invoices, contracts) by semantic similarity. Returns matching "
            "text passages with source document info."
        ),
        "parameters": {
            "query": "Search query (required)",
            "top_k": "Maximum number of results to return (optional, default: from server config)",
        },
    },
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

        if results:
            context_parts = []
            for r in results:
                content = (
                    r.get("chunk", {}).get("content", "")
                    if isinstance(r.get("chunk"), dict)
                    else r.get("content", "")
                )
                source = (
                    r.get("document", {}).get("filename", "")
                    if isinstance(r.get("document"), dict)
                    else r.get("filename", "")
                )
                if content:
                    context_parts.append(f"[{source}] {content[:500]}")

            return {
                "success": True,
                "message": f"Knowledge base results ({len(results)} hits)",
                "action_taken": True,
                "data": {
                    "query": query,
                    "results_count": len(results),
                    "context": "\n\n".join(context_parts),
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
