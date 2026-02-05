"""
Action Executor - Führt erkannte Intents aus

All external integrations (Home Assistant, n8n, camera, weather, search, etc.)
are executed via MCP servers. Only internal intents (knowledge/RAG, general
conversation) and legacy plugins have dedicated handlers here.
"""
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from models.database import User

class ActionExecutor:
    """Führt Intents aus und gibt Ergebnisse zurück"""

    def __init__(self, plugin_registry=None, mcp_manager=None):
        # Plugin system
        self.plugin_registry = plugin_registry

        # MCP system (handles HA, n8n, camera, weather, search, news, etc.)
        self.mcp_manager = mcp_manager

    def _check_plugin_permission(self, intent: str, user: Optional["User"]) -> tuple[bool, str]:
        """
        Check if user has permission to execute a plugin intent.

        Args:
            intent: The plugin intent (e.g., "weather.get_current")
            user: The user making the request (None if auth disabled)

        Returns:
            Tuple of (allowed, error_message)
        """
        # If no user context (auth disabled), allow
        if user is None:
            return True, ""

        # Extract plugin name from intent (e.g., "weather" from "weather.get_current")
        plugin_name = intent.split(".")[0] if "." in intent else intent

        # Check if user can use this plugin
        if not user.can_use_plugin(plugin_name):
            logger.warning(f"🚫 User {user.username} denied plugin access: {plugin_name}")
            return False, f"No permission to use plugin: {plugin_name}"

        return True, ""

    async def execute(self, intent_data: dict, user: Optional["User"] = None) -> dict:
        """
        Führt einen Intent aus

        Args:
            intent_data: {
                "intent": "mcp.homeassistant.turn_on",
                "parameters": {...},
                "confidence": 0.9
            }
            user: Optional user context for permission checks

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
            return await self._execute_knowledge(intent, parameters)
        elif intent == "general.conversation":
            return {
                "success": True,
                "message": "Normal conversation - no action needed",
                "action_taken": False
            }

        # Internal agent tools (room resolution, media playback)
        if intent.startswith("internal."):
            from services.internal_tools import InternalToolService
            internal_tools = InternalToolService()
            return await internal_tools.execute(intent, parameters)

        # MCP tool intents (mcp.* prefix — handles HA, n8n, weather, search, etc.)
        if self.mcp_manager and intent.startswith("mcp."):
            logger.info(f"🔌 Executing MCP tool: {intent}")
            return await self.mcp_manager.execute_tool(intent, parameters)

        # Plugin intents - check permission first
        if self.plugin_registry:
            plugin = self.plugin_registry.get_plugin_for_intent(intent)
            if plugin:
                # Check plugin permission
                allowed, error = self._check_plugin_permission(intent, user)
                if not allowed:
                    return {
                        "success": False,
                        "message": error,
                        "action_taken": False,
                        "error_code": "permission_denied"
                    }

                logger.info(f"🔌 Executing plugin intent: {intent}")
                return await plugin.execute(intent, parameters)

        # Unknown intent
        return {
            "success": False,
            "message": f"Unknown intent: {intent}",
            "action_taken": False
        }

    async def _execute_knowledge(self, intent: str, parameters: dict) -> dict:
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
                results = await rag.search(query=query, top_k=5)

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
