"""
Intent Handler - Führt erkannte Intents aus
"""
from loguru import logger
from typing import Dict, Any
from integrations.homeassistant import HomeAssistantClient
from integrations.n8n import N8NClient

class IntentHandler:
    """Handler für erkannte Intents"""
    
    def __init__(self):
        self.ha_client = HomeAssistantClient()
        self.n8n_client = N8NClient()
    
    async def execute_intent(self, intent_data: Dict) -> Dict[str, Any]:
        """
        Führt einen erkannten Intent aus
        
        Returns:
            {
                "success": bool,
                "action": str,
                "result": Any,
                "message": str
            }
        """
        intent = intent_data.get("intent", "")
        parameters = intent_data.get("parameters", {})
        
        logger.info(f"🎯 Executing Intent: {intent} with params: {parameters}")
        
        # Home Assistant Intents
        if intent.startswith("homeassistant."):
            return await self._handle_homeassistant(intent, parameters)
        
        # n8n Intents
        elif intent.startswith("n8n."):
            return await self._handle_n8n(intent, parameters)
        
        # Camera Intents
        elif intent.startswith("camera."):
            return await self._handle_camera(intent, parameters)
        
        # General conversation - keine Aktion nötig
        elif intent == "general.conversation":
            return {
                "success": True,
                "action": "conversation",
                "result": None,
                "message": "Normale Konversation, keine Aktion erforderlich"
            }
        
        # Unbekannter Intent
        else:
            logger.warning(f"⚠️  Unbekannter Intent: {intent}")
            return {
                "success": False,
                "action": "unknown",
                "result": None,
                "message": f"Intent '{intent}' wird nicht unterstützt"
            }
    
    async def _handle_homeassistant(self, intent: str, parameters: Dict) -> Dict:
        """Home Assistant Intent ausführen"""
        try:
            entity_id = parameters.get("entity_id")
            
            if not entity_id:
                return {
                    "success": False,
                    "action": intent,
                    "result": None,
                    "message": "Keine Entity ID angegeben"
                }
            
            # turn_on
            if intent == "homeassistant.turn_on":
                success = await self.ha_client.turn_on(entity_id)
                return {
                    "success": success,
                    "action": "turn_on",
                    "result": {"entity_id": entity_id},
                    "message": f"Gerät {entity_id} {'eingeschaltet' if success else 'konnte nicht eingeschaltet werden'}"
                }
            
            # turn_off
            elif intent == "homeassistant.turn_off":
                success = await self.ha_client.turn_off(entity_id)
                return {
                    "success": success,
                    "action": "turn_off",
                    "result": {"entity_id": entity_id},
                    "message": f"Gerät {entity_id} {'ausgeschaltet' if success else 'konnte nicht ausgeschaltet werden'}"
                }
            
            # toggle
            elif intent == "homeassistant.toggle":
                success = await self.ha_client.toggle(entity_id)
                return {
                    "success": success,
                    "action": "toggle",
                    "result": {"entity_id": entity_id},
                    "message": f"Gerät {entity_id} {'umgeschaltet' if success else 'konnte nicht umgeschaltet werden'}"
                }
            
            # get_state
            elif intent == "homeassistant.get_state":
                state = await self.ha_client.get_state(entity_id)
                if state:
                    return {
                        "success": True,
                        "action": "get_state",
                        "result": state,
                        "message": f"Status von {entity_id}: {state.get('state')}"
                    }
                else:
                    return {
                        "success": False,
                        "action": "get_state",
                        "result": None,
                        "message": f"Status von {entity_id} konnte nicht abgerufen werden"
                    }
            
            # set_value
            elif intent == "homeassistant.set_value":
                value = parameters.get("value")
                attribute = parameters.get("attribute", "value")
                success = await self.ha_client.set_value(entity_id, value, attribute)
                return {
                    "success": success,
                    "action": "set_value",
                    "result": {"entity_id": entity_id, "value": value},
                    "message": f"Wert von {entity_id} {'gesetzt' if success else 'konnte nicht gesetzt werden'}"
                }
            
        except Exception as e:
            logger.error(f"❌ Home Assistant Fehler: {e}")
            return {
                "success": False,
                "action": intent,
                "result": None,
                "message": f"Fehler: {str(e)}"
            }
    
    async def _handle_n8n(self, intent: str, parameters: Dict) -> Dict:
        """n8n Intent ausführen"""
        try:
            if intent == "n8n.trigger_workflow":
                workflow_id = parameters.get("workflow_id") or parameters.get("workflow_name")
                data = parameters.get("data", {})
                
                result = await self.n8n_client.trigger_workflow(workflow_id, data)
                
                return {
                    "success": result.get("success", False),
                    "action": "trigger_workflow",
                    "result": result,
                    "message": f"Workflow {workflow_id} {'getriggert' if result.get('success') else 'konnte nicht getriggert werden'}"
                }
        except Exception as e:
            logger.error(f"❌ n8n Fehler: {e}")
            return {
                "success": False,
                "action": intent,
                "result": None,
                "message": f"Fehler: {str(e)}"
            }
    
    async def _handle_camera(self, intent: str, parameters: Dict) -> Dict:
        """Camera Intent ausführen"""
        # Hier würden Kamera-Aktionen implementiert
        return {
            "success": True,
            "action": intent,
            "result": None,
            "message": "Kamera-Intents noch nicht implementiert"
        }
