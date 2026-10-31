"""
Frigate integration for camera surveillance
"""
import json
from collections.abc import Callable

import httpx
import paho.mqtt.client as mqtt
from loguru import logger

from ha_glue.utils.config import ha_glue_settings

_shared_frigate_client: httpx.AsyncClient | None = None


async def get_frigate_http_client() -> httpx.AsyncClient:
    global _shared_frigate_client
    if _shared_frigate_client is None or _shared_frigate_client.is_closed:
        _shared_frigate_client = httpx.AsyncClient(
            base_url=ha_glue_settings.frigate_url or "",
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            timeout=httpx.Timeout(ha_glue_settings.frigate_timeout),
        )
    return _shared_frigate_client


async def close_frigate_client():
    global _shared_frigate_client
    if _shared_frigate_client is not None and not _shared_frigate_client.is_closed:
        await _shared_frigate_client.aclose()
        _shared_frigate_client = None


class FrigateClient:
    """Client for the Frigate NVR"""

    def __init__(self):
        self.base_url = ha_glue_settings.frigate_url
        self.mqtt_client = None
        self.event_callbacks = []

    async def get_events(
        self,
        camera: str | None = None,
        label: str | None = None,
        limit: int = 10
    ) -> list[dict]:
        """Fetch events from Frigate"""
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
                timeout=ha_glue_settings.frigate_timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"❌ Failed to fetch the Frigate events: {e}")
            return []

    async def get_snapshot(self, event_id: str) -> bytes | None:
        """Download an event's snapshot"""
        try:
            client = await get_frigate_http_client()
            response = await client.get(
                f"/api/events/{event_id}/snapshot.jpg",
                timeout=ha_glue_settings.frigate_timeout
            )
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"❌ Failed to load the snapshot: {e}")
            return None

    async def get_cameras(self) -> list[str]:
        """List every camera"""
        try:
            client = await get_frigate_http_client()
            response = await client.get(
                "/api/config",
                timeout=ha_glue_settings.frigate_timeout
            )
            response.raise_for_status()
            config = response.json()
            return list(config.get("cameras", {}).keys())
        except Exception as e:
            logger.error(f"❌ Failed to fetch the camera list: {e}")
            return []

    def setup_mqtt(self, broker: str | None = None, port: int | None = None):
        """Set up the MQTT client for real-time events.

        Defaults to `frigate_mqtt_broker` / `frigate_mqtt_port` from
        `ha_glue_settings`; explicit args override settings.
        """
        broker = broker or ha_glue_settings.frigate_mqtt_broker
        port = port if port is not None else ha_glue_settings.frigate_mqtt_port
        def on_connect(client, userdata, flags, rc):
            logger.info(f"✅ MQTT connected (code: {rc})")
            # Subscribe to all Frigate events
            client.subscribe("frigate/events")
            client.subscribe("frigate/+/+/snapshot")

        def on_message(client, userdata, msg):
            try:
                topic = msg.topic
                payload = json.loads(msg.payload.decode())

                # Identify the event type
                if "events" in topic:
                    event_type = payload.get("type")

                    if event_type == "new":
                        # New object detected
                        self._handle_new_object(payload)
                    elif event_type == "end":
                        # Object left
                        self._handle_object_left(payload)

                # Invoke the callbacks
                for callback in self.event_callbacks:
                    callback(topic, payload)
            except Exception as e:
                logger.error(f"❌ MQTT message error: {e}")

        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_message = on_message

        try:
            self.mqtt_client.connect(broker, port, 60)
            self.mqtt_client.loop_start()
            logger.info("✅ MQTT client started")
        except Exception as e:
            logger.error(f"❌ MQTT connection error: {e}")

    def _handle_new_object(self, payload: dict):
        """Handler for new objects"""
        after = payload.get("after", {})
        label = after.get("label")
        camera = after.get("camera")

        logger.info(f"🎯 New object detected: {label} on {camera}")

        # A notification could be triggered here

    def _handle_object_left(self, payload: dict):
        """Handler for objects that leave"""
        after = payload.get("after", {})
        label = after.get("label")
        camera = after.get("camera")

        logger.info(f"👋 Object left: {label} from {camera}")

    def add_event_callback(self, callback: Callable):
        """Register an event callback"""
        self.event_callbacks.append(callback)

    async def get_latest_events_by_type(self, label: str = "person") -> list[dict]:
        """The most recent events of one type"""
        return await self.get_events(label=label, limit=5)
