"""
Action Executor - Führt erkannte Intents aus

All external integrations (Home Assistant, n8n, camera, weather, search, etc.)
are executed via MCP servers. Only internal intents (knowledge/RAG, general
conversation) have dedicated handlers here.
"""
from loguru import logger


class ActionExecutor:
    """Führt Intents aus und gibt Ergebnisse zurück"""

    def __init__(self, mcp_manager=None):
        # MCP system (handles HA, n8n, camera, weather, search, news, etc.)
        self.mcp_manager = mcp_manager

    async def execute(
        self,
        intent_data: dict,
        user_permissions: list[str] | None = None,
        user_id: int | None = None,
        progress_sink=None,
    ) -> dict:
        """
        Führt einen Intent aus

        Args:
            intent_data: {
                "intent": "mcp.<namespace>.<tool>",
                "parameters": {...},
                "confidence": 0.9
            }
            user_permissions: User's permission strings for MCP access control.
                None means no auth / allow all (backwards-compatible).
            user_id: Authenticated user ID. Passed to MCP tools as user_id
                for per-user filtering (e.g. calendar visibility).
            progress_sink: Optional async `dict -> None` callback for live
                progress relay. F4c forwards federation ProgressChunks
                to the chat WebSocket through this sink. Ignored by
                non-federation paths.

        Returns:
            {
                "success": bool,
                "message": str,
                "data": {...}
            }
        """
        intent = intent_data.get("intent", "general.conversation")
        parameters = intent_data.get("parameters", {})
        confidence = intent_data.get("confidence", 0.0)

        logger.info(f"🎯 Executing intent: {intent} (confidence: {confidence:.2f})")
        logger.debug(f"Parameters: {parameters}")

        # Internal intents (no MCP equivalent)
        if intent.startswith("knowledge."):
            return await self._execute_knowledge(intent, parameters, user_id=user_id)
        elif intent == "general.conversation":
            return {
                "success": True,
                "message": "Normal conversation - no action needed",
                "action_taken": False
            }

        # Platform-owned internal tool: knowledge base RAG search.
        if intent == "internal.knowledge_search":
            from services.knowledge_tool import knowledge_search
            return await knowledge_search(parameters)

        # Other `internal.*` intents (room resolution, media playback,
        # presence, radio) live in ha_glue and are dispatched via the
        # `execute_tool` hook. Platform-only deploys without ha_glue fall
        # through to the "Unknown intent" response below, which is the
        # correct behavior.
        if intent.startswith("internal."):
            from utils.hooks import run_hooks
            hook_results = await run_hooks(
                "execute_tool",
                intent=intent,
                parameters=parameters,
                user_permissions=user_permissions,
                user_id=user_id,
            )
            if hook_results:
                return hook_results[0]
            return {
                "success": False,
                "message": f"Internal tool not available on this deploy: {intent}",
                "action_taken": False,
            }

        # MCP tool intents (mcp.* prefix — handles HA, n8n, weather, search, etc.)
        if self.mcp_manager and intent.startswith("mcp."):
            logger.info(f"🔌 Executing MCP tool: {intent}")
            # `user_id` is passed as a kwarg to `execute_tool()` (used for
            # permission checks + audit), NOT merged into `parameters`. MCP
            # tools have strict Pydantic schemas; every unknown key triggers
            # `Unexpected keyword argument` and the call fails. This was the
            # invisible-until-auth bug: when JWT auth finally returned a real
            # int user_id, every MCP call that used to succeed with
            # `user_id=None` started failing. Fix previously landed on
            # `feat/web-chat-v2` (f45c98e) but never made it to main — this
            # cherry-picks it up. See the ws-auth and /api/auth/status PRs
            # (ebongard/renfield#364 + #365 + #366) for the sibling half-merged
            # fixes from the same branch.
            return await self.mcp_manager.execute_tool(
                intent, parameters, user_permissions=user_permissions,
                user_id=user_id, progress_sink=progress_sink,
            )

        # Plugin tool dispatch — plugins can register custom tool handlers
        from utils.hooks import run_hooks
        hook_results = await run_hooks(
            "execute_tool", intent=intent, parameters=parameters,
            user_permissions=user_permissions, user_id=user_id,
        )
        if hook_results:
            return hook_results[0]

        # Unknown intent
        return {
            "success": False,
            "message": f"Unknown intent: {intent}",
            "action_taken": False
        }

    async def _execute_knowledge(
        self, intent: str, parameters: dict, user_id: int | None = None,
    ) -> dict:
        """Wissensdatenbank-Aktionen ausführen (RAG)"""
        query = parameters.get("query") or parameters.get("question") or parameters.get("text", "")

        if not query:
            return {
                "success": False,
                "message": "Keine Suchanfrage angegeben",
                "action_taken": False
            }

        try:
            from services.database import AsyncSessionLocal
            from services.rag_service import RAGService

            async with AsyncSessionLocal() as db:
                rag = RAGService(db)
                results = await rag.search(query=query, top_k=5, user_id=user_id)

            if results:
                # Build context from search results
                context_parts = []
                for r in results:
                    _sim = r.get("similarity", 0)
                    content = r.get("chunk", {}).get("content", "") if isinstance(r.get("chunk"), dict) else r.get("content", "")
                    source = r.get("document", {}).get("filename", "") if isinstance(r.get("document"), dict) else r.get("filename", "")
                    if content:
                        context_parts.append(f"[{source}] {content[:500]}")

                return {
                    "success": True,
                    "message": f"Ergebnisse aus der Wissensdatenbank ({len(results)} Treffer)",
                    "action_taken": True,
                    "data": {
                        "query": query,
                        "results_count": len(results),
                        "context": "\n\n".join(context_parts[:5])
                    }
                }
            else:
                return {
                    "success": True,
                    "message": f"Keine Ergebnisse in der Wissensdatenbank für: {query}",
                    "action_taken": True,
                    "empty_result": True,
                    "data": {"query": query, "results_count": 0}
                }

        except Exception as e:
            logger.error(f"❌ Error executing knowledge action: {e}")
            return {
                "success": False,
                "message": f"Fehler bei der Wissensdatenbank-Suche: {e!s}",
                "action_taken": False
            }
