"""
Memory List Tool — Platform-owned agent tool.

`internal.list_my_memories` enumerates the CURRENT user's own stored
conversation memories (preferences / facts / instructions) WITHOUT the vector
similarity threshold the per-turn `{memory_context}` injection uses.

Why this exists: the only memory the agent normally sees is the ~6-row snapshot
built by `chat_handler._retrieve_memory_context` (essential top-3 by importance
≥0.9 ∪ semantic top-3 vs the current message). A broad self-knowledge query
like "Was weißt du über mich?" embeds poorly against specific facts ("mein
Lieblingsobst ist Mango", "wohnt in Korschenbroich"), so those drop below the
0.5 cosine floor and never reach the LLM. This tool gives the agent an on-demand
read of the full memory store for exactly those enumeration questions.

Mirrors `services/knowledge_tool.py`: a flattened tool definition registered by
`agent_tools._register_internal_tools` + an async handler dispatched as a special
case in `action_executor` (which injects the authenticated `user_id`).
"""
from __future__ import annotations

from loguru import logger

from models.database import MEMORY_CATEGORIES
from utils.log_safety import error_text_without_content

MEMORY_LIST_DEFAULT_LIMIT = 100
MEMORY_LIST_MAX_LIMIT = 200
# Derive the valid-category allowlist from the canonical constant rather than
# hardcoding it — a hardcoded subset silently drops a real category (it omitted
# "procedural"), turning a `category=procedural` filter into an unfiltered
# "all memories" read. Importing the source of truth makes any future category
# work automatically.
_VALID_CATEGORIES = set(MEMORY_CATEGORIES)

# Registered with the agent tool registry by
# `services/agent_tools.py::_register_internal_tools()`.
MEMORY_LIST_TOOL: dict = {
    "internal.list_my_memories": {
        "description": (
            "List what you have remembered about the CURRENT user — their stored "
            "preferences, facts, and instructions — WITHOUT semantic filtering. "
            "Use this for broad self-knowledge questions ('Was weißt du über mich?', "
            "'list everything you remember about me', 'what are my preferences?') "
            "where the small auto-injected memory snapshot is not enough. Returns "
            "the user's memories newest-first with their category. Optional "
            "'category' filter (preference|fact|context|instruction|procedural)."
        ),
        "parameters": {
            "category": "Optional category filter: preference, fact, context, instruction, or procedural (optional)",
            "limit": "Max memories to return (optional; default 100, max 200)",
        },
    },
}


async def list_my_memories(params: dict, user_id: int | None = None) -> dict:
    """Enumerate the asker's own conversation memories (no vector threshold).

    `user_id` is injected by `action_executor` from the authenticated context —
    NOT taken from the LLM-supplied params (the model can't know its own id, and
    this tool must only ever read the asker's own memories).
    """
    category = (params.get("category") or "").strip().lower() or None
    if category and category not in _VALID_CATEGORIES:
        category = None  # ignore an invalid filter rather than erroring out

    limit = MEMORY_LIST_DEFAULT_LIMIT
    if params.get("limit"):
        try:
            limit = max(1, min(MEMORY_LIST_MAX_LIMIT, int(params["limit"])))
        except (ValueError, TypeError):
            pass

    try:
        from services.conversation_memory_service import ConversationMemoryService
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            service = ConversationMemoryService(db)
            # Resolve the owner the same way save()/list do — handles single-user
            # mode (user_id is None → the sole user) so we never read across users.
            owner_id = await service._resolve_owner_user_id(user_id)
            if owner_id is None:
                return {
                    "success": True,
                    "message": "No user context available for memory lookup",
                    "action_taken": True,
                    "empty_result": True,
                    "data": {"results_count": 0},
                }
            memories = await service.list_for_user(
                user_id=owner_id, category=category, limit=limit,
            )
            total = await service.get_count(user_id=owner_id, category=category)

        if not memories:
            scope = f" in category '{category}'" if category else ""
            return {
                "success": True,
                "message": f"No stored memories for this user{scope}",
                "action_taken": True,
                "empty_result": True,
                "data": {"results_count": 0, "total": 0, "category": category},
            }

        lines = [f"- [{m['category']}] {m['content']}" for m in memories]
        truncated = total > len(memories)
        suffix = f" (showing {len(memories)} of {total}; pass a higher limit for more)" if truncated else ""
        return {
            "success": True,
            "message": f"{len(memories)} stored memories{suffix}",
            "action_taken": True,
            "data": {
                "results_count": len(memories),
                "total": total,
                "truncated": truncated,
                "category": category,
                "context": "\n".join(lines),
            },
        }
    except Exception as e:
        logger.error(f"Error in list_my_memories: {error_text_without_content(e)}")
        return {
            "success": False,
            "message": f"Memory list error: {e!s}",
            "action_taken": False,
        }
