# Renfield - Persönlicher KI-Assistent

Ein vollständig offline-fähiger, selbst-gehosteter KI-Assistent.

## Features

### Kernfunktionen
- **Chat-Interface** - Text- und Sprachbasierte Kommunikation mit Streaming-Antworten
- **Konversations-Historie** - Sidebar mit Chatverläufen, Datumsgruppierung, Session-Persistenz
- **Spracheingabe & -ausgabe** - Whisper STT und Piper TTS
- **Sprechererkennung** - Automatische Identifikation mit SpeechBrain ECAPA-TDNN
- **Multi-Room Voice Control** - Raspberry Pi Satellite Sprachassistenten
- **Konversations-Persistenz** - Follow-up-Fragen verstehen ("Mach es aus", "Und dort?")
- **Agent Loop (ReAct)** - Mehrstufige Anfragen mit bedingter Logik und Tool-Verkettung

### Integrationen
- **MCP-Server** - Externe Tools via Model Context Protocol (Weather, Search, News, Jellyfin, n8n, Home Assistant, Paperless, Email)
- **Smart Home Steuerung** - Home Assistant Integration mit Raum-Kontext
- **Kamera-Überwachung** - Frigate Integration mit Objekterkennung
- **Workflow-Automation** - n8n Integration
- **Dynamisches Plugin-System** - YAML-basierte Integration externer APIs (Legacy)

### Wissensspeicher (RAG)
- **Dokument-Upload** - PDF, DOCX, PPTX, XLSX, HTML, Markdown
- **Intelligente Chunking** - Automatische Textaufteilung mit Docling
- **Vektor-Suche** - Semantische Suche mit pgvector
- **Duplikat-Erkennung** - SHA256-Hash verhindert doppelte Dokumente
- **Knowledge Bases** - Organisiere Wissen in thematischen Sammlungen

### Raum-Management
- **Geräte-Registrierung** - Statische und mobile Geräte pro Raum
- **IP-basierte Raumerkennung** - Automatischer Raum-Kontext für Befehle
- **Audio-Output-Routing** - TTS-Ausgabe auf optimales Gerät im Raum
- **Home Assistant Sync** - Automatischer Import von Räumen und Areas

### Plattform
- **Progressive Web App** - Funktioniert auf Desktop, Tablet und Smartphone
- **Dark Mode** - Automatische oder manuelle Umschaltung zwischen Hell/Dunkel/System
- **Vollständig Offline** - Keine Cloud-Abhängigkeiten
- **GPU-Beschleunigung** - Optional NVIDIA GPU für schnellere Transkription

## Architektur

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         RENFIELD ECOSYSTEM                                │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                   │
│  │  Satellite  │    │  Satellite  │    │  Web Panel  │                   │
│  │ Wohnzimmer  │    │   Küche     │    │   Tablet    │                   │
│  │ Pi Zero 2 W │    │ Pi Zero 2 W │    │  (Browser)  │                   │
│  │ + ReSpeaker │    │ + ReSpeaker │    │             │                   │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘                   │
│         │                  │                  │                          │
│         └────────┬─────────┴─────────┬────────┘                          │
│                  │    WebSocket      │                                   │
│                  │  /ws/satellite    │  /ws/device                       │
│                  ▼                   ▼                                   │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    Frontend (React PWA)                             │  │
│  │   - Web Interface mit Chat & Voice    - Raum-Verwaltung            │  │
│  │   - PWA für iOS/Android               - Wissensspeicher-UI         │  │
│  └─────────────────────────┬──────────────────────────────────────────┘  │
│                            │ WebSocket /ws                               │
│  ┌─────────────────────────▼──────────────────────────────────────────┐  │
│  │                      Backend (FastAPI)                              │  │
│  │  ┌────────────────┐  ┌──────────────┐  ┌────────────────────────┐  │  │
│  │  │SatelliteManager│  │ OllamaService│  │    ActionExecutor      │  │  │
│  │  │ DeviceManager  │  │  RAGService  │  │    PluginRegistry      │  │  │
│  │  └────────────────┘  └──────────────┘  └────────────────────────┘  │  │
│  │  ┌─────────┐ ┌──────────┐ ┌─────────────┐ ┌─────────────────────┐  │  │
│  │  │ Whisper │ │  Piper   │ │ RoomService │ │ OutputRoutingService│  │  │
│  │  │  (STT)  │ │  (TTS)   │ │             │ │                     │  │  │
│  │  └─────────┘ └──────────┘ └─────────────┘ └─────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────────────────┐  │  │
│  │  │                    PostgreSQL + pgvector                      │  │  │
│  │  │  Conversations │ Messages │ Documents │ Chunks │ Embeddings  │  │  │
│  │  │  Rooms │ Devices │ Speakers │ Knowledge Bases                 │  │  │
│  │  └──────────────────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                            │                                             │
│  ┌─────────────────────────▼──────────────────────────────────────────┐  │
│  │                    External Integrations                            │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │  │
│  │  │  Ollama  │ │   Home   │ │ Frigate  │ │   n8n    │ │ Plugins  │  │  │
│  │  │  (LLM)   │ │Assistant │ │  (NVR)   │ │(Workflow)│ │(Weather) │  │  │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```
## Haftungsausschluss

renfield ist ein unabhängiges Open-Source-Projekt.
Es besteht keine Verbindung zu Dritten, Organisationen, Unternehmen
oder Marken mit gleichem oder ähnlichem Namen, und es erfolgt keine
Unterstützung oder Billigung durch solche Dritte.

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
docker exec -it renfield-ollama ollama pull qwen3:8b
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
2. Die **Sidebar** zeigt alle bisherigen Konversationen gruppiert nach Datum
3. Klicke auf eine Konversation um sie zu laden, oder starte einen **neuen Chat**
4. Gib eine Textnachricht ein oder nutze das Mikrofon
5. Der Assistent versteht Befehle wie:
   - "Schalte das Licht im Wohnzimmer ein"
   - "Zeige mir die Kamera-Events von heute"
   - "Starte den n8n Workflow 'Backup'"
   - "Was ist die aktuelle Temperatur?"

**Sidebar-Funktionen:**
- **Datumsgruppierung** - Heute, Gestern, Letzte 7 Tage, Älter
- **Session-Persistenz** - Konversation wird nach Reload wiederhergestellt
- **Löschen** - Hover über Konversation und klicke das Papierkorb-Symbol
- **Mobile** - Sidebar über den Menu-Button unten links öffnen

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

## Integrationen (MCP + Plugins)

Renfield nutzt **MCP-Server (Model Context Protocol)** als bevorzugten Integrationsweg. Zusätzlich existiert ein Legacy-YAML-Plugin-System.

### MCP-Server (bevorzugt)

Externe Dienste werden als MCP-Server angebunden und stellen Tools für den Agent Loop bereit. Konfiguration in `config/mcp_servers.yaml`.

**Verfügbare MCP-Server:**
| Server | Beschreibung | Aktivierung |
|--------|-------------|-------------|
| Weather | OpenWeatherMap | `WEATHER_ENABLED=true` |
| Search | SearXNG Metasearch | `SEARCH_ENABLED=true` |
| News | NewsAPI | `NEWS_ENABLED=true` |
| Jellyfin | Media Server | `JELLYFIN_ENABLED=true` |
| n8n | Workflow Automation | `N8N_MCP_ENABLED=true` |
| Home Assistant | Smart Home | `HA_MCP_ENABLED=true` |
| Paperless | Dokumenten-Management | `PAPERLESS_ENABLED=true` |
| Email | IMAP/SMTP E-Mail | `EMAIL_MCP_ENABLED=true` |

**Aktivierung:**
```bash
# In .env:
MCP_ENABLED=true
WEATHER_ENABLED=true
SEARCH_ENABLED=true
# ... weitere Server nach Bedarf
```

API-Keys werden in Produktion als Docker Secrets bereitgestellt. Siehe `docs/SECRETS_MANAGEMENT.md`.

### YAML-Plugins (Legacy)

Für einfache REST-API-Integrationen ohne eigenen MCP-Server:

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

> **Hinweis:** YAML-Plugins nutzen `*_PLUGIN_ENABLED` (z.B. `WEATHER_PLUGIN_ENABLED=true`), um Konflikte mit MCP-Server `*_ENABLED` Variablen zu vermeiden.

### Plugin-Dokumentation

Vollständige Dokumentation, Beispiele und Troubleshooting:
[Plugin Development Guide](backend/integrations/plugins/README.md)

## Sprechererkennung

Renfield erkennt automatisch **wer spricht** und kann personalisierte Antworten geben.

### Features

- **Automatische Identifikation** bei jeder Spracheingabe (Web & Satellite)
- **Auto-Discovery** - Unbekannte Sprecher werden automatisch als Profile angelegt
- **Continuous Learning** - Verbesserte Erkennung durch jede Interaktion
- **Frontend-Verwaltung** - Sprecher unter `/speakers` verwalten

### Wie es funktioniert

1. **Erster Benutzer spricht** → "Unbekannter Sprecher #1" wird angelegt
2. **Gleicher Benutzer spricht erneut** → Wird als #1 erkannt
3. **Anderer Benutzer spricht** → "Unbekannter Sprecher #2" wird angelegt
4. **Admin benennt um** → "Unbekannter Sprecher #1" → "Max Mustermann"

### Konfiguration

```bash
# In .env
SPEAKER_RECOGNITION_ENABLED=true      # Aktivieren/Deaktivieren
SPEAKER_RECOGNITION_THRESHOLD=0.25    # Erkennungs-Schwellwert (0-1)
SPEAKER_AUTO_ENROLL=true              # Auto-Discovery aktivieren
SPEAKER_CONTINUOUS_LEARNING=true      # Lernen bei jeder Interaktion
```

### Logs

```
🎤 Speaker identified: Max Mustermann (0.85)
🆕 New unknown speaker created: Unbekannter Sprecher #2 (ID: 4)
```

**Vollständige Dokumentation:** [SPEAKER_RECOGNITION.md](SPEAKER_RECOGNITION.md)

## Wissensspeicher (RAG)

Renfield kann Dokumente verarbeiten und als Wissensbasis für kontextbasierte Antworten nutzen.

### Unterstützte Formate

- PDF, DOCX, PPTX, XLSX
- HTML, Markdown, TXT

### Wie es funktioniert

1. **Dokument hochladen** → Automatische Verarbeitung mit IBM Docling
2. **Chunking** → Text wird in semantische Abschnitte aufgeteilt
3. **Embedding** → Jeder Chunk wird mit dem konfigurierten Embedding-Modell vektorisiert
4. **Hybrid Search** → Dense Embeddings (pgvector) + BM25 Full-Text Search (PostgreSQL tsvector), kombiniert via Reciprocal Rank Fusion (RRF)
5. **Context Window** → Benachbarte Chunks werden automatisch zum Treffer hinzugefügt

### Features

- **Hybrid Search** - Dense + BM25 für semantische UND keyword-basierte Suche
- **Context Window** - Erweitert Treffer-Chunks um benachbarte Abschnitte
- **Knowledge Bases** - Organisiere Dokumente in thematischen Sammlungen
- **Duplikat-Erkennung** - SHA256-Hash verhindert doppelte Uploads
- **Follow-up-Fragen** - RAG-Kontext bleibt für Nachfragen erhalten
- **Quellen-Zitation** - Antworten verweisen auf Quelldokumente

### Verwendung

1. Navigiere zu **Wissensspeicher** im Menü
2. Erstelle eine Knowledge Base (z.B. "Handbücher")
3. Lade Dokumente hoch
4. Aktiviere RAG im Chat mit dem Toggle
5. Stelle Fragen zu deinen Dokumenten

### Konfiguration

```bash
# In .env
RAG_ENABLED=true
RAG_CHUNK_SIZE=512
RAG_CHUNK_OVERLAP=50
RAG_TOP_K=5
RAG_SIMILARITY_THRESHOLD=0.4

# Hybrid Search (Dense + BM25 via RRF)
RAG_HYBRID_ENABLED=true
RAG_HYBRID_BM25_WEIGHT=0.3
RAG_HYBRID_DENSE_WEIGHT=0.7
RAG_HYBRID_FTS_CONFIG=simple       # simple/german/english

# Context Window
RAG_CONTEXT_WINDOW=1               # 0=deaktiviert
```

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

### Tests ausführen

Das Projekt verfügt über eine umfassende Test-Suite mit über 1.300 Backend-Tests:

```bash
# Alle Tests ausführen
make test

# Nur Backend-Tests
make test-backend

# Nur Frontend-Tests (React + Vitest)
make test-frontend

# Tests mit Coverage-Report
make test-coverage

# Direkt mit Docker
docker compose exec -T -e PYTHONPATH=/app backend pytest /tests/backend/ -v
```

**Testabdeckung:**
- API-Routen: Chat, Voice, Speakers, Users, HomeAssistant, Camera, Tasks, Settings
- Services: OllamaService, RAGService, SpeakerService, DeviceManager, RoomService
- Agent Loop: ComplexityDetector, AgentToolRegistry, AgentService (85 Tests)
- Auth & RBAC: JWT-Tokens, Passwort-Hashing, Berechtigungen, Rollen
- WebSocket: Protokoll-Parsing, Geräte-Registrierung, Rate-Limiting

Siehe [CLAUDE.md](CLAUDE.md#testing) für detaillierte Test-Dokumentation.

## Mobile App (iOS)

Das Frontend ist eine Progressive Web App (PWA):

1. Öffne http://your-server-ip:3000 in Safari
2. Tippe auf das Teilen-Symbol
3. Wähle "Zum Home-Bildschirm"
4. Die App verhält sich wie eine native App

## Fehlerbehebung

### Ollama lädt nicht

```bash
docker exec -it renfield-ollama ollama pull qwen3:8b
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

### Authentifizierung
- `POST /api/auth/login` - Login (Username/Passwort → JWT Tokens)
- `POST /api/auth/register` - Neuen Benutzer registrieren
- `POST /api/auth/refresh` - Access Token erneuern
- `POST /api/auth/voice` - Login per Stimmerkennung
- `GET /api/auth/me` - Aktueller Benutzer + Berechtigungen
- `GET /api/auth/status` - Auth-Status (enabled, user info)
- `POST /api/auth/change-password` - Passwort ändern

### Benutzer & Rollen (Admin)
- `GET /api/users` - Alle Benutzer auflisten
- `POST /api/users` - Benutzer erstellen
- `PATCH /api/users/{id}` - Benutzer bearbeiten
- `DELETE /api/users/{id}` - Benutzer löschen
- `POST /api/users/{id}/link-speaker` - Sprecher verknüpfen
- `GET /api/roles` - Alle Rollen auflisten
- `POST /api/roles` - Rolle erstellen
- `PATCH /api/roles/{id}` - Rolle bearbeiten
- `DELETE /api/roles/{id}` - Rolle löschen

### Chat & Konversationen
- `POST /api/chat/send` - Nachricht senden
- `GET /api/chat/history/{session_id}` - Historie abrufen
- `GET /api/chat/conversations` - Alle Konversationen auflisten
- `GET /api/chat/conversation/{session_id}/summary` - Zusammenfassung
- `GET /api/chat/search?q=...` - In Konversationen suchen
- `GET /api/chat/stats` - Statistiken
- `DELETE /api/chat/session/{session_id}` - Session löschen
- `WS /ws` - WebSocket für Streaming (mit session_id für Persistenz)

### Voice
- `POST /api/voice/stt` - Speech-to-Text
- `POST /api/voice/tts` - Text-to-Speech
- `POST /api/voice/voice-chat` - Kompletter Voice-Flow

### Satellite & Devices
- `WS /ws/satellite` - WebSocket für Satellite-Verbindungen
- `WS /ws/device` - WebSocket für Web-Panels und Tablets

### Knowledge Base (RAG)
- `POST /api/knowledge/upload` - Dokument hochladen
- `GET /api/knowledge/documents` - Dokumente auflisten
- `DELETE /api/knowledge/documents/{id}` - Dokument löschen
- `POST /api/knowledge/search` - Hybrid Search (Dense + BM25)
- `POST /api/knowledge/reindex-fts` - Full-Text-Search Vektoren neu aufbauen (Admin)
- `GET /api/knowledge/bases` - Knowledge Bases auflisten (gefiltert nach Zugriff)
- `POST /api/knowledge/bases` - Knowledge Base erstellen
- `DELETE /api/knowledge/bases/{id}` - Knowledge Base löschen
- `GET /api/knowledge/stats` - RAG-Statistiken
- `POST /api/knowledge/bases/{id}/share` - KB mit Benutzer teilen
- `GET /api/knowledge/bases/{id}/permissions` - KB-Berechtigungen auflisten
- `DELETE /api/knowledge/bases/{id}/permissions/{perm_id}` - Zugriff entziehen
- `PATCH /api/knowledge/bases/{id}/public` - KB öffentlich/privat setzen

### Rooms
- `GET /api/rooms` - Räume mit Geräten auflisten
- `POST /api/rooms` - Raum erstellen
- `GET /api/rooms/{id}/devices` - Geräte im Raum
- `POST /api/rooms/{id}/devices` - Gerät registrieren
- `GET /api/rooms/{id}/output-devices` - Audio-Output-Geräte
- `POST /api/rooms/sync-homeassistant` - Räume aus HA importieren

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

### Speakers
- `GET /api/speakers` - Alle Sprecher auflisten
- `POST /api/speakers` - Neuen Sprecher anlegen
- `POST /api/speakers/{id}/enroll` - Voice Sample hinzufügen
- `POST /api/speakers/identify` - Sprecher identifizieren
- `DELETE /api/speakers/{id}` - Sprecher löschen

## Zugriffskontrolle (RPBAC)

Renfield bietet ein flexibles **Role-Permission Based Access Control (RPBAC)** System zum Schutz von Ressourcen.

### Features

- **JWT-basierte Authentifizierung** - Access + Refresh Tokens
- **Flexible Rollen** - Erstelle eigene Rollen mit beliebigen Berechtigungen
- **Granulare Permissions** - 22+ Berechtigungen für verschiedene Ressourcen
- **Resource Ownership** - Wissensdatenbanken und Konversationen gehören Benutzern
- **KB-Sharing** - Teile Wissensdatenbanken mit anderen Nutzern
- **Voice Authentication** - Login per Stimmerkennung (optional)
- **Optional** - Standardmäßig deaktiviert für einfache Entwicklung

### Standard-Rollen

| Rolle | Berechtigungen | Verwendung |
|-------|---------------|------------|
| Admin | Vollzugriff | Systemadministratoren |
| Familie | Smart Home voll, eigene+geteilte KBs, Kameras ansehen | Familienmitglieder |
| Gast | Nur lesen, keine KBs, keine Kameras | Gäste, eingeschränkter Zugriff |

### Berechtigungs-Hierarchie

```
Knowledge Bases: kb.all > kb.shared > kb.own > kb.none
Smart Home:      ha.full > ha.control > ha.read > ha.none
Kameras:         cam.full > cam.view > cam.none
```

### Aktivierung

```bash
# In .env
AUTH_ENABLED=true
SECRET_KEY=dein-starker-zufalls-key

# Optional: Voice Authentication
VOICE_AUTH_ENABLED=true
VOICE_AUTH_MIN_CONFIDENCE=0.7
```

### Beispiel-Szenario

```
Benutzer "Erik" (Admin)
├── Sieht alle Wissensdatenbanken
├── Volle Smart Home Kontrolle
├── Kamera-Snapshots
└── Benutzer verwalten

Benutzer "Partner" (Familie)
├── Eigene + geteilte Wissensdatenbanken
├── Volle Smart Home Kontrolle
└── Kamera-Events ansehen

Benutzer "Handwerker" (Custom-Rolle "Techniker")
├── Keine Wissensdatenbanken
├── Volle Smart Home Kontrolle
└── Keine Kameras
```

**Vollständige Dokumentation:** [ACCESS_CONTROL.md](ACCESS_CONTROL.md)

## Sicherheit

- Alle Daten bleiben lokal auf deinem Server
- Keine Cloud-Verbindungen für Kernfunktionen
- Home Assistant Token wird sicher gespeichert
- HTTPS kann über Nginx Reverse Proxy aktiviert werden
- **JWT-Authentifizierung** für API-Zugriff (optional aktivierbar)
- **Passwort-Hashing** mit bcrypt
- **Rate Limiting** für WebSocket-Verbindungen

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
- [SpeechBrain](https://speechbrain.github.io/) - Speaker Recognition (ECAPA-TDNN)
- [IBM Docling](https://github.com/DS4SD/docling) - Document Processing für RAG
- [pgvector](https://github.com/pgvector/pgvector) - Vector Similarity Search
- [Home Assistant](https://www.home-assistant.io/) - Smart Home Platform
- [Frigate](https://frigate.video/) - NVR mit Objekterkennung
- [n8n](https://n8n.io/) - Workflow Automation
- [OpenWakeWord](https://github.com/dscripka/openWakeWord) - Wake Word Detection

## Support

Bei Fragen oder Problemen erstelle bitte ein Issue im Repository.

---

**Hinweis**: Dieses Projekt ist für den privaten Gebrauch konzipiert. Stelle sicher, dass du die Datenschutzrichtlinien deines Landes beachtest, insbesondere bei der Kamera-Überwachung.
