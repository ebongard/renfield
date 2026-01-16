# Renfield - Persönlicher KI-Assistent

Ein vollständig offline-fähiger, selbst-gehosteter KI-Assistent für Smart Home Steuerung, Kamera-Überwachung und mehr.

## 🌟 Features

- **💬 Chat-Interface** - Text- und Sprachbasierte Kommunikation
- **🎤 Spracheingabe & -ausgabe** - Whisper STT und Piper TTS
- **🏠 Smart Home Steuerung** - Home Assistant Integration
- **📹 Kamera-Überwachung** - Frigate Integration mit Objekterkennung
- **🔄 Workflow-Automation** - n8n Integration
- **📱 Progressive Web App** - Funktioniert auf Desktop, Tablet und Smartphone
- **🔒 Vollständig Offline** - Keine Cloud-Abhängigkeiten

## 🏗️ Architektur

```
┌─────────────────────────────────────────────────────┐
│                   Frontend (React)                   │
│    - Web Interface mit Chat & Voice                 │
│    - PWA für iOS/Android                            │
└────────────────┬────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────┐
│              Backend (FastAPI)                       │
│    - REST API & WebSocket                           │
│    - Intent Recognition                             │
│    - Task Queue                                     │
└─┬─────┬─────┬─────┬─────┬─────┬─────────────────────┘
  │     │     │     │     │     │
  │     │     │     │     │     │
┌─▼─┐ ┌─▼──┐ ┌▼──┐ ┌▼───┐ ┌▼──┐ ┌▼─────┐
│HA │ │n8n │ │Cam│ │LLM │ │STT│ │ TTS  │
└───┘ └────┘ └───┘ └────┘ └───┘ └──────┘
```

## 🚀 Schnellstart

### Voraussetzungen

- Docker & Docker Compose
- Mindestens 16GB RAM (32GB empfohlen)
- Optional: NVIDIA GPU für bessere Performance

### Installation

1. **Repository klonen**
```bash
git clone <your-repo-url> renfield
cd renfield
```

2. **Umgebungsvariablen konfigurieren**
```bash
cp .env.example .env
nano .env
```

Wichtige Einstellungen in `.env`:
```env
# Home Assistant
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=dein_long_lived_access_token

# n8n
N8N_WEBHOOK_URL=http://n8n.local:5678/webhook

# Frigate
FRIGATE_URL=http://frigate.local:5000
```

3. **System starten**
```bash
docker-compose up -d
```

4. **Ollama Modell laden**
```bash
docker exec -it renfield-ollama ollama pull llama3.2:3b
```

> **Tipp:** Du kannst auch eine externe Ollama-Instanz (z.B. auf einem GPU-Server) nutzen!
> Setze einfach `OLLAMA_URL=http://cuda.local:11434` in der `.env` Datei.
> Siehe [EXTERNAL_OLLAMA.md](EXTERNAL_OLLAMA.md) für Details.

5. **Whisper Modell wird automatisch beim ersten Start geladen**

### Zugriff

- **Web-Interface**: http://localhost:3000
- **API Dokumentation**: http://localhost:8000/docs
- **Backend API**: http://localhost:8000

## 📖 Verwendung

### Chat-Interface

1. Navigiere zu **Chat** im Menü
2. Gib eine Textnachricht ein oder nutze das Mikrofon
3. Der Assistent versteht Befehle wie:
   - "Schalte das Licht im Wohnzimmer ein"
   - "Zeige mir die Kamera-Events von heute"
   - "Starte den n8n Workflow 'Backup'"
   - "Was ist die aktuelle Temperatur?"

### Sprachsteuerung

1. Klicke auf das Mikrofon-Symbol 🎤
2. Sprich deinen Befehl
3. Die Antwort kann auch vorgelesen werden (Speaker-Symbol)

### Smart Home Steuerung

1. Navigiere zu **Smart Home**
2. Suche nach Geräten oder filtere nach Typ
3. Klicke auf ein Gerät um es ein-/auszuschalten
4. Helligkeit wird automatisch angezeigt und kann angepasst werden

### Kamera-Überwachung

1. Navigiere zu **Kameras**
2. Sieh alle erkannten Objekte (Personen, Autos, Tiere)
3. Filtere nach Event-Typ
4. Benachrichtigungen werden automatisch erstellt

## 🔧 Konfiguration

### Home Assistant Integration

1. Erstelle einen Long-Lived Access Token in Home Assistant:
   - Profil → Lange Zugangstoken erstellen
2. Füge den Token in `.env` ein
3. Starte den Container neu

### n8n Workflows

Erstelle Webhooks in n8n und trage die URLs in `.env` ein:
```env
N8N_WEBHOOK_URL=http://n8n.local:5678/webhook
```

### Frigate Setup

Stelle sicher, dass Frigate läuft und konfiguriere die URL:
```env
FRIGATE_URL=http://frigate.local:5000
```

## 🛠️ Entwicklung

### Backend entwickeln

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

### Frontend entwickeln

```bash
cd frontend
npm install
npm run dev
```

### Datenbank-Migrationen

```bash
docker exec -it renfield-backend alembic revision --autogenerate -m "description"
docker exec -it renfield-backend alembic upgrade head
```

## 📱 Mobile App (iOS)

Das Frontend ist eine Progressive Web App (PWA):

1. Öffne http://your-server-ip:3000 in Safari
2. Tippe auf das Teilen-Symbol
3. Wähle "Zum Home-Bildschirm"
4. Die App verhält sich wie eine native App

## 🐛 Fehlerbehebung

### Ollama lädt nicht

```bash
docker exec -it renfield-ollama ollama pull llama3.2:3b
docker-compose restart backend
```

### Whisper Fehler

```bash
docker exec -it renfield-backend pip install --upgrade faster-whisper
docker-compose restart backend
```

### WebSocket Verbindung fehlgeschlagen

Prüfe die CORS-Einstellungen und stelle sicher, dass der Backend-Container läuft:
```bash
docker logs renfield-backend
```

## 📊 API-Endpunkte

### Chat
- `POST /api/chat/send` - Nachricht senden
- `GET /api/chat/history/{session_id}` - Historie abrufen
- `WS /ws` - WebSocket für Streaming

### Voice
- `POST /api/voice/stt` - Speech-to-Text
- `POST /api/voice/tts` - Text-to-Speech
- `POST /api/voice/voice-chat` - Kompletter Voice-Flow

### Home Assistant
- `GET /api/homeassistant/states` - Alle Entities
- `POST /api/homeassistant/turn_on/{entity_id}` - Einschalten
- `POST /api/homeassistant/turn_off/{entity_id}` - Ausschalten
- `POST /api/homeassistant/toggle/{entity_id}` - Umschalten

### Camera
- `GET /api/camera/events` - Events abrufen
- `GET /api/camera/cameras` - Kameras auflisten
- `GET /api/camera/snapshot/{event_id}` - Snapshot

### Tasks
- `POST /api/tasks/create` - Task erstellen
- `GET /api/tasks/list` - Tasks auflisten
- `GET /api/tasks/{task_id}` - Task Details

## 🔐 Sicherheit

- Alle Daten bleiben lokal auf deinem Server
- Keine Cloud-Verbindungen für Kernfunktionen
- Home Assistant Token wird sicher gespeichert
- HTTPS kann über Nginx Reverse Proxy aktiviert werden

## 🤝 Beitragen

Contributions sind willkommen! Bitte:

1. Fork das Repository
2. Erstelle einen Feature-Branch
3. Committe deine Änderungen
4. Erstelle einen Pull Request

## 📝 Lizenz

MIT License - siehe LICENSE Datei

## 🙏 Danksagungen

- [Ollama](https://ollama.ai/) - Lokales LLM
- [Whisper](https://github.com/openai/whisper) - Speech-to-Text
- [Piper](https://github.com/rhasspy/piper) - Text-to-Speech
- [Home Assistant](https://www.home-assistant.io/) - Smart Home Platform
- [Frigate](https://frigate.video/) - NVR mit Objekterkennung
- [n8n](https://n8n.io/) - Workflow Automation

## 📧 Support

Bei Fragen oder Problemen erstelle bitte ein Issue im Repository.

---

**Hinweis**: Dieses Projekt ist für den privaten Gebrauch konzipiert. Stelle sicher, dass du die Datenschutzrichtlinien deines Landes beachtest, insbesondere bei der Kamera-Überwachung.
