"""
n8n Integration
"""
import httpx
from typing import Dict, Optional
from loguru import logger
from utils.config import settings

class N8NClient:
    """Client für n8n Webhook-Aufrufe"""
    
    def __init__(self):
        self.base_url = settings.n8n_webhook_url
    
    async def trigger_workflow(
        self,
        workflow_id: str,
        data: Optional[Dict] = None
    ) -> Dict:
        """n8n Workflow über Webhook triggern"""
        try:
            url = f"{self.base_url}/{workflow_id}"
            payload = data or {}
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    json=payload,
                    timeout=30.0
                )
                response.raise_for_status()
                
                logger.info(f"✅ n8n Workflow {workflow_id} getriggert")
                return {
                    "success": True,
                    "workflow_id": workflow_id,
                    "response": response.json() if response.text else {}
                }
        except Exception as e:
            logger.error(f"❌ Fehler beim Triggern von n8n Workflow {workflow_id}: {e}")
            return {
                "success": False,
                "workflow_id": workflow_id,
                "error": str(e)
            }
    
    async def trigger_workflow_with_name(
        self,
        workflow_name: str,
        data: Optional[Dict] = None
    ) -> Dict:
        """
        Workflow über Namen triggern
        Nutzt eine einfache Mapping-Strategie
        """
        # Mapping von Namen zu Workflow-IDs
        # In Produktion sollte das aus einer Datenbank oder Config kommen
        workflow_mapping = {
            "backup": "backup-workflow",
            "report": "daily-report",
            "notification": "send-notification",
            "email": "send-email",
            # Weitere Mappings hier hinzufügen
        }
        
        workflow_id = workflow_mapping.get(workflow_name.lower())
        
        if not workflow_id:
            logger.warning(f"⚠️  Kein Workflow mit Namen '{workflow_name}' gefunden")
            return {
                "success": False,
                "workflow_name": workflow_name,
                "error": f"Workflow '{workflow_name}' nicht gefunden"
            }
        
        return await self.trigger_workflow(workflow_id, data)
