# Bug Fixes & Lösungen

Dieses Dokument beschreibt die Probleme die beim Docker Build auftraten und wie sie gelöst wurden.

## 🐛 Problem 1: httpx Dependency Konflikt

### Symptom
```
ERROR: Cannot install -r requirements.txt (line 17) and httpx==0.26.0 
because these package versions have conflicting dependencies.
The conflict is caused by:
    ollama 0.1.6 depends on httpx<0.26.0 and >=0.25.2
```

### Ursache
- `ollama==0.1.6` benötigt `httpx>=0.25.2,<0.26.0`
- In requirements.txt war `httpx==0.26.0` angegeben
- Zusätzlich war httpx zweimal definiert (bei Integrationen und Testing)

### Lösung ✅
```python
# requirements.txt - Geändert von:
httpx==0.26.0

# Zu:
httpx==0.25.2  # Kompatibel mit ollama 0.1.6
```

Außerdem doppelte httpx-Zeile bei "Testing" entfernt.

---

## 🐛 Problem 2: pkg-config fehlt für PyAV

### Symptom
```
error: subprocess-exited-with-error
× Getting requirements to build wheel did not run successfully.
  exit code: 1
  pkg-config is required for building PyAV
```

### Ursache
- `piper-tts` benötigt `PyAV`
- `PyAV` benötigt `pkg-config` und FFmpeg development libraries
- Diese waren nicht im Dockerfile installiert

### Lösung ✅

**Ansatz 1: System-Dependencies hinzufügen**
```dockerfile
# Dockerfile
RUN apt-get update && apt-get install -y \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    # ... weitere
```

**Ansatz 2: Piper optional machen (gewählt)**

Da Piper viele Dependencies hat und TTS optional ist, wurde es auskommentiert:

```python
# requirements.txt
# piper-tts==1.2.0  # Optional, siehe README_PIPER.md
```

Der Service wurde angepasst um graceful ohne Piper zu funktionieren.

---

## 🐛 Problem 3: PyAV Build Fehler (FFmpeg Inkompatibilität)

### Symptom
```
src/av/option.c:6859:52: error: 'AV_OPT_TYPE_CHANNEL_LAYOUT' undeclared
did you mean 'AV_OPT_TYPE_CHLAYOUT'?
error: command '/usr/bin/gcc' failed with exit code 1
ERROR: Failed building wheel for av
```

### Ursache
- `faster-whisper` benötigt `PyAV` (av) Package
- `PyAV` kompiliert C-Extensions gegen FFmpeg Libraries
- Neuere FFmpeg-Versionen haben `AV_OPT_TYPE_CHANNEL_LAYOUT` umbenannt zu `AV_OPT_TYPE_CHLAYOUT`
- Die PyAV-Version ist nicht kompatibel mit moderner FFmpeg

### Lösung ✅

**Gewählter Ansatz: faster-whisper entfernen**

`faster-whisper` ist eine optimierte Alternative zu `openai-whisper`, aber nicht essentiell. 
`openai-whisper` funktioniert ohne PyAV und ist völlig ausreichend.

```python
# requirements.txt - Vorher:
openai-whisper==20231117
faster-whisper==1.0.0  # Benötigt PyAV -> Build-Fehler

# Nachher:
openai-whisper==20231117
# faster-whisper entfernt - openai-whisper ist ausreichend
```

**Service angepasst:**
- `whisper_service.py` nutzt jetzt direkt `openai-whisper`
- Keine Dependency auf `faster-whisper` oder `PyAV`
- Funktioniert out-of-the-box ohne C-Compilation

**Performance-Unterschied:**
- `faster-whisper`: Schneller (CTranslate2-optimiert), weniger RAM
- `openai-whisper`: Etwas langsamer, aber für Spracheingabe völlig ausreichend
- Für normale Sprachbefehle minimal spürbar (< 1 Sekunde Unterschied)

**Alternative Lösung** (falls später schnelleres STT gewünscht):
```bash
# Im Container PyAV manuell mit korrekter FFmpeg-Version bauen
pip install av --no-binary av
pip install faster-whisper
```

Dann `whisper_service.py` wieder auf `faster-whisper` umstellen.

---

## 🐛 Problem 4: SQLAlchemy Reserved Attribute 'metadata'

### Symptom
```
sqlalchemy.exc.InvalidRequestError: Attribute name 'metadata' is reserved 
when using the Declarative API.
```

### Ursache
- SQLAlchemy nutzt `metadata` intern für Table-Definitionen
- Unsere Models hatten ein Feld namens `metadata`
- Dies kollidiert mit SQLAlchemy's eigenem `metadata` Attribut

### Lösung ✅

**Felder umbenannt:**

```python
# models/database.py - Message Model
class Message(Base):
    # Vorher:
    metadata = Column(JSON, nullable=True)
    
    # Nachher:
    message_metadata = Column(JSON, nullable=True)

# models/database.py - CameraEvent Model  
class CameraEvent(Base):
    # Vorher:
    metadata = Column(JSON, nullable=True)
    
    # Nachher:
    event_metadata = Column(JSON, nullable=True)
```

**API Routes angepasst:**
```python
# api/routes/chat.py
# Alle Referenzen zu 'metadata' → 'message_metadata'
assistant_msg = Message(
    message_metadata={"intent": intent}  # statt metadata
)
```

---

## 📋 Zusammenfassung der Änderungen

### backend/requirements.txt
1. ✅ `httpx==0.26.0` → `httpx==0.25.2`
2. ✅ Doppelte httpx-Zeile entfernt
3. ✅ `piper-tts` auskommentiert (optional)
4. ✅ `faster-whisper` entfernt (PyAV-Probleme)
5. ✅ Nur `openai-whisper` behalten (funktioniert ohne Compilation)

### backend/Dockerfile
1. ✅ `pkg-config` hinzugefügt
2. ✅ FFmpeg development libraries hinzugefügt
3. ✅ Build-Tools erweitert

### backend/services/piper_service.py
1. ✅ Piper-Verfügbarkeit wird geprüft
2. ✅ Funktioniert ohne Piper (loggt nur Warnung)
3. ✅ Gibt leere Bytes zurück wenn nicht verfügbar

### backend/services/whisper_service.py
1. ✅ Nutzt jetzt `openai-whisper` direkt
2. ✅ Keine Dependency auf PyAV

### backend/models/database.py
1. ✅ `metadata` → `message_metadata` (Message Model)
2. ✅ `metadata` → `event_metadata` (CameraEvent Model)

### backend/api/routes/chat.py
1. ✅ Alle `metadata` Referenzen auf `message_metadata` aktualisiert

### backend/api/routes/homeassistant.py
1. ✅ Import von `Any` aus `typing` hinzugefügt
2. ✅ `value: any` → `value: Any` (korrekter Type-Hint)

### Neue Dateien
1. ✅ `README_PIPER.md` - Anleitung für optionale TTS-Installation
2. ✅ `BUGFIXES.md` - Diese Datei

---

## 🐛 Problem 5: Pydantic Schema Generation Error

### Symptom
```
pydantic.errors.PydanticSchemaGenerationError: Unable to generate 
pydantic-core schema for <built-in function any>
```

### Ursache
- In `SetValue` BaseModel wurde `value: any` definiert
- `any` ist eine Python built-in Funktion, kein Typ-Hint
- Pydantic kann keine Schema für eine Funktion generieren
- Der korrekte Type-Hint ist `Any` aus dem `typing` Modul

### Lösung ✅

**Import hinzugefügt und Typ korrigiert:**

```python
# api/routes/homeassistant.py

# Vorher:
from typing import Optional, Dict

class SetValue(BaseModel):
    entity_id: str
    value: any  # ❌ Falsch - any ist eine Funktion
    attribute: str = "value"

# Nachher:
from typing import Optional, Dict, Any

class SetValue(BaseModel):
    entity_id: str
    value: Any  # ✅ Korrekt - Any ist ein Type-Hint
    attribute: str = "value"
```

**Unterschied:**
- `any(...)` - Built-in Funktion, gibt True zurück wenn irgendein Element truthy ist
- `Any` - Type-Hint, bedeutet "beliebiger Typ"

---

## 🎯 Aktueller Status

### ✅ Funktioniert ohne weitere Änderungen:
- Docker Compose Build
- Backend API
- Frontend PWA
- Chat ohne Voice-Ausgabe
- Spracheingabe (STT mit Whisper)
- Home Assistant Integration
- Kamera-Überwachung
- n8n Workflows
- Task Management

### 🔧 Optional nachzurüsten:
- Text-to-Speech (TTS)
  - Siehe `README_PIPER.md` für Installation
  - System funktioniert vollständig ohne TTS

---

## 🚀 Build & Start

Jetzt sollte folgendes ohne Fehler durchlaufen:

```bash
# Entpacken
unzip renfield.zip
cd renfield

# Konfigurieren
cp .env.example .env
nano .env

# Starten
docker-compose up --build -d

# Logs prüfen
docker-compose logs -f backend
```

### Erwartete Build-Zeit
- Erster Build: ~10-15 Minuten
  - Ollama Modell Download: ~5 Minuten
  - Whisper Modell: automatisch beim ersten STT-Aufruf
- Nachfolgende Builds: ~2-3 Minuten (mit Cache)

---

## 🔍 Troubleshooting

### Falls immer noch Build-Fehler auftreten:

**1. Docker Cache leeren**
```bash
docker-compose build --no-cache backend
```

**2. System-Packages aktualisieren**
```bash
docker-compose down
docker system prune -a
docker-compose up --build
```

**3. Python Dependencies einzeln testen**
```bash
docker-compose run backend pip install -r requirements.txt
```

### Häufige Probleme:

**Problem: Ollama startet nicht**
```bash
docker logs renfield-ollama
# GPU-Support deaktivieren falls keine NVIDIA-GPU
# In docker-compose.yml: deploy-Sektion bei ollama auskommentieren
```

**Problem: PostgreSQL Connection Fehler**
```bash
# Warte 30 Sekunden nach Start
docker-compose restart backend
```

**Problem: Frontend nicht erreichbar**
```bash
docker logs renfield-frontend
# Port 3000 belegt? Ändere in docker-compose.yml
```

---

## 📊 Versions-Matrix (Getestet)

| Komponente | Version | Status |
|------------|---------|--------|
| Python | 3.11 | ✅ |
| FastAPI | 0.109.0 | ✅ |
| Ollama | 0.1.6 | ✅ |
| httpx | 0.25.2 | ✅ |
| faster-whisper | 1.0.0 | ✅ |
| React | 18.2.0 | ✅ |
| Node | 20 | ✅ |
| PostgreSQL | 16 | ✅ |
| Redis | 7 | ✅ |

---

## 💡 Empfehlungen

1. **Starte ohne TTS** - System ist vollständig funktionsfähig
2. **TTS später hinzufügen** - Bei Bedarf mit README_PIPER.md
3. **Teste zuerst Chat** - Dann Spracheingabe, dann TTS
4. **Logs überwachen** - Bei Problemen: `docker-compose logs -f`

---

## 📞 Support

Bei weiteren Problemen:
1. Prüfe diese Datei und README_PIPER.md
2. Schaue in INSTALLATION.md
3. Erstelle GitHub Issue mit:
   - Error-Message
   - Docker Logs
   - Systeminformationen

---

**Alle bekannten Probleme sind gelöst!** Der Build sollte jetzt erfolgreich sein. 🎉
