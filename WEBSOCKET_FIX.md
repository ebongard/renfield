# WebSocket Fix - v1.1.1

## 🐛 Problem

**Symptom:**
```
Logs zeigen nur:
INFO: WebSocket Nachricht: text - Welche Fenster sind offen?...
```

**Fehlt:**
- Intent-Erkennung
- Action-Execution
- Detaillierte Logs

## 🔍 Ursache

Das Frontend nutzt **WebSocket** für Echtzeit-Streaming, nicht den REST-Endpoint `/api/chat/send`.

Der WebSocket-Handler hatte noch nicht die neue Action-Executor-Logik aus v1.1!

## ✅ Lösung (v1.1.1)

### Backend: WebSocket-Handler aktualisiert

**Vorher:**
```python
# Nur direktes Streaming ohne Intent-Erkennung
async for chunk in ollama.chat_stream(content):
    await websocket.send_json({"type": "stream", "content": chunk})
```

**Jetzt:**
```python
# 1. Intent extrahieren
intent = await ollama.extract_intent(content)
logger.info(f"🎯 Intent: {intent.get('intent')}")

# 2. Action ausführen
if intent.get("intent") != "general.conversation":
    executor = ActionExecutor()
    action_result = await executor.execute(intent)
    logger.info(f"✅ Aktion: {action_result.get('success')}")

# 3. Response mit Ergebnis generieren
if action_result and action_result.get("success"):
    enhanced_prompt = f"Nutzer fragte: {content}
Ergebnis: {action_result.get('message')}"
    async for chunk in ollama.chat_stream(enhanced_prompt):
        await websocket.send_json({"type": "stream", "content": chunk})
```

### Frontend: Action-Handling hinzugefügt

```javascript
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.type === 'action') {
    // Action wurde ausgeführt
    console.log('Action:', data.intent, data.result);
  } else if (data.type === 'stream') {
    // Streaming-Antwort
    ...
  }
};
```

## 🔧 Geänderte Dateien

1. `backend/main.py` - WebSocket-Handler
2. `frontend/src/pages/ChatPage.jsx` - Action-Message-Handling

## 🚀 Update durchführen

### Option 1: Nur Backend neu starten

```bash
docker-compose restart backend
```

### Option 2: Komplett neu bauen

```bash
docker-compose down
docker-compose up --build -d
```

## ✅ Verifizieren

Nach dem Update:

### 1. Logs in Echtzeit

```bash
docker-compose logs -f backend
```

### 2. Teste im Chat

```
User: "Ist das Licht im Wohnzimmer an?"
```

### 3. Erwartete Logs

```
📨 WebSocket Nachricht: 'Ist das Licht im Wohnzimmer an?'
🔍 Extrahiere Intent...
🎯 Intent erkannt: homeassistant.get_state | Entity: light.wohnzimmer
⚡ Führe Aktion aus: homeassistant.get_state
✅ Aktion: True - Licht Wohnzimmer ist eingeschaltet
✅ WebSocket Response gesendet
```

### 4. Erwartete Antwort

```
"Das Licht im Wohnzimmer ist eingeschaltet."
```

**Kein JSON mehr!** ✅

## 📊 Log-Vergleich

### Vorher (v1.1):
```
INFO: WebSocket Nachricht: text - Ist das Licht an?...
```

### Nachher (v1.1.1):
```
📨 WebSocket Nachricht: 'Ist das Licht an?'
🔍 Extrahiere Intent...
🎯 Intent erkannt: homeassistant.get_state | Entity: light.wohnzimmer
⚡ Führe Aktion aus: homeassistant.get_state
✅ Aktion: True - Licht ist eingeschaltet
✅ WebSocket Response gesendet
```

## 🐛 Troubleshooting

### Logs zeigen noch alte Version

```bash
# Container vollständig neu bauen
docker-compose down
docker-compose build --no-cache backend
docker-compose up -d
```

### WebSocket verbindet nicht

```bash
# Prüfe Backend-Logs
docker-compose logs backend | grep WebSocket

# Prüfe Frontend-Console
# Browser DevTools → Console → Suche nach "WebSocket"
```

### Immer noch keine Intent-Logs

```bash
# Prüfe ob neue main.py geladen wurde
docker-compose exec backend cat main.py | grep "Intent erkannt"

# Sollte zeigen:
# logger.info(f"🎯 Intent erkannt: {intent.get('intent')}...")
```

## 💡 Warum WebSocket statt REST?

**Vorteile:**
- ✅ Echtzeit-Streaming (Antwort kommt Token für Token)
- ✅ Bessere UX (User sieht sofort dass System arbeitet)
- ✅ Weniger Latenz

**Nachteil:**
- ⚠️ Muss separat implementiert werden (wie hier gefixt)

## 📝 Changelog v1.1.1

**Fixed:**
- WebSocket-Handler führt jetzt Intent-Erkennung durch
- WebSocket-Handler führt Actions aus (Home Assistant, etc.)
- Detailliertes Logging für WebSocket-Requests
- Frontend behandelt action-Type Messages

**Added:**
- Emoji-Marker in WebSocket-Logs
- Action-Result-Feedback im WebSocket

---

**Problem gelöst!** 🎉 

Jetzt funktioniert die Intent-Erkennung und Action-Execution auch über WebSocket!
