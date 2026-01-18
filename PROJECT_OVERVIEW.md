# Renfield - Projekt-Übersicht

## Projektstruktur

```
renfield/
├── README.md                       # Hauptdokumentation
├── QUICKSTART.md                   # Schnellstart-Anleitung
├── INSTALLATION.md                 # Detaillierte Setup-Anleitung
├── FEATURES.md                     # Feature-Dokumentation
├── CLAUDE.md                       # Claude Code Anweisungen
├── EXTERNAL_OLLAMA.md              # Externe Ollama-Dokumentation
├── docker-compose.yml              # Standard Docker Services
├── docker-compose.dev.yml          # Entwicklung (Mac)
├── docker-compose.prod.yml         # Produktion (GPU)
├── .env.example                    # Umgebungsvariablen Template
├── .gitignore                      # Git Ignore
├── start.sh                        # Startup Script
├── update.sh                       # Update Script
│
├── backend/                        # Python Backend
│   ├── Dockerfile                  # CPU-Version
│   ├── Dockerfile.gpu              # GPU-Version (CUDA)
│   ├── requirements.txt
│   ├── main.py                     # FastAPI App
│   ├── api/
│   │   └── routes/                 # API Endpoints
│   │       ├── chat.py
│   │       ├── voice.py
│   │       ├── tasks.py
│   │       ├── camera.py
│   │       └── homeassistant.py
│   ├── services/                   # Business Logic
│   │   ├── database.py
│   │   ├── ollama_service.py
│   │   ├── whisper_service.py
│   │   ├── piper_service.py
│   │   ├── satellite_manager.py    # Satellite Session Management
│   │   └── task_queue.py
│   ├── integrations/               # Externe Integrationen
│   │   ├── homeassistant.py
│   │   ├── n8n.py
│   │   ├── frigate.py
│   │   └── plugins/                # YAML Plugin System
│   │       ├── README.md
│   │       ├── weather.yaml
│   │       ├── news.yaml
│   │       ├── search.yaml
│   │       └── music.yaml
│   ├── models/                     # Datenbank Models
│   │   └── database.py
│   └── utils/                      # Hilfsfunktionen
│       └── config.py
│
├── frontend/                       # React Frontend
│   ├── Dockerfile
│   ├── package.json
│   ├── vite.config.js
│   ├── tailwind.config.js
│   ├── index.html
│   └── src/
│       ├── main.jsx
│       ├── App.jsx
│       ├── index.css
│       ├── components/
│       │   └── Layout.jsx
│       ├── pages/
│       │   ├── HomePage.jsx
│       │   ├── ChatPage.jsx        # Chat mit Voice
│       │   ├── TasksPage.jsx
│       │   ├── CameraPage.jsx
│       │   └── HomeAssistantPage.jsx
│       └── utils/
│           └── axios.js
│
├── renfield-satellite/             # Raspberry Pi Satellite Software
│   ├── README.md                   # Vollständige Satellite-Dokumentation
│   ├── renfield_satellite/
│   │   ├── __init__.py
│   │   ├── __main__.py             # Entry point
│   │   ├── config.py               # Konfiguration
│   │   ├── satellite.py            # Haupt-Satellite-Klasse
│   │   ├── audio/
│   │   │   ├── capture.py          # Mikrofonaufnahme (PyAudio/ALSA)
│   │   │   └── playback.py         # Lautsprecherausgabe
│   │   ├── wakeword/
│   │   │   └── detector.py         # OpenWakeWord Integration
│   │   ├── hardware/
│   │   │   ├── led.py              # APA102 RGB LED Steuerung
│   │   │   └── button.py           # GPIO Button
│   │   └── network/
│   │       └── websocket_client.py # WebSocket Verbindung
│   └── config/
│       └── satellite.yaml          # Beispiel-Konfiguration
│
├── config/                         # Server-Konfigurationsdateien
│   ├── nginx.conf                  # Nginx Reverse Proxy (HTTPS)
│   └── nginx-dev.conf              # Nginx ohne SSL (Entwicklung)
│
└── docs/                           # Zusätzliche Dokumentation
    └── ENVIRONMENT_VARIABLES.md    # Umgebungsvariablen-Referenz
```

## Hauptkomponenten

### Backend (Python/FastAPI)
- **REST API** für alle Funktionen
- **WebSocket** für Echtzeit-Chat (`/ws`)
- **WebSocket** für Satellites (`/ws/satellite`)
- **Ollama Integration** für lokales LLM
- **Whisper** für Speech-to-Text (mit GPU-Support)
- **Piper** für Text-to-Speech
- **PostgreSQL** als Datenbank
- **Redis** als Message Queue
- **Zeroconf** für Service Discovery

### Frontend (React)
- **Single Page Application** mit React Router
- **Tailwind CSS** für Styling
- **Progressive Web App** (PWA)
- **WebSocket** für Live-Updates
- **Responsive Design** für alle Geräte

### Satellite (Raspberry Pi)
- **Wake-Word-Erkennung** mit OpenWakeWord
- **Audio-Streaming** über WebSocket
- **LED-Feedback** mit APA102 RGB LEDs
- **Button-Steuerung** über GPIO
- **Auto-Discovery** via Zeroconf/mDNS

### Integrationen
- **Home Assistant** - Smart Home Steuerung
- **Frigate** - Kamera NVR mit KI-Objekterkennung
- **n8n** - Workflow Automation
- **Plugin System** - YAML-basierte API-Integrationen

## Quick Start

### Entwicklung auf Mac
```bash
# 1. .env konfigurieren
cp .env.example .env
nano .env

# 2. System starten
docker compose -f docker-compose.dev.yml up -d

# 3. Im Browser öffnen
# http://localhost:3000
```

### Produktion mit GPU
```bash
# 1. .env konfigurieren
cp .env.example .env
nano .env

# 2. System starten
docker compose -f docker-compose.prod.yml up -d

# 3. Im Browser öffnen
# http://localhost:3000 (oder über nginx auf Port 80/443)
```

## Docker Compose Varianten

| Datei | Verwendung | Features |
|-------|------------|----------|
| `docker-compose.yml` | Standard | Basis-Setup |
| `docker-compose.dev.yml` | Entwicklung | Debug-Ports, Mac-freundlich |
| `docker-compose.prod.yml` | Produktion | NVIDIA GPU, nginx |

## Wichtige Dateien

### Für Entwickler
- `backend/main.py` - FastAPI Application Entry Point
- `backend/services/satellite_manager.py` - Satellite Session Management
- `frontend/src/App.jsx` - React Application Entry Point
- `docker-compose.yml` - Service Orchestrierung
- `backend/Dockerfile.gpu` - GPU-fähiges Docker Image

### Für Betreiber
- `.env` - Umgebungsvariablen und Konfiguration
- `start.sh` - System starten
- `update.sh` - System aktualisieren
- `INSTALLATION.md` - Setup-Anleitung

### Für Satellite-Setup
- `renfield-satellite/README.md` - Vollständige Anleitung
- `renfield-satellite/config/satellite.yaml` - Konfiguration
- `renfield-satellite/renfield_satellite/` - Python-Paket

### Für Anwender
- `README.md` - Übersicht und Schnellstart
- `FEATURES.md` - Alle Features im Detail

## Key Features

1. **Vollständig Offline** - Keine Cloud-Abhängigkeiten
2. **Voice Interface** - Sprechen und Hören
3. **Multi-Room Satellites** - Raspberry Pi Sprachassistenten
4. **Smart Home Control** - Home Assistant Integration
5. **Camera Monitoring** - Frigate Integration
6. **Task Automation** - n8n Workflows
7. **Plugin System** - YAML-basierte API-Integrationen
8. **Mobile Ready** - PWA für iOS/Android
9. **GPU Support** - NVIDIA CUDA für Whisper

## Technologie-Stack

### Backend
- Python 3.11
- FastAPI (Web Framework)
- SQLAlchemy (ORM)
- Ollama (LLM)
- Whisper (STT)
- Piper (TTS)
- Zeroconf (Service Discovery)

### Frontend
- React 18
- Vite (Build Tool)
- Tailwind CSS
- Axios (HTTP Client)
- Lucide Icons

### Satellite
- Python 3.11
- OpenWakeWord (Wake Word Detection)
- PyAudio (Audio Capture via ALSA)
- python-mpv (Audio Playback)
- spidev (LED Control)
- RPi.GPIO (Button)

### Infrastructure
- Docker & Docker Compose
- PostgreSQL 16
- Redis 7
- Nginx (optional)
- NVIDIA Container Toolkit (optional)

## Nächste Schritte

1. **Installation**: Folge `INSTALLATION.md`
2. **Konfiguration**: Passe `.env` an deine Bedürfnisse an
3. **Start**: Führe `./start.sh` oder `docker compose` aus
4. **Test**: Öffne http://localhost:3000 und teste die Features
5. **Satellites**: Optional Raspberry Pi Satellites einrichten
6. **Anpassung**: Erweitere das System nach deinen Wünschen

## Entwicklung

### Backend erweitern
```bash
cd backend
# Neue Route in api/routes/ erstellen
# Neue Integration in integrations/ hinzufügen
# Neuen Service in services/ erstellen
```

### Frontend erweitern
```bash
cd frontend
# Neue Page in src/pages/ erstellen
# Neue Component in src/components/ erstellen
# Route in App.jsx registrieren
```

### Plugin entwickeln
```bash
cd backend/integrations/plugins
# Neue YAML-Datei erstellen
# Siehe README.md für Dokumentation
```

### API testen
```bash
# API Dokumentation öffnen
open http://localhost:8000/docs

# Oder mit curl
curl http://localhost:8000/health
```

## System-Anforderungen

### Backend-Server

**Minimum:**
- 4 CPU Cores
- 16 GB RAM
- 50 GB Speicher

**Empfohlen:**
- 8+ CPU Cores
- 32 GB RAM
- 100 GB+ SSD
- NVIDIA GPU (optional)

### Satellite (Raspberry Pi)

**Minimum:**
- Raspberry Pi Zero 2 W
- ReSpeaker 2-Mics Pi HAT
- 16 GB MicroSD
- 5V/2A Netzteil

## Sicherheit

- Alle Daten bleiben auf deinem Server
- Keine Telemetrie oder Tracking
- Optional HTTPS via Nginx
- Token-basierte Home Assistant Auth
- Satellite-Verbindungen über WebSocket

## Lizenz

MIT License - Siehe LICENSE Datei

---

**Viel Erfolg mit Renfield!**

Bei Fragen oder Problemen: Siehe README.md oder erstelle ein GitHub Issue.
