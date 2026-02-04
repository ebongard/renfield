"""
Frigate Integration für Kamera-Überwachung
"""
import httpx
import paho.mqtt.client as mqtt
from typing import Dict, List, Callable, Optional
from loguru import logger
from utils.config import settings
import json

class FrigateClient:
    """Client für Frigate NVR"""
    
    def __init__(self):
        self.base_url = settings.frigate_url
        self.mqtt_client = None
        self.event_callbacks = []
    
    async def get_events(
        self,
        camera: Optional[str] = None,
        label: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict]:
        """Events von Frigate abrufen"""
        try:
            params = {"limit": limit}
            if camera:
                params["camera"] = camera
            if label:
                params["label"] = label
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/events",
                    params=params,
                    timeout=settings.frigate_timeout
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"❌ Fehler beim Abrufen der Frigate Events: {e}")
            return []
    
    async def get_snapshot(self, event_id: str) -> Optional[bytes]:
        """Snapshot eines Events herunterladen"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/events/{event_id}/snapshot.jpg",
                    timeout=settings.frigate_timeout
                )
                response.raise_for_status()
                return response.content
        except Exception as e:
            logger.error(f"❌ Fehler beim Laden des Snapshots: {e}")
            return None
    
    async def get_cameras(self) -> List[str]:
        """Liste aller Kameras"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/config",
                    timeout=settings.frigate_timeout
                )
                response.raise_for_status()
                config = response.json()
                return list(config.get("cameras", {}).keys())
        except Exception as e:
            logger.error(f"❌ Fehler beim Abrufen der Kamera-Liste: {e}")
            return []
    
    def setup_mqtt(self, broker: str = "localhost", port: int = 1883):
        """MQTT Client für Echtzeit-Events einrichten"""
        def on_connect(client, userdata, flags, rc):
            logger.info(f"✅ MQTT verbunden (Code: {rc})")
            # Alle Frigate Events abonnieren
            client.subscribe("frigate/events")
            client.subscribe("frigate/+/+/snapshot")
        
        def on_message(client, userdata, msg):
            try:
                topic = msg.topic
                payload = json.loads(msg.payload.decode())
                
                # Event-Typ identifizieren
                if "events" in topic:
                    event_type = payload.get("type")
                    
                    if event_type == "new":
                        # Neues Objekt erkannt
                        self._handle_new_object(payload)
                    elif event_type == "end":
                        # Objekt verlassen
                        self._handle_object_left(payload)
                
                # Callbacks aufrufen
                for callback in self.event_callbacks:
                    callback(topic, payload)
            except Exception as e:
                logger.error(f"❌ MQTT Message Fehler: {e}")
        
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_message = on_message
        
        try:
            self.mqtt_client.connect(broker, port, 60)
            self.mqtt_client.loop_start()
            logger.info("✅ MQTT Client gestartet")
        except Exception as e:
            logger.error(f"❌ MQTT Connection Fehler: {e}")
    
    def _handle_new_object(self, payload: Dict):
        """Handler für neue Objekte"""
        after = payload.get("after", {})
        label = after.get("label")
        camera = after.get("camera")
        
        logger.info(f"🎯 Neues Objekt erkannt: {label} auf {camera}")
        
        # Hier könnte Benachrichtigung getriggert werden
    
    def _handle_object_left(self, payload: Dict):
        """Handler für verlassende Objekte"""
        after = payload.get("after", {})
        label = after.get("label")
        camera = after.get("camera")
        
        logger.info(f"👋 Objekt verlassen: {label} von {camera}")
    
    def add_event_callback(self, callback: Callable):
        """Event Callback registrieren"""
        self.event_callbacks.append(callback)
    
    async def get_latest_events_by_type(self, label: str = "person") -> List[Dict]:
        """Letzte Events eines bestimmten Typs"""
        return await self.get_events(label=label, limit=5)
