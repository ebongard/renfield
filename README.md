# Renfield - Persönlicher KI-Assistent

Ein vollständig offline-fähiger, selbst-gehosteter KI-Assistent für Smart Home Steuerung, Kamera-Überwachung und mehr.

## Features

- **Chat-Interface** - Text- und Sprachbasierte Kommunikation
- **Spracheingabe & -ausgabe** - Whisper STT und Piper TTS
- **Multi-Room Voice Control** - Raspberry Pi Satellite Sprachassistenten
- **Smart Home Steuerung** - Home Assistant Integration
- **Kamera-Überwachung** - Frigate Integration mit Objekterkennung
- **Workflow-Automation** - n8n Integration
- **Dynamisches Plugin-System** - Einfache Integration externer APIs (Wetter, News, Musik, Suche)
- **Progressive Web App** - Funktioniert auf Desktop, Tablet und Smartphone
- **Vollständig Offline** - Keine Cloud-Abhängigkeiten
- **GPU-Beschleunigung** - Optional NVIDIA GPU für schnellere Transkription

## Architektur

```
┌─────────────────────────────────────────────────────────────────────┐
│                      RENFIELD ECOSYSTEM                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐             │
│  │  Satellite  │    │  Satellite  │    │  Satellite  │             │
│  │ Wohnzimmer  │    │   Küche     │    │ Schlafzimmer│             │
│  │ Pi Zero 2 W │    │ Pi Zero 2 W │    │ Pi Zero 2 W │             │
│  │ + ReSpeaker │    │ + ReSpeaker │    │ + ReSpeaker │             │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘             │
│         │                  │                  │                     │
│         └────────┬─────────┴─────────┬────────┘                     │
│                  │   WebSocket       │                              │
│                  │ /ws/satellite     │                              │
│                  ▼                   ▼                              │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │                  Frontend (React PWA)                          │ │
│  │      - Web Interface mit Chat & Voice                         │ │
│  │      - PWA für iOS/Android                                    │ │
│  └────────────────────────┬──────────────────────────────────────┘ │
│                           │ WebSocket /ws                          │
│  ┌────────────────────────▼──────────────────────────────────────┐ │
│  │                   Backend (FastAPI)                            │ │
│  │  ┌────────────────┐  ┌──────────────┐  ┌───────────────────┐  │ │
│  │  │SatelliteManager│  │ OllamaService│  │  ActionExecutor   │  │ │
│  │  └────────────────┘  └──────────────┘  └───────────────────┘  │ │
│  │  ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌──────────────────┐  │ │
│  │  │ Whisper │ │  Piper   │ │   Redis   │ │    PostgreSQL    │  │ │
│  │  │  (STT)  │ │  (TTS)   │ │  (Queue)  │ │   (Database)     │  │ │
│  │  └─────────┘ └──────────┘ └───────────┘ └──────────────────┘  │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                           │                                        │
│  ┌────────────────────────▼──────────────────────────────────────┐ │
│  │               External Integrations                            │ │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │ │
│  │  │  Ollama  │ │   Home   │ │ Frigate  │ │       n8n        │  │ │
│  │  │  (LLM)   │ │Assistant │ │  (NVR)   │ │   (Workflows)    │  │ │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘  │ │
│  └───────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

## Schnellstart

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

**Entwicklung auf Mac:**
```bash
docker compose -f docker-compose.dev.yml up -d
```

**Produktion mit NVIDIA GPU:**
```bash
docker compose -f docker-compose.prod.yml up -d
```

**Standard (ohne GPU):**
```bash
docker compose up -d
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

## Docker Compose Varianten

| Datei | Verwendung | GPU | Beschreibung |
|-------|------------|-----|--------------|
| `docker-compose.yml` | Standard | Nein | Basis-Setup für die meisten Anwendungsfälle |
| `docker-compose.dev.yml` | Entwicklung | Nein | Mac-Entwicklung mit exponierten Debug-Ports |
| `docker-compose.prod.yml` | Produktion | Ja | NVIDIA GPU-Support, nginx mit SSL |

### Entwicklung auf Mac
```bash
# Standard (ohne lokales Ollama)
docker compose -f docker-compose.dev.yml up -d

# Mit lokalem Ollama-Container
docker compose -f docker-compose.dev.yml --profile ollama up -d
```

### Produktion mit GPU
```bash
# Voraussetzungen: NVIDIA Container Toolkit installiert
docker compose -f docker-compose.prod.yml up -d

# Mit lokalem GPU-Ollama
docker compose -f docker-compose.prod.yml --profile ollama-gpu up -d
```

## Multi-Room Satellite System

Renfield unterstützt Multi-Room Sprachassistenten basierend auf Raspberry Pi Zero 2 W mit ReSpeaker 2-Mics Pi HAT.

### Features

- **Lokale Wake-Word-Erkennung** mit OpenWakeWord (CPU ~20%)
- **Auto-Discovery** via Zeroconf/mDNS
- **WebSocket-Streaming** für Audio
- **LED-Feedback** (Idle, Listening, Processing, Speaking)
- **Hardware-Button** für manuelle Aktivierung

### Hardware pro Satellite (~63€)

| Komponente | Preis |
|------------|-------|
| Raspberry Pi Zero 2 W | ~18€ |
| ReSpeaker 2-Mics Pi HAT V2.0 | ~12€ |
| MicroSD Card 16GB | ~8€ |
| 5V/2A Netzteil | ~10€ |
| 3.5mm Lautsprecher | ~10€ |
| Gehäuse (optional) | ~5€ |

### Schnellstart Satellite

```bash
# Auf dem Raspberry Pi
cd /opt/renfield-satellite
source venv/bin/activate
python -m renfield_satellite config/satellite.yaml
```

**Vollständige Anleitung:** [renfield-satellite/README.md](renfield-satellite/README.md)

## Verwendung

### Chat-Interface

1. Navigiere zu **Chat** im Menü
2. Gib eine Textnachricht ein oder nutze das Mikrofon
3. Der Assistent versteht Befehle wie:
   - "Schalte das Licht im Wohnzimmer ein"
   - "Zeige mir die Kamera-Events von heute"
   - "Starte den n8n Workflow 'Backup'"
   - "Was ist die aktuelle Temperatur?"

### Sprachsteuerung

1. Klicke auf das Mikrofon-Symbol
2. Sprich deinen Befehl
3. Die Antwort kann auch vorgelesen werden (Speaker-Symbol)

### Satellite Sprachsteuerung

1. Sage das Wake-Word ("Alexa" standardmäßig)
2. LEDs werden grün (Listening)
3. Sprich deinen Befehl
4. LEDs werden gelb (Processing)
5. Antwort wird über Lautsprecher abgespielt

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

## Konfiguration

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

### GPU-Beschleunigung für Whisper

Für schnellere Spracherkennung auf NVIDIA GPUs:

1. Installiere NVIDIA Container Toolkit:
```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

2. Starte mit GPU-Profil:
```bash
docker compose -f docker-compose.prod.yml up -d
```

## Plugin System

Renfield verfügt über ein **dynamisches, YAML-basiertes Plugin-System**, das es ermöglicht, externe APIs und Services ohne Code-Änderungen zu integrieren.

### Verfügbare Plugins

#### Weather Plugin (OpenWeatherMap)
Aktuelle Wetterdaten und Vorhersagen.

**Aktivierung:**
```bash
# In .env hinzufügen:
WEATHER_ENABLED=true
OPENWEATHER_API_URL=https://api.openweathermap.org/data/2.5
OPENWEATHER_API_KEY=dein_api_key
```

**API-Key erhalten:** https://openweathermap.org/api

**Beispiele:**
- "Wie ist das Wetter in Berlin?"
- "Wettervorhersage für München"

---

#### News Plugin (NewsAPI)
Aktuelle Nachrichten und Schlagzeilen.

**Aktivierung:**
```bash
NEWS_ENABLED=true
NEWSAPI_URL=https://newsapi.org/v2
NEWSAPI_KEY=dein_api_key
```

**API-Key erhalten:** https://newsapi.org/register

**Beispiele:**
- "Zeige mir die Nachrichten"
- "Suche Nachrichten über Tesla"

---

#### Search Plugin (SearXNG)
Web-Suche mit SearXNG Metasearch Engine - **Kein API-Key nötig!**

**Aktivierung:**
```bash
SEARCH_ENABLED=true
SEARXNG_API_URL=http://cuda.local:3002
```

**Hinweis:** Benötigt eine laufende SearXNG-Instanz.

**Beispiele:**
- "Suche nach Python Tutorials"
- "Was ist Quantencomputing?"
- "Wie funktioniert Photosynthese?"

---

#### Music Plugin (Spotify)
Musik-Steuerung über Spotify.

**Aktivierung:**
```bash
MUSIC_ENABLED=true
SPOTIFY_API_URL=https://api.spotify.com
SPOTIFY_ACCESS_TOKEN=dein_access_token
```

**Access Token erhalten:** https://developer.spotify.com/console/

**Beispiele:**
- "Spiele Musik von Coldplay"
- "Nächster Song"
- "Lautstärke auf 50"

---

### Eigenes Plugin erstellen

Erstelle eine YAML-Datei in `backend/integrations/plugins/`:

```yaml
name: mein_plugin
version: 1.0.0
description: Meine Integration
enabled_var: MEIN_PLUGIN_ENABLED

config:
  url: MEIN_PLUGIN_API_URL
  api_key: MEIN_PLUGIN_API_KEY

intents:
  - name: mein_plugin.aktion
    description: Führt eine Aktion aus
    parameters:
      - name: query
        type: string
        required: true
    examples:
      - "Mache etwas"
    api:
      method: GET
      url: "{config.url}/endpoint?q={params.query}&key={config.api_key}"
      timeout: 10
      response_mapping:
        result: "data.result"
```

Setze die Umgebungsvariablen in `.env`:
```bash
MEIN_PLUGIN_ENABLED=true
MEIN_PLUGIN_API_URL=https://api.example.com
MEIN_PLUGIN_API_KEY=dein_key
```

Starte den Container neu:
```bash
docker compose up -d --force-recreate backend
```

**Fertig!** Keine Code-Änderungen nötig.

### Plugin-Dokumentation

Vollständige Dokumentation, Beispiele und Troubleshooting:
[Plugin Development Guide](backend/integrations/plugins/README.md)

## Entwicklung

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

## Mobile App (iOS)

Das Frontend ist eine Progressive Web App (PWA):

1. Öffne http://your-server-ip:3000 in Safari
2. Tippe auf das Teilen-Symbol
3. Wähle "Zum Home-Bildschirm"
4. Die App verhält sich wie eine native App

## Fehlerbehebung

### Ollama lädt nicht

```bash
docker exec -it renfield-ollama ollama pull llama3.2:3b
docker compose restart backend
```

### Whisper Fehler

```bash
docker exec -it renfield-backend pip install --upgrade openai-whisper
docker compose restart backend
```

### WebSocket Verbindung fehlgeschlagen

Prüfe die CORS-Einstellungen und stelle sicher, dass der Backend-Container läuft:
```bash
docker logs renfield-backend
```

### Satellite findet Backend nicht

```bash
# Prüfe ob Backend Zeroconf advertised
docker compose logs backend | grep zeroconf

# Manuelle URL in satellite config setzen
# config/satellite.yaml:
server:
  auto_discover: false
  url: "ws://renfield.local:8000/ws/satellite"
```

## API-Endpunkte

### Chat
- `POST /api/chat/send` - Nachricht senden
- `GET /api/chat/history/{session_id}` - Historie abrufen
- `WS /ws` - WebSocket für Streaming

### Voice
- `POST /api/voice/stt` - Speech-to-Text
- `POST /api/voice/tts` - Text-to-Speech
- `POST /api/voice/voice-chat` - Kompletter Voice-Flow

### Satellite
- `WS /ws/satellite` - WebSocket für Satellite-Verbindungen

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

## Sicherheit

- Alle Daten bleiben lokal auf deinem Server
- Keine Cloud-Verbindungen für Kernfunktionen
- Home Assistant Token wird sicher gespeichert
- HTTPS kann über Nginx Reverse Proxy aktiviert werden

## Beitragen

Contributions sind willkommen! Bitte:

1. Fork das Repository
2. Erstelle einen Feature-Branch
3. Committe deine Änderungen
4. Erstelle einen Pull Request

## Lizenz

MIT License - siehe LICENSE Datei

## Danksagungen

- [Ollama](https://ollama.ai/) - Lokales LLM
- [Whisper](https://github.com/openai/whisper) - Speech-to-Text
- [Piper](https://github.com/rhasspy/piper) - Text-to-Speech
- [Home Assistant](https://www.home-assistant.io/) - Smart Home Platform
- [Frigate](https://frigate.video/) - NVR mit Objekterkennung
- [n8n](https://n8n.io/) - Workflow Automation
- [OpenWakeWord](https://github.com/dscripka/openWakeWord) - Wake Word Detection

## Support

Bei Fragen oder Problemen erstelle bitte ein Issue im Repository.

---

**Hinweis**: Dieses Projekt ist für den privaten Gebrauch konzipiert. Stelle sicher, dass du die Datenschutzrichtlinien deines Landes beachtest, insbesondere bei der Kamera-Überwachung.
