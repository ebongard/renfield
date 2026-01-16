# Renfield - Projekt-Übersicht

## 📁 Projektstruktur

```
renfield/
├── 📄 README.md                    # Hauptdokumentation
├── 📄 INSTALLATION.md              # Detaillierte Setup-Anleitung
├── 📄 FEATURES.md                  # Feature-Dokumentation
├── 📄 docker-compose.yml           # Docker Services
├── 📄 .env.example                 # Umgebungsvariablen Template
├── 📄 .gitignore                   # Git Ignore
├── 🚀 start.sh                     # Startup Script
├── 🔄 update.sh                    # Update Script
│
├── 📂 backend/                     # Python Backend
│   ├── 📄 Dockerfile
│   ├── 📄 requirements.txt
│   ├── 📄 main.py                  # FastAPI App
│   ├── 📂 api/
│   │   └── 📂 routes/              # API Endpoints
│   │       ├── chat.py
│   │       ├── voice.py
│   │       ├── tasks.py
│   │       ├── camera.py
│   │       └── homeassistant.py
│   ├── 📂 services/                # Business Logic
│   │   ├── database.py
│   │   ├── ollama_service.py
│   │   ├── whisper_service.py
│   │   ├── piper_service.py
│   │   └── task_queue.py
│   ├── 📂 integrations/            # Externe Integrationen
│   │   ├── homeassistant.py
│   │   ├── n8n.py
│   │   └── frigate.py
│   ├── 📂 models/                  # Datenbank Models
│   │   └── database.py
│   └── 📂 utils/                   # Hilfsfunktionen
│       └── config.py
│
├── 📂 frontend/                    # React Frontend
│   ├── 📄 Dockerfile
│   ├── 📄 package.json
│   ├── 📄 vite.config.js
│   ├── 📄 tailwind.config.js
│   ├── 📄 index.html
│   └── 📂 src/
│       ├── 📄 main.jsx
│       ├── 📄 App.jsx
│       ├── 📄 index.css
│       ├── 📂 components/
│       │   └── Layout.jsx
│       ├── 📂 pages/
│       │   ├── HomePage.jsx
│       │   ├── ChatPage.jsx        # Chat mit Voice
│       │   ├── TasksPage.jsx
│       │   ├── CameraPage.jsx
│       │   └── HomeAssistantPage.jsx
│       └── 📂 utils/
│           └── axios.js
│
└── 📂 config/                      # Konfigurationsdateien
    └── nginx.conf                  # Nginx Reverse Proxy
```

## 🎯 Hauptkomponenten

### Backend (Python/FastAPI)
- **REST API** für alle Funktionen
- **WebSocket** für Echtzeit-Chat
- **Ollama Integration** für lokales LLM
- **Whisper** für Speech-to-Text
- **Piper** für Text-to-Speech
- **PostgreSQL** als Datenbank
- **Redis** als Message Queue

### Frontend (React)
- **Single Page Application** mit React Router
- **Tailwind CSS** für Styling
- **Progressive Web App** (PWA)
- **WebSocket** für Live-Updates
- **Responsive Design** für alle Geräte

### Integrationen
- **Home Assistant** - Smart Home Steuerung
- **Frigate** - Kamera NVR mit KI-Objekterkennung
- **n8n** - Workflow Automation

## 🚀 Quick Start

```bash
# 1. .env konfigurieren
cp .env.example .env
nano .env

# 2. System starten
./start.sh

# 3. Im Browser öffnen
# http://localhost:3000
```

## 📚 Wichtige Dateien

### Für Entwickler
- `backend/main.py` - FastAPI Application Entry Point
- `frontend/src/App.jsx` - React Application Entry Point
- `docker-compose.yml` - Service Orchestrierung

### Für Betreiber
- `.env` - Umgebungsvariablen und Konfiguration
- `start.sh` - System starten
- `update.sh` - System aktualisieren
- `INSTALLATION.md` - Setup-Anleitung

### Für Anwender
- `README.md` - Übersicht und Schnellstart
- `FEATURES.md` - Alle Features im Detail

## 🔑 Key Features

1. **Vollständig Offline** - Keine Cloud-Abhängigkeiten
2. **Voice Interface** - Sprechen und Hören
3. **Smart Home Control** - Home Assistant Integration
4. **Camera Monitoring** - Frigate Integration
5. **Task Automation** - n8n Workflows
6. **Mobile Ready** - PWA für iOS/Android

## 🛠️ Technologie-Stack

### Backend
- Python 3.11
- FastAPI (Web Framework)
- SQLAlchemy (ORM)
- Ollama (LLM)
- Whisper (STT)
- Piper (TTS)

### Frontend
- React 18
- Vite (Build Tool)
- Tailwind CSS
- Axios (HTTP Client)
- Lucide Icons

### Infrastructure
- Docker & Docker Compose
- PostgreSQL 16
- Redis 7
- Nginx (optional)

## 📞 Nächste Schritte

1. **Installation**: Folge `INSTALLATION.md`
2. **Konfiguration**: Passe `.env` an deine Bedürfnisse an
3. **Start**: Führe `./start.sh` aus
4. **Test**: Öffne http://localhost:3000 und teste die Features
5. **Anpassung**: Erweitere das System nach deinen Wünschen

## 🤝 Entwicklung

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

### API testen
```bash
# API Dokumentation öffnen
open http://localhost:8000/docs

# Oder mit curl
curl http://localhost:8000/health
```

## 📊 System-Anforderungen

**Minimum:**
- 4 CPU Cores
- 16 GB RAM
- 50 GB Speicher

**Empfohlen:**
- 8+ CPU Cores
- 32 GB RAM
- 100 GB+ SSD
- NVIDIA GPU (optional)

## 🔒 Sicherheit

- Alle Daten bleiben auf deinem Server
- Keine Telemetrie oder Tracking
- Optional HTTPS via Nginx
- Token-basierte Home Assistant Auth

## 📄 Lizenz

MIT License - Siehe LICENSE Datei

---

**Viel Erfolg mit Renfield!** 🎉

Bei Fragen oder Problemen: Siehe README.md oder erstelle ein GitHub Issue.
