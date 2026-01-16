# Piper TTS - Optionale Installation

Piper TTS (Text-to-Speech) ist optional und kann bei Bedarf nachträglich installiert werden.

## 🎯 Status

**Aktuell:** TTS ist deaktiviert, aber das System funktioniert vollständig ohne TTS.

**Funktionen ohne TTS:**
- ✅ Chat funktioniert
- ✅ Spracheingabe (STT) funktioniert
- ✅ Home Assistant Steuerung funktioniert
- ✅ Kamera-Überwachung funktioniert
- ❌ Sprachausgabe (TTS) fehlt

## 📦 Warum ist TTS optional?

Piper TTS hat viele System-Dependencies (PyAV, FFmpeg libraries) die Probleme beim Build verursachen können. Daher ist es auskommentiert und kann bei Bedarf manuell nachinstalliert werden.

## 🚀 TTS nachträglich installieren

### Option 1: In laufendem Container

```bash
# In Container einloggen
docker exec -it renfield-backend bash

# System-Dependencies installieren
apt-get update && apt-get install -y \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev

# Piper installieren
pip install piper-tts

# Container neu starten
exit
docker-compose restart backend
```

### Option 2: Dockerfile anpassen

1. **requirements.txt bearbeiten:**
```python
# Text-to-Speech (Piper)
piper-tts==1.2.0  # Kommentar entfernen
```

2. **Container neu bauen:**
```bash
docker-compose build backend
docker-compose up -d
```

## 🔧 Alternative TTS-Lösungen

Falls Piper Probleme macht, gibt es Alternativen:

### 1. gTTS (Google Text-to-Speech)
```bash
pip install gtts
```

Einfacher, aber benötigt Internet-Verbindung.

### 2. eSpeak
```bash
apt-get install espeak
pip install py-espeak-ng
```

Vollständig offline, aber robotische Stimme.

### 3. Mozilla TTS
```bash
pip install TTS
```

Hochwertig, aber größerer Download.

## 📝 Code-Anpassung für alternative TTS

Wenn du eine andere TTS-Engine verwenden möchtest, passe `backend/services/piper_service.py` an oder erstelle einen neuen Service.

## ✅ Testen ob TTS funktioniert

Nach Installation:

```bash
# Im Container
docker exec -it renfield-backend python3 -c "
from services.piper_service import PiperService
import asyncio

async def test():
    piper = PiperService()
    print('Piper verfügbar:', piper.available)

asyncio.run(test())
"
```

Sollte ausgeben: `Piper verfügbar: True`

## 🎤 Frontend ohne TTS

Das Frontend funktioniert auch ohne TTS:
- Der "Vorlesen" Button wird angezeigt
- Bei Klick wird eine Warnung angezeigt: "TTS nicht verfügbar"
- Alle anderen Funktionen arbeiten normal

## 📞 Hilfe

Bei Problemen mit der TTS-Installation:
1. Prüfe Docker Logs: `docker-compose logs backend`
2. Prüfe Piper-Status im Container
3. Erstelle ein GitHub Issue

---

**Empfehlung:** Starte erst mal ohne TTS, das System ist auch so vollständig funktionsfähig. TTS kann später jederzeit nachgerüstet werden!
