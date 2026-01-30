"""
Home Assistant Integration

Provides REST API client for controlling devices and WebSocket API client
for Area Registry operations (listing, creating, updating areas).
"""
import httpx
import json
import asyncio
import time
from typing import Dict, List, Optional, Any
from loguru import logger
from utils.config import settings

class HomeAssistantClient:
    """Client für Home Assistant REST API"""

    # Class-level entity map cache (shared across instances, same HA backend)
    _entity_map_cache: Optional[List[Dict]] = None
    _entity_map_cache_time: float = 0
    _ENTITY_MAP_TTL: float = 60.0  # seconds

    def __init__(self):
        self.base_url = settings.home_assistant_url
        self.token = settings.home_assistant_token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        # Keyword Cache
        self._keywords_cache = None
        self._keywords_last_updated = None
        self._cache_ttl = 300  # 5 Minuten Cache
    
    async def get_states(self) -> List[Dict]:
        """Alle Entity States abrufen"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/states",
                    headers=self.headers,
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"❌ Fehler beim Abrufen der States: {e}")
            return []
    
    async def get_state(self, entity_id: str) -> Optional[Dict]:
        """State einer bestimmten Entity abrufen"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/states/{entity_id}",
                    headers=self.headers,
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"❌ Fehler beim Abrufen des States für {entity_id}: {e}")
            return None
    
    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: Optional[str] = None,
        service_data: Optional[Dict] = None,
        timeout: float = 10.0
    ) -> bool:
        """Service aufrufen"""
        try:
            data = service_data or {}
            if entity_id:
                data["entity_id"] = entity_id

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/api/services/{domain}/{service}",
                    headers=self.headers,
                    json=data,
                    timeout=timeout
                )
                response.raise_for_status()
                logger.info(f"✅ Service {domain}.{service} für {entity_id} aufgerufen")
                return True
        except Exception as e:
            logger.error(f"❌ Fehler beim Aufrufen von {domain}.{service}: {e}")
            return False
    
    async def turn_on(self, entity_id: str) -> bool:
        """Gerät einschalten"""
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_on", entity_id)
    
    async def turn_off(self, entity_id: str) -> bool:
        """Gerät ausschalten"""
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_off", entity_id)
    
    async def toggle(self, entity_id: str) -> bool:
        """Gerät umschalten"""
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "toggle", entity_id)
    
    async def set_value(self, entity_id: str, value: any, attribute: str = "value") -> bool:
        """Wert setzen (z.B. Helligkeit, Temperatur)"""
        domain = entity_id.split(".")[0]
        
        # Spezielle Handler für verschiedene Domains
        if domain == "light" and attribute in ["brightness", "brightness_pct"]:
            return await self.call_service(
                "light",
                "turn_on",
                entity_id,
                {attribute: value}
            )
        elif domain == "climate" and attribute == "temperature":
            return await self.call_service(
                "climate",
                "set_temperature",
                entity_id,
                {"temperature": value}
            )
        elif domain == "cover" and attribute == "position":
            return await self.call_service(
                "cover",
                "set_cover_position",
                entity_id,
                {"position": value}
            )
        else:
            return await self.call_service(
                domain,
                "set_value",
                entity_id,
                {attribute: value}
            )
    
    async def search_entities(self, query: str) -> List[Dict]:
        """Entities nach Namen suchen"""
        all_states = await self.get_states()
        query_lower = query.lower()
        
        results = []
        for state in all_states:
            entity_id = state.get("entity_id", "")
            friendly_name = state.get("attributes", {}).get("friendly_name", "")
            
            if query_lower in entity_id.lower() or query_lower in friendly_name.lower():
                results.append({
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "state": state.get("state"),
                    "domain": entity_id.split(".")[0]
                })
        
        return results
    
    async def get_entities_by_domain(self, domain: str) -> List[Dict]:
        """Alle Entities eines bestimmten Domains"""
        all_states = await self.get_states()
        return [
            {
                "entity_id": s.get("entity_id"),
                "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
                "state": s.get("state")
            }
            for s in all_states
            if s.get("entity_id", "").startswith(f"{domain}.")
        ]
    
    async def get_keywords(self, refresh: bool = False) -> set:
        """
        Extrahiere alle Keywords aus Home Assistant Entities
        
        Cached für 5 Minuten, refresh=True erzwingt Neuladung
        
        Returns:
            set: Keywords (Gerätenamen, Räume, Domains)
        """
        from datetime import datetime, timedelta
        
        # Prüfe Cache
        if not refresh and self._keywords_cache is not None:
            if self._keywords_last_updated:
                cache_age = datetime.now() - self._keywords_last_updated
                if cache_age < timedelta(seconds=self._cache_ttl):
                    logger.debug(f"🗂️  Using cached keywords ({len(self._keywords_cache)} items)")
                    return self._keywords_cache
        
        logger.info("🔄 Lade Keywords aus Home Assistant...")
        
        try:
            states = await self.get_states()
            if not states:
                logger.warning("⚠️  Keine States von Home Assistant erhalten")
                return self._get_fallback_keywords()
            
            keywords = set()
            
            for state in states:
                entity_id = state.get("entity_id", "")
                attributes = state.get("attributes", {})
                friendly_name = attributes.get("friendly_name", "")
                
                # Domain extrahieren (light, switch, etc.)
                if "." in entity_id:
                    domain, name = entity_id.split(".", 1)
                    keywords.add(domain)
                    
                    # Name extrahieren (z.B. arbeitszimmer aus light.arbeitszimmer)
                    # Ersetze _ durch Leerzeichen für besseres Matching
                    name_parts = name.replace("_", " ").split()
                    keywords.update(name_parts)
                
                # Friendly Name parsen (z.B. "Licht Arbeitszimmer")
                if friendly_name:
                    # Alle Wörter als Keywords (lowercase für besseres Matching)
                    name_words = friendly_name.lower().split()
                    keywords.update(name_words)
            
            # Deutsche Übersetzungen für häufige Domains hinzufügen
            domain_translations = {
                "light": ["licht", "lampe", "beleuchtung"],
                "switch": ["schalter", "steckdose"],
                "binary_sensor": ["sensor", "fenster", "tür", "kontakt"],
                "climate": ["thermostat", "heizung", "klima"],
                "cover": ["rolladen", "jalousie", "rollo"],
                "media_player": ["fernseher", "tv", "player"],
                "lock": ["schloss", "türschloss"],
                "fan": ["lüfter", "ventilator"],
                "vacuum": ["staubsauger", "saugroboter"]
            }
            
            # Füge Übersetzungen für vorhandene Domains hinzu
            for domain, translations in domain_translations.items():
                if domain in keywords:
                    keywords.update(translations)
            
            # Häufige Aktions-Verben hinzufügen
            action_words = [
                "ein", "aus", "an", "schalten", "stelle", "setze",
                "öffne", "schließe", "öffnen", "schließen",
                "dimme", "dimmen", "erhöhe", "verringere"
            ]
            keywords.update(action_words)
            
            # Cache aktualisieren
            self._keywords_cache = keywords
            self._keywords_last_updated = datetime.now()
            
            logger.info(f"✅ {len(keywords)} Keywords aus {len(states)} Entities extrahiert")
            logger.debug(f"Beispiel-Keywords: {list(keywords)[:20]}")
            
            return keywords
            
        except Exception as e:
            logger.error(f"❌ Fehler beim Laden der Keywords: {e}")
            return self._get_fallback_keywords()
    
    def _get_fallback_keywords(self) -> set:
        """Fallback Keywords wenn HA nicht erreichbar"""
        return {
            "licht", "lampe", "schalter", "thermostat", "heizung",
            "fenster", "tür", "rolladen", "ein", "aus", "an", "schalten"
        }

    async def get_entity_map(self) -> List[Dict]:
        """
        Erstelle eine Map aller Entities für Intent Recognition.

        Uses a class-level TTL cache (60s) to avoid hitting the HA REST API
        on every intent extraction call.

        Returns:
            List[Dict]: Liste mit entity_id, friendly_name, domain, room (falls vorhanden)
        """
        # Check class-level cache
        now = time.time()
        cls = HomeAssistantClient
        if cls._entity_map_cache is not None and (now - cls._entity_map_cache_time) < cls._ENTITY_MAP_TTL:
            logger.debug(f"Entity map cache hit ({len(cls._entity_map_cache)} entities)")
            return cls._entity_map_cache

        try:
            states = await self.get_states()
            if not states:
                logger.warning("⚠️  Keine States von Home Assistant erhalten")
                return []

            entity_map = []

            # Use a set for O(1) domain lookups
            relevant_domains = {
                "light", "switch", "binary_sensor", "sensor",
                "climate", "cover", "lock", "fan", "media_player",
                "vacuum", "camera", "alarm_control_panel", "scene"
            }

            for state in states:
                entity_id = state.get("entity_id", "")
                attributes = state.get("attributes", {})
                friendly_name = attributes.get("friendly_name", "")
                domain = entity_id.split(".")[0] if "." in entity_id else ""

                # Extrahiere Raum aus friendly_name oder entity_id
                room = self._extract_room(entity_id, friendly_name)

                if domain in relevant_domains:
                    entity_map.append({
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "domain": domain,
                        "room": room,
                        "state": state.get("state", "unknown")
                    })

            logger.info(f"✅ {len(entity_map)} relevante Entities für Intent Recognition geladen")

            # Update class-level cache
            cls._entity_map_cache = entity_map
            cls._entity_map_cache_time = time.time()

            return entity_map

        except Exception as e:
            logger.error(f"❌ Fehler beim Erstellen der Entity Map: {e}")
            return []

    def _extract_room(self, entity_id: str, friendly_name: str) -> Optional[str]:
        """
        Versuche Raum aus Entity ID oder Friendly Name zu extrahieren

        Beispiele:
        - "binary_sensor.arbeitszimmer_fenster" → "arbeitszimmer"
        - "Arbeitszimmer Fenster" → "arbeitszimmer"
        - "light.wohnzimmer" → "wohnzimmer"
        """
        # Häufige Raumnamen (lowercase)
        known_rooms = {
            "arbeitszimmer", "wohnzimmer", "schlafzimmer", "küche", "kueche",
            "bad", "badezimmer", "flur", "diele", "wc", "toilette",
            "kinderzimmer", "gästezimmer", "gaestezimmer", "keller",
            "dachboden", "garage", "terrasse", "balkon", "garten"
        }

        # Prüfe entity_id (z.B. "arbeitszimmer" in "binary_sensor.arbeitszimmer_fenster")
        entity_name = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        entity_parts = entity_name.lower().replace("_", " ").split()

        for part in entity_parts:
            if part in known_rooms:
                return part

        # Prüfe friendly_name (z.B. "Arbeitszimmer" in "Arbeitszimmer Fenster")
        if friendly_name:
            name_parts = friendly_name.lower().split()
            for part in name_parts:
                if part in known_rooms:
                    return part

        return None

    # --- Area Registry (WebSocket API) ---

    async def _ws_send_command(self, ws_type: str, **kwargs) -> Optional[Dict]:
        """
        Send a command via WebSocket API.

        Home Assistant WebSocket API uses a different URL and protocol than REST.
        Protocol:
        1. Connect to ws://<host>:8123/api/websocket
        2. Receive auth_required message
        3. Send auth with access_token
        4. Receive auth_ok or auth_invalid
        5. Send commands with incrementing id

        Args:
            ws_type: WebSocket message type (e.g., "config/area_registry/list")
            **kwargs: Additional parameters for the command

        Returns:
            Response data or None on error
        """
        import websockets

        # Convert http(s) URL to ws(s) URL
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/websocket"

        try:
            async with websockets.connect(ws_url) as ws:
                # 1. Receive auth_required
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(msg)

                if data.get("type") != "auth_required":
                    logger.error(f"Unexpected HA WS message: {data}")
                    return None

                # 2. Send authentication
                await ws.send(json.dumps({
                    "type": "auth",
                    "access_token": self.token
                }))

                # 3. Receive auth result
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(msg)

                if data.get("type") != "auth_ok":
                    logger.error(f"HA WS auth failed: {data}")
                    return None

                # 4. Send command
                cmd = {"id": 1, "type": ws_type}
                cmd.update(kwargs)
                await ws.send(json.dumps(cmd))

                # 5. Receive response
                msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                data = json.loads(msg)

                if data.get("success"):
                    return data.get("result")
                else:
                    logger.error(f"HA WS command failed: {data}")
                    return None

        except ImportError:
            logger.error("websockets library not installed. Install with: pip install websockets")
            return None
        except asyncio.TimeoutError:
            logger.error("HA WebSocket timeout")
            return None
        except Exception as e:
            logger.error(f"HA WebSocket error: {e}")
            return None

    async def get_areas(self) -> List[Dict[str, Any]]:
        """
        Fetch all areas from Home Assistant Area Registry.

        Returns:
            List of areas: [{"area_id": str, "name": str, "icon": str}, ...]
        """
        try:
            result = await self._ws_send_command("config/area_registry/list")

            if result is None:
                # Fallback: Try REST API (some HA versions support this)
                return await self._get_areas_rest_fallback()

            return result if isinstance(result, list) else []

        except Exception as e:
            logger.error(f"Failed to get HA areas: {e}")
            return await self._get_areas_rest_fallback()

    async def _get_areas_rest_fallback(self) -> List[Dict[str, Any]]:
        """
        Fallback method to get areas via REST API.

        Uses /api/config/area_registry (may not be available on all HA versions).
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/config/area_registry",
                    headers=self.headers,
                    timeout=10.0
                )

                if response.status_code == 200:
                    return response.json()

                logger.warning(f"HA areas REST fallback failed: {response.status_code}")
                return []

        except Exception as e:
            logger.error(f"HA areas REST fallback error: {e}")
            return []

    async def create_area(self, name: str, icon: Optional[str] = None) -> Optional[Dict]:
        """
        Create a new area in Home Assistant.

        Args:
            name: Area name (display name)
            icon: Optional Material Design icon (e.g., "mdi:sofa")

        Returns:
            Created area dict with area_id, or None on error
        """
        kwargs = {"name": name}
        if icon:
            kwargs["icon"] = icon

        try:
            result = await self._ws_send_command("config/area_registry/create", **kwargs)

            if result:
                logger.info(f"Created HA area: {name} (id: {result.get('area_id')})")
                return result

            return None

        except Exception as e:
            logger.error(f"Failed to create HA area: {e}")
            return None

    async def update_area(
        self,
        area_id: str,
        name: Optional[str] = None,
        icon: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Update an existing area in Home Assistant.

        Args:
            area_id: Area ID to update
            name: New name (optional)
            icon: New icon (optional)

        Returns:
            Updated area dict or None on error
        """
        kwargs = {"area_id": area_id}
        if name:
            kwargs["name"] = name
        if icon:
            kwargs["icon"] = icon

        try:
            result = await self._ws_send_command("config/area_registry/update", **kwargs)

            if result:
                logger.info(f"Updated HA area: {area_id}")
                return result

            return None

        except Exception as e:
            logger.error(f"Failed to update HA area: {e}")
            return None

    async def delete_area(self, area_id: str) -> bool:
        """
        Delete an area from Home Assistant.

        Args:
            area_id: Area ID to delete

        Returns:
            True if deleted, False on error
        """
        try:
            result = await self._ws_send_command(
                "config/area_registry/delete",
                area_id=area_id
            )

            if result is not None:
                logger.info(f"Deleted HA area: {area_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Failed to delete HA area: {e}")
            return False
