# Voice Input Fix - v1.2.2

## 🐛 Problem: Spracheingabe funktioniert nicht

**Symptome:**
- ✅ Mikrofon-Berechtigung erteilt
- ❌ Keine Logs im Backend
- ❌ Keine Transkription

**Mögliche Ursachen:**
1. Whisper-Modell nicht geladen
2. Fehlende Logs zum Debuggen
3. Audio-Format-Probleme
4. Request erreicht Backend nicht

## ✅ Lösung (v1.2.2)

### 1. **Detailliertes Logging hinzugefügt**

**Backend (`/api/voice/stt`):**
```python
logger.info(f"🎤 STT-Anfrage erhalten: {audio.filename}")
logger.info(f"📊 Audio-Größe: {len(audio_bytes)} bytes")
logger.info("🔄 Starte Transkription...")
logger.info(f"✅ Transkription: '{text[:100]}'")
```

**Frontend (ChatPage.jsx):**
```javascript
console.log('🎤 Starte Aufnahme...');
console.log('✅ Mikrofon-Zugriff erhalten');
console.log('📊 Audio-Daten erhalten:', event.data.size);
console.log('🛑 Aufnahme gestoppt');
console.log('📤 Sende Audio an Backend...');
console.log('✅ STT Response:', sttResponse.data);
```

### 2. **Whisper-Modell wird beim Start vorgeladen**

```python
# In main.py - lifespan()
async def preload_whisper():
    whisper_service = WhisperService()
    whisper_service.load_model()
    logger.info("✅ Whisper Service bereit (STT aktiviert)")

asyncio.create_task(preload_whisper())
```

**Vorteile:**
- ✅ Schnellere erste Transkription
- ✅ Keine Wartezeit beim ersten Gebrauch
- ✅ User-Experience verbessert

### 3. **Besseres Error-Handling**

```javascript
// Frontend
catch (error) {
  console.error('❌ Spracheingabe Fehler:', error);
  console.error('Error Details:', error.response?.data);
  
  let errorMessage = 'Spracheingabe nicht verarbeitet.';
  if (error.response?.data?.detail) {
    errorMessage += ' (' + error.response.data.detail + ')';
  }
  // Zeige Fehler an User
}
```

### 4. **Audio-Format korrigiert**

```javascript
// Vorher: 'audio/wav' (MediaRecorder macht aber webm!)
const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/wav' });

// Nachher: Korrektes Format
const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/webm' });
formData.append('audio', audioBlob, 'recording.webm');
```

**Whisper unterstützt:** wav, mp3, m4a, webm, ogg, flac

## 🔍 Debug-Schritte

### Schritt 1: Backend-Logs prüfen

```bash
# Logs in Echtzeit
docker-compose logs -f backend

# Nach Klick auf Mikrofon solltest du sehen:
✅ Whisper Service bereit (STT aktiviert)

# Nach Aufnahme solltest du sehen:
🎤 STT-Anfrage erhalten: recording.webm, Content-Type: audio/webm
📊 Audio-Größe: 45678 bytes
🔄 Starte Transkription...
✅ Transkription erfolgreich: 'Hallo, wie geht es dir?'
```

### Schritt 2: Frontend-Console prüfen

```bash
# Öffne Browser DevTools (F12) → Console Tab
# Klicke auf Mikrofon → solltest du sehen:

🎤 Starte Aufnahme...
✅ Mikrofon-Zugriff erhalten
▶️ Aufnahme läuft...

# Klicke nochmal (Stoppen) → solltest du sehen:

⏹️ Stoppe Aufnahme...
🛑 Aufnahme gestoppt, Chunks: 5
📦 Audio-Blob erstellt: 45678 bytes, Type: audio/webm
🔄 Verarbeite Spracheingabe...
📤 Sende Audio an Backend...
✅ STT Response: {text: "Hallo, wie geht es dir?", language: "de"}
📝 Transkribierter Text: Hallo, wie geht es dir?
```

### Schritt 3: Netzwerk-Requests prüfen

```bash
# Browser DevTools → Network Tab → Filter: "stt"
# Nach Aufnahme solltest du sehen:

Request:
POST /api/voice/stt
Status: 200 OK
Request Payload: FormData (audio: recording.webm)

Response:
{
  "text": "Hallo, wie geht es dir?",
  "language": "de"
}
```

## 🚀 Update durchführen

```bash
cd renfield
./quick-update.sh
```

## ✅ Testen

### Test 1: Whisper-Modell geladen?

```bash
# Prüfe Startup-Logs
docker-compose logs backend | grep Whisper

# Erwartete Ausgabe:
📥 Lade Whisper Modell 'base'...
✅ Whisper Modell geladen
✅ Whisper Service bereit (STT aktiviert)
```

**Falls nicht:** Modell wird beim ersten Gebrauch geladen (dauert ~30 Sek)

### Test 2: Mikrofon-Test

```bash
# 1. Öffne Chat: http://localhost:3000
# 2. Öffne Browser Console (F12)
# 3. Klicke auf Mikrofon-Icon
# 4. Erlaube Mikrofon-Zugriff
# 5. Sprich etwas
# 6. Klicke nochmal (Stoppen)
# 7. Prüfe Console-Logs
```

**Erwartete Console-Ausgabe:**
```
🎤 Starte Aufnahme...
✅ Mikrofon-Zugriff erhalten
▶️ Aufnahme läuft...
📊 Audio-Daten erhalten: 12345 bytes
⏹️ Stoppe Aufnahme...
🛑 Aufnahme gestoppt, Chunks: 3
📦 Audio-Blob erstellt: 45678 bytes
🔄 Verarbeite Spracheingabe...
📤 Sende Audio an Backend...
✅ STT Response: {text: "...", language: "de"}
📝 Transkribierter Text: ...
```

### Test 3: Backend-Logs prüfen

```bash
docker-compose logs -f backend | grep -E "STT|Transkription"

# Erwartete Ausgabe:
🎤 STT-Anfrage erhalten: recording.webm
📊 Audio-Größe: 45678 bytes
🔄 Starte Transkription...
✅ Transkription erfolgreich: 'Hallo'
```

## 🐛 Troubleshooting

### Problem 1: Keine Logs im Backend

**Symptom:** Kein `🎤 STT-Anfrage` Log

**Mögliche Ursachen:**
1. Request erreicht Backend nicht
2. CORS-Problem
3. Falscher API-Endpoint

**Debug:**
```bash
# Prüfe Network Tab in Browser
# Siehst du einen 404 oder 500 Error?

# Teste Backend direkt
curl -X POST http://localhost:8000/api/voice/stt \
  -F "audio=@test.wav"

# Sollte zeigen:
🎤 STT-Anfrage erhalten: test.wav
```

**Lösung:**
```bash
# Backend neu starten
docker-compose restart backend

# Oder rebuild
docker-compose up --build backend
```

### Problem 2: Whisper-Modell nicht geladen

**Symptom:** 
```
❌ Fehler beim Laden des Whisper Modells
```

**Ursache:** Modell-Download fehlgeschlagen oder keine Internet-Verbindung beim Start

**Lösung:**
```bash
# Manuell Modell laden
docker-compose exec backend python3 -c "
import whisper
model = whisper.load_model('base')
print('✅ Modell geladen')
"

# Backend neu starten
docker-compose restart backend
```

### Problem 3: Transkription dauert zu lange

**Symptom:** Lange Wartezeit (>30 Sekunden)

**Ursache:** Whisper-Modell zu groß

**Lösung:** Kleineres Modell in `.env` setzen
```bash
# In .env:
WHISPER_MODEL=tiny     # Schnellst (weniger genau)
# oder
WHISPER_MODEL=base     # Empfohlen (gut genug)
# oder
WHISPER_MODEL=small    # Besser (langsamer)
```

### Problem 4: "Keine Sprache erkannt"

**Symptom:** Frontend zeigt "Keine Sprache erkannt"

**Mögliche Ursachen:**
1. Zu kurze Aufnahme
2. Zu leise gesprochen
3. Hintergrundgeräusche

**Lösung:**
```bash
# Teste mit längerer Aufnahme (3-5 Sekunden)
# Spreche deutlich und laut
# Reduziere Hintergrundgeräusche

# Prüfe Audio-Qualität in Browser Console:
"📦 Audio-Blob erstellt: 12345 bytes"  # Sollte >10KB sein
```

### Problem 5: MediaRecorder nicht unterstützt

**Symptom:** Browser-Alert "MediaRecorder not supported"

**Lösung:** 
- Verwende modernen Browser (Chrome, Firefox, Edge)
- HTTPS erforderlich (oder localhost)
- Mikrofon-Berechtigung erteilen

## 📊 Performance

| Modell | Größe | Geschwindigkeit | Genauigkeit |
|--------|-------|-----------------|-------------|
| tiny | 39 MB | ~1-2 Sek | Niedrig |
| base | 74 MB | ~2-4 Sek | Gut |
| small | 244 MB | ~5-10 Sek | Sehr gut |
| medium | 769 MB | ~15-30 Sek | Exzellent |

**Empfehlung für Renfield:** `base` (gute Balance)

## 🔧 Geänderte Dateien

1. **backend/api/routes/voice.py**
   - ✅ Detailliertes Logging hinzugefügt
   - ✅ Besseres Error-Handling

2. **backend/main.py**
   - ✅ Whisper-Preloading beim Start

3. **frontend/src/pages/ChatPage.jsx**
   - ✅ Console-Logging für jeden Schritt
   - ✅ Audio-Format korrigiert (webm statt wav)
   - ✅ Besseres Error-Handling

## 📝 Changelog v1.2.2

**Fixed:**
- ❌ Fehlende Logs bei Spracheingabe
- ❌ Whisper-Modell wurde nicht vorgeladen
- ❌ Falsches Audio-Format (wav statt webm)

**Added:**
- ✅ Detailliertes Logging (Backend + Frontend)
- ✅ Whisper-Preloading beim Start
- ✅ Bessere Fehlerdiagnostik

**Improved:**
- 🚀 Schnellere erste Transkription
- 🚀 Bessere User-Feedback
- 🚀 Einfacheres Debugging

## 🎯 Schnell-Diagnose

```bash
# Komplett-Check in einem Befehl:
echo "1. Backend-Logs:" && \
docker-compose logs backend | grep -E "Whisper|STT" | tail -5 && \
echo -e "\n2. Test STT-Endpoint:" && \
curl -s http://localhost:8000/health && \
echo -e "\n\n3. Öffne Browser Console und klicke Mikrofon"
```

---

**Jetzt mit vollständigem Logging und Whisper-Preloading!** 🎤

Update mit `./quick-update.sh` und prüfe die Logs! 🚀
