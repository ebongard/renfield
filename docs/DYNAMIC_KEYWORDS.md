# Dynamic Keyword Extraction - v1.2.0

## 🎯 Problem gelöst

**Vorher (v1.1.2):**
```python
# Statische Keyword-Liste
ha_keywords = [
    'licht', 'lampe', 'schalter', 'thermostat',
    'heizung', 'fenster', 'tür', ...
]
```

❌ Musste bei jedem neuen Gerät manuell erweitert werden  
❌ Funktionierte nicht mit benutzerdefinierten Entity-Namen  
❌ Keine Anpassung an verschiedene HA-Setups

**Jetzt (v1.2.0):**
```python
# Automatische Extraktion aus Home Assistant
keywords = await ha_client.get_keywords()
# ✅ Alle Entities werden automatisch erkannt
# ✅ Friendly Names werden berücksichtigt
# ✅ Funktioniert mit jedem HA-Setup
```

## ✨ Features

### 1. **Automatische Entity-Erkennung**

Beim Start lädt Renfield alle Entities von Home Assistant und extrahiert:

- **Domains**: `light`, `switch`, `binary_sensor`, `climate`, etc.
- **Entity-Namen**: `arbeitszimmer`, `wohnzimmer`, `schlafzimmer`
- **Friendly Names**: Alle Wörter aus "Licht Arbeitszimmer"
- **Deutsche Übersetzungen**: Automatisch für bekannte Domains

**Beispiel:**

Deine HA-Entities:
```
light.buero → Friendly Name: "Licht Büro"
binary_sensor.fenster_kueche → Friendly Name: "Fenster Küche"
switch.kaffeemaschine → Friendly Name: "Kaffeemaschine"
```

Extrahierte Keywords:
```
{
  'light', 'licht', 'lampe', 'beleuchtung',
  'buero', 'büro',
  'binary_sensor', 'sensor', 'fenster', 'kontakt',
  'fenster', 'kueche', 'küche',
  'switch', 'schalter', 'steckdose',
  'kaffeemaschine',
  'ein', 'aus', 'an', 'schalten', ...
}
```

### 2. **Intelligenter Cache (5 Minuten)**

Keywords werden für 5 Minuten gecached:
- ✅ Schnelle Intent-Erkennung (kein API-Call bei jeder Nachricht)
- ✅ Automatisches Refresh alle 5 Minuten
- ✅ Manuelles Refresh via API möglich

### 3. **Fallback-Mechanismus**

Wenn Home Assistant nicht erreichbar:
```python
# Minimale Keyword-Liste als Fallback
fallback_keywords = {
    'licht', 'lampe', 'schalter', 'thermostat',
    'heizung', 'fenster', 'tür', 'ein', 'aus'
}
```

### 4. **Domain-Übersetzungen**

Automatische Übersetzungen für gängige Domains:

| Domain | Deutsche Keywords |
|--------|------------------|
| light | licht, lampe, beleuchtung |
| switch | schalter, steckdose |
| binary_sensor | sensor, fenster, tür, kontakt |
| climate | thermostat, heizung, klima |
| cover | rolladen, jalousie, rollo |
| media_player | fernseher, tv, player |
| lock | schloss, türschloss |
| fan | lüfter, ventilator |
| vacuum | staubsauger, saugroboter |

## 🔄 Workflow

### Beim App-Start:

```
1. Renfield startet
2. Im Hintergrund: Verbindung zu HA
3. Alle Entities abrufen
4. Keywords extrahieren
5. Keywords cachen
6. ✅ Bereit für Intent-Erkennung
```

### Bei Intent-Erkennung:

```
1. User fragt: "Ist die Kaffeemaschine an?"
2. Intent-Extraction prüft: "kaffeemaschine" in Keywords?
3. ✅ JA → homeassistant.get_state
4. Entity-ID: switch.kaffeemaschine
```

### Bei Keyword-Refresh:

```
# Automatisch alle 5 Minuten
Keywords veraltet? → Neu laden von HA

# Oder manuell:
POST /admin/refresh-keywords
```

## 📊 API-Endpoints

### Keywords abrufen

```bash
# Im Backend (Python)
from integrations.homeassistant import HomeAssistantClient

ha_client = HomeAssistantClient()
keywords = await ha_client.get_keywords()
print(f"Gefunden: {len(keywords)} Keywords")
```

### Keywords manuell refreshen

```bash
# Via API
curl -X POST http://localhost:8000/admin/refresh-keywords

# Response:
{
  "status": "success",
  "keywords_count": 342,
  "sample_keywords": ["licht", "arbeitszimmer", "wohnzimmer", ...]
}
```

## 🧪 Testen

### Test 1: Neues Gerät hinzufügen

```bash
# 1. Füge neues Gerät in Home Assistant hinzu
# z.B. "Luftbefeuchter Schlafzimmer"

# 2. Warte 5 Minuten (oder refresh manuell)
curl -X POST http://localhost:8000/admin/refresh-keywords

# 3. Teste im Chat
"Ist der Luftbefeuchter an?"
```

**Erwartetes Ergebnis:**
```
🎯 Intent: homeassistant.get_state
Entity: climate.luftbefeuchter_schlafzimmer
```

### Test 2: Benutzerdefinierte Namen

```bash
# Home Assistant Entity:
light.dg_bad → Friendly Name: "Badlicht Dachgeschoss"

# Test im Chat:
"Schalte das Badlicht ein"
```

**Erwartetes Ergebnis:**
```
🎯 Intent: homeassistant.turn_on
(Keywords: "badlicht" wurde aus Friendly Name extrahiert)
```

### Test 3: Cache-Verhalten

```bash
# Erste Anfrage
docker-compose logs backend | grep "Lade Keywords"
# ✅ Zeigt: "🔄 Lade Keywords aus Home Assistant..."

# Zweite Anfrage (innerhalb 5 Min)
# ✅ Zeigt: "🗂️  Using cached keywords (342 items)"
```

## 🐛 Troubleshooting

### Keywords werden nicht geladen

**Prüfe Logs:**
```bash
docker-compose logs backend | grep -i keyword
```

**Erwartete Ausgabe:**
```
✅ Home Assistant Keywords vorgeladen: 342 Keywords
```

**Falls Fehler:**
```
⚠️  Keywords konnten nicht vorgeladen werden: Connection refused
```

**Lösung:**
```bash
# Prüfe Home Assistant-Verbindung
curl -H "Authorization: Bearer $TOKEN" \
  http://homeassistant.local:8123/api/states

# Oder in .env prüfen:
HOME_ASSISTANT_URL=http://192.168.1.100:8123
HOME_ASSISTANT_TOKEN=eyJ...
```

### Gerät wird nicht erkannt

**Debug:**
```bash
# Prüfe welche Keywords geladen wurden
curl http://localhost:8000/admin/refresh-keywords | jq '.sample_keywords'

# Oder im Backend:
docker-compose exec backend python3 -c "
from integrations.homeassistant import HomeAssistantClient
import asyncio

async def test():
    client = HomeAssistantClient()
    keywords = await client.get_keywords(refresh=True)
    print('Keywords:', list(keywords)[:50])

asyncio.run(test())
"
```

### Cache nicht aktuell

**Manuelles Refresh:**
```bash
# Via API
curl -X POST http://localhost:8000/admin/refresh-keywords

# Oder Backend neu starten
docker-compose restart backend
```

## 💡 Erweiterte Nutzung

### Custom Keyword-Mapping

Wenn du spezielle Keywords hinzufügen willst:

```python
# In backend/integrations/homeassistant.py
# Erweitere domain_translations:

domain_translations = {
    "light": ["licht", "lampe", "beleuchtung", "led"],  # + "led"
    "switch": ["schalter", "steckdose", "power"],       # + "power"
    # ...
}
```

### Keyword-Logging

Debug-Logging für Keyword-Matches:

```python
# In backend/services/ollama_service.py
# In _get_ha_keywords():

logger.debug(f"Prüfe Nachricht gegen {len(keywords)} Keywords")
matched = [kw for kw in keywords if kw in message_lower]
logger.debug(f"Gefundene Keywords: {matched}")
```

## 📈 Performance

| Metric | Wert |
|--------|------|
| Keywords laden | ~500ms (erstes Mal) |
| Keywords aus Cache | ~1ms |
| Cache-Dauer | 5 Minuten |
| HA-API-Calls | 1 pro 5 Min + bei Refresh |

**Für 100 Entities:**
- ~300-400 Keywords
- ~10KB Memory
- Vernachlässigbare CPU-Last

## 🎊 Vorteile

| Feature | Vorher | Nachher |
|---------|--------|---------|
| Neue Geräte | ❌ Manuell hinzufügen | ✅ Automatisch erkannt |
| Custom Names | ❌ Nicht unterstützt | ✅ Friendly Names genutzt |
| Verschiedene Setups | ❌ Eine Liste für alle | ✅ Angepasst an dein HA |
| Wartung | ❌ Bei jedem neuen Gerät | ✅ Keine Wartung nötig |
| Genauigkeit | ⚠️  Mittelmäßig | ✅ Sehr hoch |

## 🚀 Update durchführen

```bash
cd renfield
./quick-update.sh
```

Oder:
```bash
docker-compose restart backend
```

## ✅ Verifizieren

Nach dem Update:

```bash
# 1. Logs prüfen
docker-compose logs backend | grep Keywords

# Erwartete Ausgabe:
# ✅ Home Assistant Keywords vorgeladen: 342 Keywords

# 2. Teste mit deinem Gerät
# "Ist [dein-gerät] an?"

# 3. Prüfe Keywords-Endpoint
curl http://localhost:8000/admin/refresh-keywords | jq
```

---

## 📝 Changelog v1.2.0

**Added:**
- ✅ Automatische Keyword-Extraktion aus Home Assistant
- ✅ Intelligenter 5-Minuten-Cache
- ✅ Fallback-Keywords bei HA-Ausfall
- ✅ Domain-Übersetzungen
- ✅ Background-Preloading beim Start
- ✅ `/admin/refresh-keywords` Endpoint

**Changed:**
- 🔄 Intent-Erkennung nutzt jetzt dynamische Keywords
- 🔄 Keyword-Validierung nutzt HA-Entities

**Improved:**
- 🚀 Funktioniert mit jedem HA-Setup
- 🚀 Keine manuelle Keyword-Pflege mehr nötig
- 🚀 Höhere Intent-Erkennungsrate

---

**Keine manuelle Keyword-Liste mehr nötig!** 🎉

Renfield lernt automatisch alle deine Home Assistant Geräte!
