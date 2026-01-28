"""
Action Executor - Führt erkannte Intents aus
"""
from typing import Dict, Optional, TYPE_CHECKING
from loguru import logger
from integrations.homeassistant import HomeAssistantClient
from integrations.n8n import N8NClient

if TYPE_CHECKING:
    from models.database import User

class ActionExecutor:
    """Führt Intents aus und gibt Ergebnisse zurück"""

    def __init__(self, plugin_registry=None, mcp_manager=None):
        self.ha_client = HomeAssistantClient()
        self.n8n_client = N8NClient()

        # Plugin system
        self.plugin_registry = plugin_registry

        # MCP system
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

    async def execute(self, intent_data: Dict, user: Optional["User"] = None) -> Dict:
        """
        Führt einen Intent aus

        Args:
            intent_data: {
                "intent": "homeassistant.turn_on",
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

        # Routing basierend auf Intent (Core intents first - backward compatibility)
        if intent.startswith("homeassistant."):
            return await self._execute_homeassistant(intent, parameters)
        elif intent.startswith("n8n."):
            return await self._execute_n8n(intent, parameters)
        elif intent.startswith("camera."):
            return await self._execute_camera(intent, parameters)
        elif intent == "general.conversation":
            return {
                "success": True,
                "message": "Normal conversation - no action needed",
                "action_taken": False
            }

        # MCP tool intents (mcp.* prefix)
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
    
    async def _execute_homeassistant(self, intent: str, parameters: Dict) -> Dict:
        """Home Assistant Aktionen ausführen"""
        entity_id = parameters.get("entity_id")
        
        if not entity_id:
            # Versuche Entity aus Namen zu finden
            query = parameters.get("name") or parameters.get("device") or parameters.get("location")
            if query:
                entities = await self.ha_client.search_entities(query)
                if entities:
                    entity_id = entities[0]["entity_id"]
                    logger.info(f"🔍 Found entity: {entity_id} for query '{query}'")
                else:
                    return {
                        "success": False,
                        "message": f"Konnte kein Gerät mit Namen '{query}' finden",
                        "action_taken": False
                    }
        
        # Intent-spezifische Aktionen
        try:
            if intent == "homeassistant.turn_on":
                success = await self.ha_client.turn_on(entity_id)
                state = await self.ha_client.get_state(entity_id)
                return {
                    "success": success,
                    "message": f"{state.get('attributes', {}).get('friendly_name', entity_id)} ist jetzt eingeschaltet",
                    "action_taken": True,
                    "entity_id": entity_id,
                    "state": state.get("state")
                }
            
            elif intent == "homeassistant.turn_off":
                success = await self.ha_client.turn_off(entity_id)
                state = await self.ha_client.get_state(entity_id)
                return {
                    "success": success,
                    "message": f"{state.get('attributes', {}).get('friendly_name', entity_id)} ist jetzt ausgeschaltet",
                    "action_taken": True,
                    "entity_id": entity_id,
                    "state": state.get("state")
                }
            
            elif intent == "homeassistant.toggle":
                success = await self.ha_client.toggle(entity_id)
                state = await self.ha_client.get_state(entity_id)
                return {
                    "success": success,
                    "message": f"{state.get('attributes', {}).get('friendly_name', entity_id)} wurde umgeschaltet",
                    "action_taken": True,
                    "entity_id": entity_id,
                    "state": state.get("state")
                }
            
            elif intent == "homeassistant.get_state" or intent == "homeassistant.check_state":
                state = await self.ha_client.get_state(entity_id)
                if state:
                    friendly_name = state.get('attributes', {}).get('friendly_name', entity_id)
                    current_state = state.get('state')
                    
                    # Übersetze State
                    state_text = self._translate_state(current_state)
                    
                    return {
                        "success": True,
                        "message": f"{friendly_name} ist {state_text}",
                        "action_taken": True,
                        "entity_id": entity_id,
                        "state": current_state,
                        "attributes": state.get('attributes', {})
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Konnte Status von {entity_id} nicht abrufen",
                        "action_taken": False
                    }
            
            elif intent == "homeassistant.set_value":
                value = parameters.get("value")
                attribute = parameters.get("attribute", "value")
                success = await self.ha_client.set_value(entity_id, value, attribute)
                return {
                    "success": success,
                    "message": f"{attribute} wurde auf {value} gesetzt",
                    "action_taken": True,
                    "entity_id": entity_id
                }
            
            else:
                return {
                    "success": False,
                    "message": f"Unbekannter Home Assistant Intent: {intent}",
                    "action_taken": False
                }
        
        except Exception as e:
            logger.error(f"❌ Error executing Home Assistant action: {e}")
            return {
                "success": False,
                "message": f"Fehler bei der Ausführung: {str(e)}",
                "action_taken": False
            }
    
    async def _execute_n8n(self, intent: str, parameters: Dict) -> Dict:
        """n8n Workflows ausführen"""
        workflow_id = parameters.get("workflow_id") or parameters.get("workflow_name")
        data = parameters.get("data", {})
        
        try:
            result = await self.n8n_client.trigger_workflow(workflow_id, data)
            return {
                "success": result.get("success", False),
                "message": f"Workflow {workflow_id} wurde getriggert",
                "action_taken": True,
                "workflow_result": result
            }
        except Exception as e:
            logger.error(f"❌ Error executing n8n workflow: {e}")
            return {
                "success": False,
                "message": f"Fehler beim Triggern des Workflows: {str(e)}",
                "action_taken": False
            }
    
    async def _execute_camera(self, intent: str, parameters: Dict) -> Dict:
        """Kamera-Aktionen ausführen"""
        return {
            "success": True,
            "message": "Kamera-Aktion würde hier ausgeführt",
            "action_taken": False
        }
    
    def _translate_state(self, state: str) -> str:
        """Übersetze technischen State in natürliche Sprache"""
        translations = {
            "on": "eingeschaltet",
            "off": "ausgeschaltet",
            "open": "offen",
            "closed": "geschlossen",
            "locked": "verschlossen",
            "unlocked": "entriegelt",
            "home": "zuhause",
            "away": "abwesend",
            "playing": "läuft",
            "paused": "pausiert",
            "idle": "inaktiv"
        }
        return translations.get(state.lower(), state)
