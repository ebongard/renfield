# Renfield Debugging Guide

## 🔍 Request Flow verstehen

### Normaler Chat-Flow:

```
1. User → Frontend
   ↓
2. Frontend → Backend (/api/chat/send)
   ↓
3. Backend → Ollama (Intent Recognition)
   ↓
4. Backend → Entscheidung:
   - Falls "general.conversation" → Direkt antworten
   - Falls "homeassistant.*" → Home Assistant aufrufen
   - Falls "camera.*" → Frigate aufrufen
   - Falls "n8n.*" → n8n triggern
   ↓
5. Integration ausführen
   ↓
6. Backend → Response zusammenstellen
   ↓
7. Frontend → Anzeige
```

### Aktuelles Problem:

**Symptom:** Intent wird erkannt, aber Integration nicht ausgeführt

**Ursache:** Der Chat-Handler führt keine Aktionen basierend auf dem Intent aus!

## 📊 Debugging aktivieren

### 1. Log-Level auf DEBUG setzen

```bash
# In .env:
LOG_LEVEL=DEBUG
```

Dann neu starten:
```bash
docker-compose restart backend
```

### 2. Logs in Echtzeit anzeigen

```bash
# Alle Logs
docker-compose logs -f backend

# Nur wichtige Zeilen
docker-compose logs -f backend | grep -E "Intent|HomeAssistant|ERROR|INFO"
```

### 3. Strukturierte Logs

Die Logs zeigen dir:
```
[Timestamp] | [Level] | [Module] | Message
```

Beispiel:
```
2026-01-15 21:00:00.000 | INFO | main:lifespan | 🚀 Renfield startet...
2026-01-15 21:00:01.123 | DEBUG | ollama_service:extract_intent | Intent erkannt: homeassistant.turn_on
2026-01-15 21:00:01.456 | INFO | homeassistant:turn_on | ✅ Licht eingeschaltet
```

## 🐛 Typische Probleme

### Problem 1: Intent erkannt, aber nicht ausgeführt

**Logs zeigen:**
```
INFO | Intent erkannt: homeassistant.turn_on
INFO | Response zurück an User
```

**Fehlt:**
```
INFO | HomeAssistant aufgerufen
```

**Grund:** Chat-Handler führt keine Aktionen aus basierend auf Intent

**Lösung:** Chat-Handler muss erweitert werden (siehe unten)

---

### Problem 2: Home Assistant nicht erreichbar

**Logs zeigen:**
```
ERROR | HomeAssistant Connection Failed
```

**Prüfen:**
```bash
# In .env:
HOME_ASSISTANT_URL=http://192.168.1.100:8123  # Korrekte IP?
HOME_ASSISTANT_TOKEN=eyJ...                    # Gültiger Token?

# Testen:
docker-compose exec backend python3 -c "
from integrations.homeassistant import HomeAssistantClient
import asyncio

async def test():
    client = HomeAssistantClient()
    states = await client.get_states()
    print(f'✅ Gefunden: {len(states)} Entities')

asyncio.run(test())
"
```

---

### Problem 3: Entity nicht gefunden

**Logs zeigen:**
```
INFO | Intent: homeassistant.turn_on
ERROR | Entity 'light.arbeitszimmer' nicht gefunden
```

**Prüfen welche Entities verfügbar sind:**
```bash
docker-compose exec backend python3 -c "
from integrations.homeassistant import HomeAssistantClient
import asyncio

async def test():
    client = HomeAssistantClient()
    results = await client.search_entities('arbeitszimmer')
    for r in results:
        print(f'{r[\"entity_id\"]}: {r[\"friendly_name\"]}')

asyncio.run(test())
"
```

---

## 🔧 Request Tracking

### Methode 1: Backend Logs

**Starte Backend mit Debug-Level:**
```bash
# In docker-compose.yml, bei backend:
environment:
  LOG_LEVEL: DEBUG

docker-compose restart backend
```

**Oder temporär:**
```bash
docker-compose exec backend sh -c "export LOG_LEVEL=DEBUG && uvicorn main:app --reload"
```

### Methode 2: Request-ID Tracking

Füge Request-ID zu jedem API-Call hinzu:

```javascript
// Frontend: src/utils/axios.js
apiClient.interceptors.request.use((config) => {
  config.headers['X-Request-ID'] = Date.now().toString();
  console.log('→ API Request:', config.url, config.headers['X-Request-ID']);
  return config;
});
```

### Methode 3: Browser Developer Tools

**Network Tab:**
1. Öffne Browser DevTools (F12)
2. Gehe zu Network Tab
3. Sende Nachricht im Chat
4. Sieh Request/Response:

```
Request Payload:
{
  "message": "Ist das Licht im Arbeitszimmer an?",
  "session_id": "session-xxx"
}

Response:
{
  "message": "Das Licht ist aus",
  "intent": {
    "intent": "homeassistant.get_state",
    "entity_id": "light.arbeitszimmer"
  }
}
```

### Methode 4: Custom Debug Endpoint

Erstelle temporären Debug-Endpoint in `backend/main.py`:

```python
@app.post("/debug/intent")
async def debug_intent(message: str):
    """Debug: Zeige erkannten Intent"""
    ollama = app.state.ollama
    intent = await ollama.extract_intent(message)
    
    # Simuliere Aktion
    if intent["intent"].startswith("homeassistant."):
        ha_client = HomeAssistantClient()
        # ... führe Aktion aus
    
    return {
        "message": message,
        "intent": intent,
        "action_executed": True  # oder False
    }
```

Teste mit:
```bash
curl -X POST http://localhost:8000/debug/intent \
  -H "Content-Type: application/json" \
  -d '{"message": "Ist das Licht an?"}'
```

---

## 📝 Logging Best Practices

### Im Code:

```python
from loguru import logger

# Bei Anfrage
logger.info(f"📨 Neue Nachricht: {message[:50]}...")

# Intent erkannt
logger.debug(f"🎯 Intent erkannt: {intent['intent']}")

# Aktion ausgeführt
logger.info(f"⚡ Home Assistant: {action} für {entity_id}")

# Fehler
logger.error(f"❌ Fehler bei {action}: {error}")

# Ergebnis
logger.info(f"✅ Antwort: {response[:100]}...")
```

### Log-Levels:

- `DEBUG`: Detaillierte Info (nur für Entwicklung)
- `INFO`: Normale Events
- `WARNING`: Unerwartetes, aber nicht kritisch
- `ERROR`: Fehler die behandelt werden
- `CRITICAL`: System-kritische Fehler

---

## 🎯 Dein spezifisches Problem

### Aktueller Flow (FALSCH):

```
User: "Ist das Licht im Arbeitszimmer an?"
  ↓
Ollama: Intent erkannt: "homeassistant.get_state"
  ↓
Backend: Gibt Intent als Text zurück ❌
  ↓
User sieht: JSON-Output und erfundene Antwort
```

### Gewünschter Flow (RICHTIG):

```
User: "Ist das Licht im Arbeitszimmer an?"
  ↓
Ollama: Intent erkannt: "homeassistant.get_state"
  ↓
Backend: Führt Home Assistant Aktion aus ✅
  ↓
Home Assistant: Gibt State zurück
  ↓
Backend: Formuliert natürliche Antwort
  ↓
User sieht: "Das Licht im Arbeitszimmer ist derzeit aus."
```

---

## 🔍 Schnell-Diagnose

Führe diese Befehle aus um das Problem zu finden:

```bash
# 1. Ist Home Assistant erreichbar?
docker-compose exec backend python3 -c "
from integrations.homeassistant import HomeAssistantClient
import asyncio
async def test():
    client = HomeAssistantClient()
    try:
        states = await client.get_states()
        print(f'✅ HA erreichbar: {len(states)} Entities')
    except Exception as e:
        print(f'❌ HA nicht erreichbar: {e}')
asyncio.run(test())
"

# 2. Wird Intent korrekt erkannt?
docker-compose exec backend python3 -c "
from services.ollama_service import OllamaService
import asyncio
async def test():
    ollama = OllamaService()
    intent = await ollama.extract_intent('Ist das Licht im Arbeitszimmer an?')
    print(f'Intent: {intent}')
asyncio.run(test())
"

# 3. Werden Aktionen ausgeführt?
# → Prüfe Logs während Chat:
docker-compose logs -f backend | grep -E "Home|Intent|Action"
```

---

## 💡 Lösung

Das Hauptproblem ist im **Chat-Handler**. Er muss erweitert werden um:

1. Intent erkennen ✅ (funktioniert)
2. **Basierend auf Intent Aktion ausführen** ❌ (fehlt!)
3. Ergebnis der Aktion in Antwort einbauen ❌ (fehlt!)

Ich erstelle gleich den Fix dafür!

---

## 📚 Weitere Debug-Tools

- **debug.sh** - Zeigt alle Container-Logs
- **BUGFIXES.md** - Gelöste Probleme
- **FRONTEND_FIXES.md** - Frontend-Probleme
- **Diese Datei** - Debugging & Request-Tracking

---

**Nächster Schritt:** Ich erstelle den Fix für den Chat-Handler, damit Intents auch ausgeführt werden!
