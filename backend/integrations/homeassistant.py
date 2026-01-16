"""
Home Assistant Integration
"""
import httpx
from typing import Dict, List, Optional
from loguru import logger
from utils.config import settings

class HomeAssistantClient:
    """Client für Home Assistant REST API"""
    
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
        service_data: Optional[Dict] = None
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
                    timeout=10.0
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
        Erstelle eine Map aller Entities für Intent Recognition

        Returns:
            List[Dict]: Liste mit entity_id, friendly_name, domain, room (falls vorhanden)
        """
        try:
            states = await self.get_states()
            if not states:
                logger.warning("⚠️  Keine States von Home Assistant erhalten")
                return []

            entity_map = []

            for state in states:
                entity_id = state.get("entity_id", "")
                attributes = state.get("attributes", {})
                friendly_name = attributes.get("friendly_name", "")
                domain = entity_id.split(".")[0] if "." in entity_id else ""

                # Extrahiere Raum aus friendly_name oder entity_id
                room = self._extract_room(entity_id, friendly_name)

                # Nur relevante Domains (steuerbare/abfragbare Entities)
                relevant_domains = [
                    "light", "switch", "binary_sensor", "sensor",
                    "climate", "cover", "lock", "fan", "media_player",
                    "vacuum", "camera", "alarm_control_panel", "scene"
                ]

                if domain in relevant_domains:
                    entity_map.append({
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "domain": domain,
                        "room": room,
                        "state": state.get("state", "unknown")
                    })

            logger.info(f"✅ {len(entity_map)} relevante Entities für Intent Recognition geladen")
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
