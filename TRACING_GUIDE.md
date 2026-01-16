# Request Flow Tracking - Schnellanleitung

## 🎯 Dein Problem

**Frage:** "Ist das Licht im Arbeitszimmer an?"  
**Erwartete Antwort:** "Das Licht im Arbeitszimmer ist ausgeschaltet."  
**Tatsächliche Antwort:** JSON-Output und erfundene Antwort

## ✅ Lösung ist implementiert!

Das System hat jetzt:
1. ✅ **ActionExecutor** - Führt erkannte Intents aus
2. ✅ **Verbesserte Intent-Erkennung** - Erkennt Status-Anfragen korrekt
3. ✅ **Natürliche Antworten** - Kein JSON mehr in Antworten
4. ✅ **Detailliertes Logging** - Komplettes Request-Tracking

## 📊 So verfolgst du den Request-Flow:

### 1. Logs in Echtzeit anzeigen

```bash
docker-compose logs -f backend
```

### 2. Was du in den Logs sehen solltest:

```
📨 Neue Nachricht: 'Ist das Licht im Arbeitszimmer an?'
🔍 Extrahiere Intent...
🎯 Intent: homeassistant.get_state | Entity: light.arbeitszimmer
⚡ Führe Aktion aus: homeassistant.get_state
🔍 Found entity: light.arbeitszimmer
✅ Aktion ausgeführt: True - Licht Arbeitszimmer ist ausgeschaltet
✅ Antwort generiert: 'Das Licht im Arbeitszimmer ist ausgeschaltet.'
```

### 3. Nur wichtige Events filtern

```bash
docker-compose logs -f backend | grep -E "📨|🎯|⚡|✅|❌"
```

### 4. Vollständiger Flow-Trace

```bash
# Starte Logs
docker-compose logs -f backend > backend.log &

# Stelle Frage im Chat
# "Ist das Licht im Arbeitszimmer an?"

# Stoppe Logs (Ctrl+C)

# Prüfe Log
cat backend.log | grep -A 5 -B 5 "arbeitszimmer"
```

## 🔍 Detailliertes Request-Tracing

### Flow für "Ist das Licht im Arbeitszimmer an?":

```
┌─────────────────────────────────────────┐
│ 1. User fragt im Frontend               │
│    "Ist das Licht im Arbeitszimmer an?" │
└─────────────────┬───────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ 2. POST /api/chat/send                  │
│    Log: "📨 Neue Nachricht..."          │
└─────────────────┬───────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ 3. Intent Extraction                    │
│    Log: "🔍 Extrahiere Intent..."       │
│    Result: homeassistant.get_state      │
│    Log: "🎯 Intent: homeassistant..."   │
└─────────────────┬───────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ 4. ActionExecutor.execute()             │
│    Log: "⚡ Führe Aktion aus..."        │
└─────────────────┬───────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ 5. HomeAssistantClient.get_state()      │
│    → Ruft HA API auf                    │
│    → Erhält State: "off"                │
└─────────────────┬───────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ 6. ActionExecutor returns               │
│    {                                    │
│      "success": true,                   │
│      "message": "Licht ist aus"         │
│    }                                    │
│    Log: "✅ Aktion ausgeführt..."       │
└─────────────────┬───────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ 7. Ollama generiert natürliche Antwort │
│    Prompt: "Ergebnis: Licht ist aus"    │
│    → "Das Licht ist ausgeschaltet."     │
└─────────────────┬───────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ 8. Response an Frontend                 │
│    Log: "✅ Antwort generiert..."       │
└─────────────────┴───────────────────────┘
```

## 🐛 Troubleshooting

### Problem: Intent nicht erkannt

**Logs zeigen:**
```
🎯 Intent: general.conversation | Entity: none
```

**Lösung:** Intent-Prompt muss angepasst werden

---

### Problem: Home Assistant nicht erreichbar

**Logs zeigen:**
```
❌ Error executing Home Assistant action: Connection refused
```

**Prüfen:**
```bash
# In .env:
HOME_ASSISTANT_URL=http://192.168.1.100:8123
HOME_ASSISTANT_TOKEN=eyJ...

# Testen:
curl -H "Authorization: Bearer $TOKEN" $URL/api/states
```

---

### Problem: Entity nicht gefunden

**Logs zeigen:**
```
❌ Entity 'light.arbeitszimmer' not found
```

**Lösung:** Finde korrekte Entity-ID:
```bash
docker-compose exec backend python3 -c "
from integrations.homeassistant import HomeAssistantClient
import asyncio

async def test():
    client = HomeAssistantClient()
    entities = await client.search_entities('arbeit')
    for e in entities:
        print(e['entity_id'])

asyncio.run(test())
"
```

---

### Problem: Antwort enthält noch JSON

**Logs zeigen:**
```
✅ Antwort: '{"intent": "homeassistant.turn_on"...}'
```

**Grund:** Ollama System-Prompt nicht aktualisiert  
**Lösung:** Container neu starten:
```bash
docker-compose restart backend
```

## 📋 Schnell-Checkliste

Nach Update testen:

```bash
# 1. Backend neu starten
docker-compose restart backend

# 2. Logs öffnen
docker-compose logs -f backend

# 3. Im Chat fragen:
"Ist das Licht im Wohnzimmer an?"

# 4. In Logs prüfen:
✅ Intent erkannt: homeassistant.get_state
✅ Aktion ausgeführt: True
✅ Natürliche Antwort (kein JSON!)
```

## 🎯 Erwartete Antworten

| Frage | Erwartete Antwort |
|-------|-------------------|
| "Ist das Licht an?" | "Das Licht ist eingeschaltet." |
| "Schalte das Licht aus" | "Ich habe das Licht ausgeschaltet." |
| "Wie ist das Wetter?" | "Ich habe keine Wetterinformationen." |

**KEINE JSON-Ausgaben mehr!**

## 💡 Nützliche Debug-Befehle

```bash
# Alle Chat-Events
docker-compose logs backend | grep "📨"

# Alle Intent-Erkennungen
docker-compose logs backend | grep "🎯"

# Alle Fehler
docker-compose logs backend | grep "❌"

# Flow für eine Nachricht
docker-compose logs backend | grep -A 20 "Ist das Licht"
```

---

**Mit diesen Logs siehst du GENAU was im System passiert!** 🔍
