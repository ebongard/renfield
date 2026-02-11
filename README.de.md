[English](README.md) | **Deutsch**

# Renfield - Persönlicher KI-Assistent

Ein vollständig offline-fähiger, selbst-gehosteter **digitaler Assistent** — ein persönlicher AI Hub, der Wissen, Informationsabfragen und Multi-Channel-Steuerung in einer Oberfläche bündelt. Renfield dient mehreren Nutzern parallel im Haushalt, mit abfragbarer Wissensbasis (RAG), gebündeltem Tool-Zugriff über MCP-Server und Smart-Home-Steuerung.

**Tech Stack:** Python 3.11 · FastAPI · React 18 · TypeScript · PostgreSQL 16 · Redis 7 · Ollama · Docker Compose

## Haftungsausschluss

renfield ist ein unabhängiges Open-Source-Projekt.
Es besteht keine Verbindung zu Dritten, Organisationen, Unternehmen
oder Marken mit gleichem oder ähnlichem Namen, und es erfolgt keine
Unterstützung oder Billigung durch solche Dritte.

## Features

### Kernfunktionen
- **Chat-Interface** — Text- und sprachbasierte Kommunikation mit Streaming-Antworten
- **Konversations-Historie** — Sidebar mit Chatverläufen, Datumsgruppierung, Session-Persistenz, Follow-up-Fragen
- **Agent System (ReAct)** — Mehrstufige Anfragen mit Tool-Verkettung, Agent Router mit 8 spezialisierten Rollen
- **Konversations-Gedächtnis** — Langzeit-Erinnerungen (Präferenzen, Fakten, Anweisungen) mit Widerspruchserkennung
- **Intent Feedback Learning** — Lernt aus Korrekturen und verbessert Intent-Erkennung über semantisches Matching
- **Spracheingabe & -ausgabe** — Whisper STT und Piper TTS, Sprechererkennung mit SpeechBrain

### Integrationen (8 MCP-Server)
| Server | Beschreibung | Transport | Aktivierung |
|--------|-------------|-----------|-------------|
| Weather | OpenWeatherMap | stdio | `WEATHER_ENABLED=true` |
| Search | SearXNG Metasearch | stdio | `SEARCH_ENABLED=true` |
| News | NewsAPI | stdio | `NEWS_ENABLED=true` |
| Jellyfin | Media Server | stdio | `JELLYFIN_ENABLED=true` |
| n8n | Workflow Automation | stdio | `N8N_MCP_ENABLED=true` |
| Home Assistant | Smart Home | streamable_http | `HA_MCP_ENABLED=true` |
| Paperless | Dokumenten-Management | stdio | `PAPERLESS_ENABLED=true` |
| Email | IMAP/SMTP E-Mail | stdio | `EMAIL_MCP_ENABLED=true` |

### Wissensspeicher (RAG)
- **Hybrid Search** — Dense Embeddings (pgvector) + BM25 Full-Text-Search, kombiniert via RRF
- **Unterstützte Formate** — PDF, DOCX, PPTX, XLSX, HTML, Markdown, TXT
- **Knowledge Bases** — Thematische Sammlungen mit Sharing und Zugriffssteuerung
- **Context Window** — Benachbarte Chunks werden automatisch zum Treffer hinzugefügt

### Proaktive Benachrichtigungen & Erinnerungen
- **Webhook-basiert** — Home Assistant / n8n → Renfield (Bearer Token Auth)
- **Semantische Deduplizierung** — Verhindert doppelte Benachrichtigungen via pgvector
- **Erinnerungen** — Zeitgesteuerte Reminder mit natürlichsprachlicher Eingabe

### Multi-Room Device System
- **Raspberry Pi Satellites** — Pi Zero 2 W + ReSpeaker 2-Mics HAT (~63€ pro Einheit)
- **Wake-Word-Erkennung** — Lokales OpenWakeWord, zentrale Verwaltung
- **Audio-Output-Routing** — TTS-Ausgabe auf optimales Gerät im Raum
- **IP-basierte Raumerkennung** — Automatischer Raum-Kontext für Befehle

### Sicherheit & Zugriffskontrolle
- **RPBAC** — Role-Permission Based Access Control mit JWT (optional)
- **Rate Limiting** — WebSocket + REST API, konfigurierbare Limits
- **Circuit Breaker** — Automatische Ausfallsicherung für LLM und Agent
- **Secrets Management** — Docker Secrets in Produktion (`/run/secrets/`)
- **Trusted Proxies** — CIDR-basierte Proxy-Konfiguration

### Plattform
- **Progressive Web App** — Desktop, Tablet, Smartphone
- **Dark Mode** — Hell, Dunkel, System (folgt OS-Präferenz)
- **Mehrsprachigkeit** — Deutsch und Englisch (react-i18next)
- **Vollständig Offline** — Keine Cloud-Abhängigkeiten
- **Prometheus Metrics** — Opt-in `/metrics` Endpoint

## Architektur

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                             RENFIELD ECOSYSTEM                               │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                      │
│  │  Satellite  │    │  Satellite  │    │  Web Panel  │                      │
│  │ Wohnzimmer  │    │   Küche     │    │   Tablet    │                      │
│  │ Pi Zero 2 W │    │ Pi Zero 2 W │    │  (Browser)  │                      │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘                      │
│         │                  │                  │                              │
│         └────────┬─────────┴─────────┬────────┘                             │
│                  │   WebSocket       │                                       │
│                  ▼                   ▼                                       │
│  ┌───────────────────────────────────────────────────────────────────────┐   │
│  │                     Frontend (React PWA)                              │   │
│  │   Chat · Voice · Knowledge · Rooms · Cameras · Admin · Notifications │   │
│  └──────────────────────────┬────────────────────────────────────────────┘   │
│                             │ WebSocket + REST                               │
│  ┌──────────────────────────▼────────────────────────────────────────────┐   │
│  │                       Backend (FastAPI)                                │   │
│  │                                                                       │   │
│  │  ┌────────────────┐  ┌────────────────┐  ┌────────────────────────┐   │   │
│  │  │ Intent Recog.  │  │  Agent Router  │  │   Action Executor      │   │   │
│  │  │ (Ranked, FB)   │  │  (8 Roles)     │  │   (MCP + Plugins)      │   │   │
│  │  └────────────────┘  └────────────────┘  └────────────────────────┘   │   │
│  │  ┌────────────────┐  ┌────────────────┐  ┌────────────────────────┐   │   │
│  │  │ Memory Service │  │ Notification   │  │   RAG Service          │   │   │
│  │  │ (Long-term)    │  │ Service        │  │   (Hybrid Search)      │   │   │
│  │  └────────────────┘  └────────────────┘  └────────────────────────┘   │   │
│  │  ┌────────┐ ┌────────┐ ┌─────────────┐ ┌──────────┐ ┌────────────┐   │   │
│  │  │Whisper │ │ Piper  │ │RoomService  │ │ Speaker  │ │  Circuit   │   │   │
│  │  │ (STT)  │ │ (TTS)  │ │OutputRouting│ │ Recog.   │ │  Breaker   │   │   │
│  │  └────────┘ └────────┘ └─────────────┘ └──────────┘ └────────────┘   │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│              │                    │                    │                      │
│  ┌───────────▼────────────────────▼────────────────────▼─────────────────┐   │
│  │                   PostgreSQL + pgvector + Redis                        │   │
│  │  Conversations · Memories · Documents · Embeddings · Notifications    │   │
│  │  Rooms · Devices · Speakers · Feedback · Suppressions · Reminders     │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│              │                                                               │
│  ┌───────────▼───────────────────────────────────────────────────────────┐   │
│  │                      MCP-Server & Externe Dienste                     │   │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐  │   │
│  │  │ Ollama │ │Home    │ │Frigate │ │  n8n   │ │Weather │ │Search  │  │   │
│  │  │ (LLM)  │ │Assist. │ │ (NVR)  │ │  MCP   │ │  MCP   │ │  MCP   │  │   │
│  │  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘ └────────┘  │   │
│  │  ┌────────┐ ┌────────┐ ┌────────┐                                    │   │
│  │  │ News   │ │Jellyfin│ │Paper-  │                                    │   │
│  │  │  MCP   │ │  MCP   │ │less/   │                                    │   │
│  │  │        │ │        │ │Email   │                                    │   │
│  │  └────────┘ └────────┘ └────────┘                                    │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
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

Wichtige Einstellungen:
```env
# Ollama (lokal oder extern)
OLLAMA_URL=http://ollama:11434
OLLAMA_CHAT_MODEL=qwen3:14b

# Home Assistant (optional)
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=dein_long_lived_access_token

# MCP aktivieren
MCP_ENABLED=true
```

3. **System starten**
```bash
# Entwicklung auf Mac
docker compose -f docker-compose.dev.yml up -d

# Produktion mit NVIDIA GPU
docker compose -f docker-compose.prod.yml up -d

# Produktion ohne GPU
docker compose -f docker-compose.prod-cpu.yml up -d

# Standard
docker compose up -d
```

4. **Ollama Modell laden**
```bash
docker exec -it renfield-ollama ollama pull qwen3:8b
```

> **Tipp:** Du kannst auch eine externe Ollama-Instanz (z.B. auf einem GPU-Server) nutzen.
> Setze `OLLAMA_URL=http://cuda.local:11434` in der `.env` Datei.
> Siehe [docs/EXTERNAL_OLLAMA.md](docs/EXTERNAL_OLLAMA.md) für Details.

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
| `docker-compose.prod-cpu.yml` | Produktion | Nein | Produktion ohne GPU |

## Konfiguration

Alle Einstellungen via `.env`, geladen durch Pydantic Settings (`src/backend/utils/config.py`). Vollständige Referenz: [docs/ENVIRONMENT_VARIABLES.md](docs/ENVIRONMENT_VARIABLES.md)

### LLM (Multi-Modell)

Renfield unterstützt separate Modelle für verschiedene Aufgaben:

```env
OLLAMA_URL=http://ollama:11434
OLLAMA_CHAT_MODEL=qwen3:14b      # Chat-Antworten
OLLAMA_INTENT_MODEL=qwen3:8b     # Intent-Erkennung
OLLAMA_RAG_MODEL=qwen3:14b       # RAG-Antworten
OLLAMA_EMBED_MODEL=nomic-embed-text  # Embeddings (768 Dim.)
AGENT_MODEL=                      # Optional: Agent-Modell
AGENT_OLLAMA_URL=                 # Optional: Separate Ollama-Instanz
```

Siehe [docs/LLM_MODEL_GUIDE.md](docs/LLM_MODEL_GUIDE.md) für Modell-Empfehlungen.

### Integrationen (MCP)

```env
MCP_ENABLED=true                  # Master-Switch
WEATHER_ENABLED=true              # + OPENWEATHER_API_KEY
SEARCH_ENABLED=true               # + SEARXNG_API_URL
NEWS_ENABLED=true                 # + NEWSAPI_KEY
JELLYFIN_ENABLED=true             # + JELLYFIN_URL, JELLYFIN_API_KEY
N8N_MCP_ENABLED=true              # + N8N_BASE_URL, N8N_API_KEY
HA_MCP_ENABLED=true               # + HOME_ASSISTANT_URL, HOME_ASSISTANT_TOKEN
PAPERLESS_ENABLED=true            # + PAPERLESS_API_URL, PAPERLESS_API_TOKEN
EMAIL_MCP_ENABLED=true            # + config/mail_accounts.yaml
```

API-Keys in Produktion als Docker Secrets bereitstellen. Siehe [docs/SECRETS_MANAGEMENT.md](docs/SECRETS_MANAGEMENT.md).

### Weitere Schlüssel-Einstellungen

```env
# Agent System
AGENT_ENABLED=false               # ReAct Agent Loop (opt-in)
AGENT_MAX_STEPS=12                # Max Reasoning-Schritte

# Konversations-Gedächtnis
MEMORY_ENABLED=false              # Langzeit-Erinnerungen (opt-in)
MEMORY_CONTRADICTION_RESOLUTION=false  # Widerspruchserkennung (opt-in)

# Proaktive Benachrichtigungen
PROACTIVE_ENABLED=false           # Webhook-Benachrichtigungen (opt-in)
PROACTIVE_REMINDERS_ENABLED=false # Erinnerungen (opt-in)

# Authentifizierung
AUTH_ENABLED=false                # RPBAC (opt-in)

# Monitoring
METRICS_ENABLED=false             # Prometheus /metrics (opt-in)
```

## Entwicklung

### Befehle

```bash
make lint                    # Lint all code (ruff + eslint)
make format-backend          # Format + auto-fix mit ruff
make test                    # Alle Tests
make test-backend            # Backend-Tests (1.700+)
make test-frontend-react     # React-Tests (Vitest + RTL)
make test-coverage           # Tests mit Coverage-Report (fail-under=50%)
```

### Datenbank-Migrationen

```bash
docker exec -it renfield-backend alembic revision --autogenerate -m "description"
docker exec -it renfield-backend alembic upgrade head
```

### Debug & Admin

```bash
# Intent-Erkennung testen
curl -X POST "http://localhost:8000/debug/intent?message=Schalte das Licht ein"

# Home Assistant Keywords neu laden
curl -X POST "http://localhost:8000/admin/refresh-keywords"

# Alle Embeddings neu berechnen (nach Modell-Wechsel)
curl -X POST "http://localhost:8000/admin/reembed"
```

## API-Übersicht

20 Route-Module mit 100+ Endpunkten. Interaktive Dokumentation unter `/docs`.

| Bereich | Prefix | Beschreibung |
|---------|--------|--------------|
| Auth | `/api/auth` | Login, Register, JWT Refresh, Voice Auth |
| Chat | `/api/chat` | Nachrichten, Historie, Suche, Statistiken |
| Voice | `/api/voice` | STT (Whisper), TTS (Piper) |
| Knowledge | `/api/knowledge` | RAG: Upload, Search, Knowledge Bases, Sharing |
| Memory | `/api/memory` | Langzeit-Erinnerungen: CRUD, History, Audit Trail |
| Notifications | `/api/notifications` | Webhook, Acknowledge, Suppressions, Reminders |
| Feedback | `/api/feedback` | Intent-Korrekturen und Feedback Learning |
| MCP | `/api/mcp` | Server-Status, Tools, Refresh |
| Intents | `/api/intents` | Verfügbare Intents und Integrationen |
| Home Assistant | `/api/homeassistant` | Entity-Steuerung (via direkter API) |
| Camera | `/api/camera` | Frigate Events, Snapshots |
| Rooms | `/api/rooms` | Raum-CRUD, Geräte, HA-Sync |
| Speakers | `/api/speakers` | Sprechererkennung, Enrollment |
| Satellites | `/api/satellites` | Monitoring, Metriken, Logs |
| Settings | `/api/settings` | Wake-Word, Modell-Konfiguration |
| Preferences | `/api/preferences` | Sprach-Einstellungen |
| Users | `/api/users` | Benutzer-Verwaltung |
| Roles | `/api/roles` | Rollen-Verwaltung (RPBAC) |
| Tasks | `/api/tasks` | Task-Queue |
| Plugins | `/api/plugins` | Plugin-Verwaltung (Legacy) |
| WebSocket | `/ws`, `/ws/device`, `/ws/satellite` | Chat, Geräte, Satellites |

## Multi-Room Satellite System

Raspberry Pi Zero 2 W + ReSpeaker 2-Mics Pi HAT als Sprachassistenten in jedem Raum.

### Hardware pro Satellite (~63€)

| Komponente | Preis |
|------------|-------|
| Raspberry Pi Zero 2 W | ~18€ |
| ReSpeaker 2-Mics Pi HAT V2.0 | ~12€ |
| MicroSD Card 16GB | ~8€ |
| 5V/2A Netzteil | ~10€ |
| 3.5mm Lautsprecher | ~10€ |
| Gehäuse (optional) | ~5€ |

### Schnellstart

```bash
cd /opt/renfield-satellite
source venv/bin/activate
python -m renfield_satellite config/satellite.yaml
```

**Vollständige Anleitung:** [renfield-satellite/README.md](renfield-satellite/README.md)

## Fehlerbehebung

### Ollama lädt nicht
```bash
docker exec -it renfield-ollama ollama pull qwen3:8b
docker compose restart backend
```

### WebSocket Verbindung fehlgeschlagen
```bash
# CORS und Backend-Container prüfen
docker logs renfield-backend
```

### Satellite findet Backend nicht
```bash
# Zeroconf-Advertisement prüfen
docker compose logs backend | grep zeroconf

# Manuelle URL in satellite config setzen
# config/satellite.yaml:
server:
  auto_discover: false
  url: "ws://renfield.local:8000/ws/satellite"
```

### Intent-Erkennung ungenau
```bash
# HA-Keywords aktualisieren
curl -X POST "http://localhost:8000/admin/refresh-keywords"

# Intent direkt testen
curl -X POST "http://localhost:8000/debug/intent?message=DEINE_NACHRICHT"
```

### Embeddings nach Modellwechsel neu berechnen
```bash
curl -X POST "http://localhost:8000/admin/reembed"
# Re-embedded: RAG Chunks, Memories, Feedback Corrections, Notification Suppressions
```

## Weitere Dokumentation

| Dokument | Inhalt |
|----------|--------|
| [docs/FEATURES.md](docs/FEATURES.md) | Ausführliche Feature-Dokumentation |
| [docs/ENVIRONMENT_VARIABLES.md](docs/ENVIRONMENT_VARIABLES.md) | Vollständige Konfigurationsreferenz |
| [docs/LLM_MODEL_GUIDE.md](docs/LLM_MODEL_GUIDE.md) | Modell-Empfehlungen und Konfiguration |
| [docs/SECRETS_MANAGEMENT.md](docs/SECRETS_MANAGEMENT.md) | Docker Secrets für Produktion |
| [docs/ACCESS_CONTROL.md](docs/ACCESS_CONTROL.md) | RPBAC-Zugriffskontrolle |
| [docs/SPEAKER_RECOGNITION.md](docs/SPEAKER_RECOGNITION.md) | Sprechererkennung |
| [docs/OUTPUT_ROUTING.md](docs/OUTPUT_ROUTING.md) | Audio-Output-Routing |
| [docs/MULTILANGUAGE.md](docs/MULTILANGUAGE.md) | Mehrsprachigkeit |
| [CLAUDE.md](CLAUDE.md) | Entwickler-Referenz (Architektur, Patterns, Commands) |

## Danksagungen

- [Ollama](https://ollama.ai/) — Lokales LLM
- [Whisper](https://github.com/openai/whisper) — Speech-to-Text
- [Piper](https://github.com/rhasspy/piper) — Text-to-Speech
- [SpeechBrain](https://speechbrain.github.io/) — Speaker Recognition (ECAPA-TDNN)
- [IBM Docling](https://github.com/DS4SD/docling) — Document Processing für RAG
- [pgvector](https://github.com/pgvector/pgvector) — Vector Similarity Search
- [Home Assistant](https://www.home-assistant.io/) — Smart Home Platform
- [Frigate](https://frigate.video/) — NVR mit Objekterkennung
- [n8n](https://n8n.io/) — Workflow Automation
- [SearXNG](https://docs.searxng.org/) — Metasearch Engine
- [OpenWakeWord](https://github.com/dscripka/openWakeWord) — Wake Word Detection

## Lizenz

MIT License — siehe LICENSE Datei

## Support

Bei Fragen oder Problemen erstelle bitte ein Issue im Repository.

---

**Hinweis**: Dieses Projekt ist für den privaten Gebrauch konzipiert. Stelle sicher, dass du die Datenschutzrichtlinien deines Landes beachtest, insbesondere bei der Kamera-Überwachung.
