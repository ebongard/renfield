"""
Frigate Integration für Kamera-Überwachung
"""
import json
from collections.abc import Callable

import httpx
import paho.mqtt.client as mqtt
from loguru import logger

from utils.config import settings

_shared_frigate_client: httpx.AsyncClient | None = None


async def get_frigate_http_client() -> httpx.AsyncClient:
    global _shared_frigate_client
    if _shared_frigate_client is None or _shared_frigate_client.is_closed:
        _shared_frigate_client = httpx.AsyncClient(
            base_url=settings.frigate_url or "",
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            timeout=httpx.Timeout(settings.frigate_timeout),
        )
    return _shared_frigate_client


async def close_frigate_client():
    global _shared_frigate_client
    if _shared_frigate_client is not None and not _shared_frigate_client.is_closed:
        await _shared_frigate_client.aclose()
        _shared_frigate_client = None


class FrigateClient:
    """Client für Frigate NVR"""

    def __init__(self):
        self.base_url = settings.frigate_url
        self.mqtt_client = None
        self.event_callbacks = []

    async def get_events(
        self,
        camera: str | None = None,
        label: str | None = None,
        limit: int = 10
    ) -> list[dict]:
        """Events von Frigate abrufen"""
        try:
            params = {"limit": limit}
            if camera:
                params["camera"] = camera
            if label:
                params["label"] = label

            client = await get_frigate_http_client()
            response = await client.get(
                "/api/events",
                params=params,
                timeout=settings.frigate_timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"❌ Fehler beim Abrufen der Frigate Events: {e}")
            return []

    async def get_snapshot(self, event_id: str) -> bytes | None:
        """Snapshot eines Events herunterladen"""
        try:
            client = await get_frigate_http_client()
            response = await client.get(
                f"/api/events/{event_id}/snapshot.jpg",
                timeout=settings.frigate_timeout
            )
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"❌ Fehler beim Laden des Snapshots: {e}")
            return None

    async def get_cameras(self) -> list[str]:
        """Liste aller Kameras"""
        try:
            client = await get_frigate_http_client()
            response = await client.get(
                "/api/config",
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

    def _handle_new_object(self, payload: dict):
        """Handler für neue Objekte"""
        after = payload.get("after", {})
        label = after.get("label")
        camera = after.get("camera")

        logger.info(f"🎯 Neues Objekt erkannt: {label} auf {camera}")

        # Hier könnte Benachrichtigung getriggert werden

    def _handle_object_left(self, payload: dict):
        """Handler für verlassende Objekte"""
        after = payload.get("after", {})
        label = after.get("label")
        camera = after.get("camera")

        logger.info(f"👋 Objekt verlassen: {label} von {camera}")

    def add_event_callback(self, callback: Callable):
        """Event Callback registrieren"""
        self.event_callbacks.append(callback)

    async def get_latest_events_by_type(self, label: str = "person") -> list[dict]:
        """Letzte Events eines bestimmten Typs"""
        return await self.get_events(label=label, limit=5)
