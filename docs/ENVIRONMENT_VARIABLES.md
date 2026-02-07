# Environment Variables Guide

Vollständige Referenz aller Umgebungsvariablen für Renfield.

---

## 📋 Inhaltsverzeichnis

- [Naming Conventions](#naming-conventions)
- [Core System](#core-system)
- [RAG (Wissensspeicher)](#rag-wissensspeicher)
- [Audio Output Routing](#audio-output-routing)
- [Integrationen](#integrationen)
- [MCP Server Configuration](#mcp-server-configuration)
- [Plugin System (Legacy YAML)](#plugin-system-legacy-yaml)
- [Verfügbare Plugins](#verfügbare-plugins)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

---

## Naming Conventions

### Plugin-spezifische Variablen

**Format:**
```
{PLUGIN_NAME}_PLUGIN_{PURPOSE}    # YAML-Plugin Aktivierung
{SERVICE_NAME}_{PURPOSE}          # MCP-Server Aktivierung & Konfiguration
```

**Beispiele:**
```bash
# MCP-Server (bevorzugt)
WEATHER_ENABLED=true              # MCP-Server aktivieren
OPENWEATHER_API_KEY=abc123        # API-Schlüssel (in Produktion: Docker Secret)

# YAML-Plugin (Legacy)
WEATHER_PLUGIN_ENABLED=true       # YAML-Plugin aktivieren
OPENWEATHER_API_URL=https://...   # API-URL
```

### Regeln

1. **UPPERCASE_SNAKE_CASE** - Alle Buchstaben groß, Wörter mit Unterstrich getrennt
2. **Beschreibende Namen** - Klar erkennbar, wofür die Variable ist
3. **Konsistente Suffixe:**
   - `_ENABLED` - Boolean zum Aktivieren (MCP-Server)
   - `_PLUGIN_ENABLED` - Boolean zum Aktivieren (YAML-Plugins)
   - `_URL` - API-Endpunkte
   - `_KEY` - API-Schlüssel
   - `_TOKEN` - Authentifizierungs-Token

---

## Core System

### Datenbank

```bash
# PostgreSQL Passwort
POSTGRES_PASSWORD=changeme_secure_password
```

**Default:** `changeme`
**Hinweis:** In Produktion IMMER ändern!

---

### Redis

```bash
# Wird automatisch konfiguriert
REDIS_URL=redis://redis:6379
```

**Default:** `redis://redis:6379`
**Hinweis:** Nur ändern wenn externes Redis verwendet wird.

---

### Ollama LLM

```bash
# Ollama URL (intern oder extern)
OLLAMA_URL=http://ollama:11434
OLLAMA_URL=http://cuda.local:11434  # Externe GPU-Instanz

# Ollama Modell
OLLAMA_MODEL=qwen3:8b
```

**Defaults:**
- `OLLAMA_URL`: `http://ollama:11434`
- `OLLAMA_MODEL`: `llama3.2:3b` (dev fallback)

**Empfohlene Modelle:**
- `qwen3:8b` - Bestes Preis-Leistungs-Verhältnis, starkes Deutsch
- `qwen3:14b` - Sehr gute Qualität für Chat/RAG (empfohlen mit GPU)
- `qwen3-embedding:4b` - Embedding-Modell mit exzellentem Deutsch

Siehe `docs/LLM_MODEL_GUIDE.md` für eine vollständige Modell-Übersicht pro Rolle.

---

### Sprache & Voice

```bash
# Standard-Sprache für STT/TTS
DEFAULT_LANGUAGE=de

# Unterstützte Sprachen (kommasepariert)
SUPPORTED_LANGUAGES=de,en

# Whisper STT Modell
WHISPER_MODEL=base

# Piper TTS Voice (Standard-Stimme)
PIPER_VOICE=de_DE-thorsten-high

# Piper Multi-Voice Konfiguration (pro Sprache)
PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium
```

**Defaults:**
- `DEFAULT_LANGUAGE`: `de`
- `SUPPORTED_LANGUAGES`: `de,en`
- `WHISPER_MODEL`: `base`
- `PIPER_VOICE`: `de_DE-thorsten-high`
- `PIPER_VOICES`: (nicht gesetzt, nutzt `PIPER_VOICE` für alle Sprachen)

**Whisper Modelle:**
- `tiny` - Sehr schnell, niedrige Qualität
- `base` - Schnell, gute Qualität (Empfohlen)
- `small` - Langsamer, bessere Qualität
- `medium` - Langsam, hohe Qualität
- `large` - Sehr langsam, beste Qualität

**Piper Stimmen (Beispiele):**
- `de_DE-thorsten-high` - Deutsch, männlich, hohe Qualität
- `de_DE-eva_k-medium` - Deutsch, weiblich, mittlere Qualität
- `en_US-amy-medium` - Englisch (US), weiblich, mittlere Qualität
- `en_GB-cori-medium` - Englisch (UK), weiblich, mittlere Qualität

**Hinweis:** Die Frontend-Sprache wird unabhängig im Browser gespeichert (`localStorage`) und kann über das Globus-Symbol im Header geändert werden.

---

### Monitoring

```bash
# Prometheus Metrics Endpoint aktivieren
METRICS_ENABLED=false
```

**Default:** `false`

**Wenn aktiviert:**
- `/metrics` Endpoint im Prometheus-Format verfügbar
- HTTP Request Counter + Latency Histogram
- WebSocket Connection Gauge
- LLM Call Duration Histogram
- Agent Steps Histogram
- Circuit Breaker State + Failures

**Prometheus Scrape Config:**
```yaml
scrape_configs:
  - job_name: 'renfield'
    static_configs:
      - targets: ['renfield-backend:8000']
```

---

### Logging

```bash
# Log Level
LOG_LEVEL=INFO
```

**Default:** `INFO`

**Levels:**
- `DEBUG` - Alles loggen (für Entwicklung)
- `INFO` - Normale Informationen (Empfohlen)
- `WARNING` - Nur Warnungen und Fehler
- `ERROR` - Nur Fehler

---

### Agent Loop (ReAct)

```bash
# Agent Loop aktivieren (Multi-Step Tool Chaining)
AGENT_ENABLED=false

# Maximale Reasoning-Schritte pro Anfrage
AGENT_MAX_STEPS=5

# Timeout pro LLM-Call (Sekunden)
AGENT_STEP_TIMEOUT=30.0

# Gesamt-Timeout für gesamten Agent-Run (Sekunden)
AGENT_TOTAL_TIMEOUT=120.0

# Optionales separates Modell für Agent (Standard: OLLAMA_MODEL)
# AGENT_MODEL=qwen3:14b
```

**Defaults:**
- `AGENT_ENABLED`: `false` (Opt-in)
- `AGENT_MAX_STEPS`: `5`
- `AGENT_STEP_TIMEOUT`: `30.0`
- `AGENT_TOTAL_TIMEOUT`: `120.0`
- `AGENT_MODEL`: None (nutzt `OLLAMA_MODEL`)

**Wann aktivieren:**
Der Agent Loop ermöglicht komplexe, mehrstufige Anfragen mit bedingter Logik und Tool-Verkettung:
- "Wie ist das Wetter in Berlin und wenn es kälter als 10 Grad ist, suche ein Hotel"
- "Schalte das Licht ein und dann stelle die Heizung auf 22 Grad"

Einfache Anfragen ("Schalte das Licht ein") nutzen weiterhin den schnellen Single-Intent-Pfad.

---

### Proaktive Benachrichtigungen

```bash
# Master-Switch (opt-in)
PROACTIVE_ENABLED=false

# Dedup-Fenster in Sekunden (gleiche Nachricht wird innerhalb dieses Zeitfensters unterdrückt)
PROACTIVE_SUPPRESSION_WINDOW=60

# TTS standardmäßig an bei Webhook-Benachrichtigungen
PROACTIVE_TTS_DEFAULT=true

# Notification-Ablauf in Sekunden (abgelaufene werden automatisch gelöscht)
PROACTIVE_NOTIFICATION_TTL=86400
```

**Defaults:**
- `PROACTIVE_ENABLED`: `false` (Opt-in)
- `PROACTIVE_SUPPRESSION_WINDOW`: `60` (1 Minute)
- `PROACTIVE_TTS_DEFAULT`: `true`
- `PROACTIVE_NOTIFICATION_TTL`: `86400` (24 Stunden)

**Webhook-Token:** Wird NICHT in `.env` gespeichert, sondern in der Datenbank (`SystemSetting`). Token wird über die Admin-API generiert/rotiert: `POST /api/notifications/token`.

**Endpunkte:**
- `POST /api/notifications/webhook` — Webhook-Empfang (Bearer Token Auth)
- `GET /api/notifications` — Liste mit Filtern (room_id, urgency, status, since)
- `PATCH /api/notifications/{id}/acknowledge` — Bestätigen
- `DELETE /api/notifications/{id}` — Verwerfen (Soft Delete)
- `POST /api/notifications/token` — Token generieren/rotieren (Admin)

**Dokumentation:** Siehe `docs/PROACTIVE_NOTIFICATIONS.md` für Details und HA-Automations-Template.

#### Phase 2: Notification Intelligence

```bash
# Semantische Deduplizierung — erkennt Paraphrasen via pgvector Cosine Similarity
PROACTIVE_SEMANTIC_DEDUP_ENABLED=false
PROACTIVE_SEMANTIC_DEDUP_THRESHOLD=0.85

# Urgency Auto-Klassifizierung — LLM klassifiziert urgency: "auto" → critical/info/low
PROACTIVE_URGENCY_AUTO_ENABLED=false

# LLM Content Enrichment — Natürlich-sprachliche Aufbereitung der Nachricht
PROACTIVE_ENRICHMENT_ENABLED=false
PROACTIVE_ENRICHMENT_MODEL=              # Optional: separates Modell (Default: OLLAMA_MODEL)

# Feedback-Learning — "Nicht mehr melden"-Button erstellt Suppression-Regeln
PROACTIVE_FEEDBACK_LEARNING_ENABLED=false
PROACTIVE_FEEDBACK_SIMILARITY_THRESHOLD=0.80
```

**Zusätzliche Endpunkte:**
- `POST /api/notifications/{id}/suppress` — Ähnliche Benachrichtigungen unterdrücken
- `GET /api/notifications/suppressions` — Aktive Suppression-Regeln
- `DELETE /api/notifications/suppressions/{id}` — Suppression aufheben

#### Reminders

```bash
# Timer-Erinnerungen ("in 30 Minuten", "um 18:00")
PROACTIVE_REMINDERS_ENABLED=false
PROACTIVE_REMINDER_CHECK_INTERVAL=15     # Prüfintervall in Sekunden
```

**Reminder-Endpunkte:**
- `POST /api/notifications/reminders` — Erinnerung erstellen
- `GET /api/notifications/reminders` — Offene Erinnerungen
- `DELETE /api/notifications/reminders/{id}` — Erinnerung stornieren

#### Externe Scheduling-Templates

Cron-basiertes Scheduling (z.B. Morgenbriefing) wird extern via **n8n-Workflows** oder **Home Assistant-Automationen** gelöst. Diese senden per Webhook an `POST /api/notifications/webhook`.

Siehe `docs/PROACTIVE_SCHEDULING_TEMPLATES.md` für fertige Templates.

---

### RAG (Wissensspeicher)

```bash
# RAG aktivieren
RAG_ENABLED=true

# Chunking
RAG_CHUNK_SIZE=512               # Token-Limit pro Chunk
RAG_CHUNK_OVERLAP=50             # Überlappung zwischen Chunks
RAG_TOP_K=5                      # Anzahl der relevantesten Chunks
RAG_SIMILARITY_THRESHOLD=0.4     # Minimum Similarity für Dense-only (0-1)

# Hybrid Search (Dense + BM25 via Reciprocal Rank Fusion)
RAG_HYBRID_ENABLED=true          # Hybrid Search aktivieren
RAG_HYBRID_BM25_WEIGHT=0.3      # BM25-Gewicht im RRF (0.0-1.0)
RAG_HYBRID_DENSE_WEIGHT=0.7     # Dense-Gewicht im RRF (0.0-1.0)
RAG_HYBRID_RRF_K=60             # RRF-Konstante k (Standard: 60)
RAG_HYBRID_FTS_CONFIG=simple    # PostgreSQL FTS: simple/german/english

# Context Window (benachbarte Chunks zum Treffer hinzufügen)
RAG_CONTEXT_WINDOW=1             # Chunks pro Richtung (0=deaktiviert)
RAG_CONTEXT_WINDOW_MAX=3         # Maximale Window-Größe
```

**Defaults:**
- `RAG_ENABLED`: `true`
- `RAG_CHUNK_SIZE`: `512`
- `RAG_CHUNK_OVERLAP`: `50`
- `RAG_TOP_K`: `5`
- `RAG_SIMILARITY_THRESHOLD`: `0.4`
- `RAG_HYBRID_ENABLED`: `true`
- `RAG_HYBRID_BM25_WEIGHT`: `0.3`
- `RAG_HYBRID_DENSE_WEIGHT`: `0.7`
- `RAG_HYBRID_RRF_K`: `60`
- `RAG_HYBRID_FTS_CONFIG`: `simple`
- `RAG_CONTEXT_WINDOW`: `1`
- `RAG_CONTEXT_WINDOW_MAX`: `3`

**Hybrid Search:**
Kombiniert Dense-Embeddings (pgvector Cosine Similarity) mit BM25 Full-Text Search (PostgreSQL tsvector) via Reciprocal Rank Fusion (RRF). Dense findet semantisch ähnliche Chunks, BM25 findet exakte Keyword-Matches. RRF kombiniert beide Rankings robust und score-unabhängig.

**FTS Config:**
- `simple` — Sprachunabhängig, kein Stemming (Standard)
- `german` — Deutsch Stemming (z.B. "Häuser" → "Haus")
- `english` — English Stemming

Nach Änderung der FTS-Config: `POST /api/knowledge/reindex-fts` ausführen.

**Context Window:**
Erweitert jeden Treffer-Chunk um benachbarte Chunks aus demselben Dokument für mehr Kontext. Bei `RAG_CONTEXT_WINDOW=1` wird ein Chunk links und rechts hinzugefügt. Deduplizierung verhindert doppelte Chunks wenn benachbarte Chunks beide Treffer sind.

---

### Conversation Memory (Langzeitgedaechtnis)

```bash
# Langzeitgedaechtnis aktivieren
MEMORY_ENABLED=false

# Retrieval-Einstellungen
MEMORY_RETRIEVAL_LIMIT=3             # Max Memories pro Query
MEMORY_RETRIEVAL_THRESHOLD=0.7      # Cosine-Similarity Schwellwert (0-1)
MEMORY_MAX_PER_USER=500             # Max aktive Memories pro User
MEMORY_CONTEXT_DECAY_DAYS=30        # Tage bis Context-Memories verfallen
MEMORY_DEDUP_THRESHOLD=0.9          # Deduplizierungs-Schwellwert (0.5-1.0)

# Automatische Extraktion
MEMORY_EXTRACTION_ENABLED=false     # Fakten automatisch aus Dialogen extrahieren

# Widerspruchserkennung (zweiter LLM-Pass)
MEMORY_CONTRADICTION_RESOLUTION=false   # LLM-basierte Widerspruchserkennung aktivieren
MEMORY_CONTRADICTION_THRESHOLD=0.6      # Similarity-Untergrenze fuer Vergleich (0.3-0.89)
MEMORY_CONTRADICTION_TOP_K=5            # Max bestehende Erinnerungen zum Vergleich (1-10)
```

**Defaults:**
- `MEMORY_ENABLED`: `false`
- `MEMORY_RETRIEVAL_LIMIT`: `3`
- `MEMORY_RETRIEVAL_THRESHOLD`: `0.7`
- `MEMORY_MAX_PER_USER`: `500`
- `MEMORY_CONTEXT_DECAY_DAYS`: `30`
- `MEMORY_DEDUP_THRESHOLD`: `0.9`
- `MEMORY_EXTRACTION_ENABLED`: `false`
- `MEMORY_CONTRADICTION_RESOLUTION`: `false`
- `MEMORY_CONTRADICTION_THRESHOLD`: `0.6`
- `MEMORY_CONTRADICTION_TOP_K`: `5`

**Automatische Extraktion:**
Wenn `MEMORY_EXTRACTION_ENABLED=true` (und `MEMORY_ENABLED=true`), analysiert das LLM nach jeder Konversationsrunde den Dialog und extrahiert erinnerungswuerdige Fakten (Praeferenzen, persoenliche Fakten, Anweisungen, Kontext). Die Extraktion laeuft als Background-Task und blockiert nicht die Antwort an den Benutzer.

**Widerspruchserkennung:**
Wenn `MEMORY_CONTRADICTION_RESOLUTION=true` (und `MEMORY_EXTRACTION_ENABLED=true`), wird nach der Faktenextraktion ein zweiter LLM-Pass ausgefuehrt. Dieser vergleicht neue Fakten mit bestehenden Erinnerungen (Similarity-Bereich 0.6-0.89) und entscheidet: ADD (neuer Fakt), UPDATE (bestehende Erinnerung aktualisieren), DELETE (bestehende Erinnerung ersetzen) oder NOOP (bereits bekannt). Alle Aenderungen werden in der `memory_history`-Tabelle protokolliert. Audittrail via `GET /api/memory/{id}/history`.

---

### Satellite System

```bash
# Wake Word Konfiguration
WAKE_WORD_DEFAULT=alexa
WAKE_WORD_THRESHOLD=0.5

# Zeroconf Service Advertisement
ADVERTISE_HOST=renfield
# Oder:
ADVERTISE_IP=192.168.1.100
```

**Defaults:**
- `WAKE_WORD_DEFAULT`: `alexa`
- `WAKE_WORD_THRESHOLD`: `0.5`

**Wake Word Optionen:**
- `alexa` - "Alexa" (empfohlen, funktioniert auf 32-bit)
- `hey_mycroft` - "Hey Mycroft"
- `hey_jarvis` - "Hey Jarvis"

**Zeroconf:**
- Satellites finden das Backend automatisch über mDNS
- Setze `ADVERTISE_HOST` auf den Hostnamen deines Servers
- Alternativ `ADVERTISE_IP` für eine feste IP-Adresse

---

### Audio Output Routing

```bash
# Hostname/IP die externe Dienste (z.B. Home Assistant) erreichen können
ADVERTISE_HOST=demeter.local

# Port für ADVERTISE_HOST (optional)
ADVERTISE_PORT=8000
```

**Defaults:**
- `ADVERTISE_HOST`: None (muss gesetzt werden für HA Media Player Output)
- `ADVERTISE_PORT`: `8000`

**Wann benötigt:**
- Wenn TTS-Ausgabe auf Home Assistant Media Playern erfolgen soll
- Der Wert muss eine Adresse sein, die Home Assistant erreichen kann (nicht `localhost`!)

**Beispiele:**
```bash
ADVERTISE_HOST=192.168.1.100      # IP-Adresse
ADVERTISE_HOST=renfield.local     # mDNS Hostname
ADVERTISE_HOST=demeter.local      # Server Hostname
```

**Ohne ADVERTISE_HOST:**
- TTS wird nur auf Renfield-Geräten (Satellites, Web Panels) abgespielt
- HA Media Player können keine TTS-Dateien abrufen

**Dokumentation:** Siehe `OUTPUT_ROUTING.md` für Details zum Output Routing System.

---

### Security

```bash
# Secret Key für Sessions/JWT
SECRET_KEY=changeme-in-production-use-strong-random-key

# CORS Origins (kommasepariert oder "*" für Entwicklung)
CORS_ORIGINS=*
CORS_ORIGINS=https://renfield.local,https://admin.local
```

**Defaults:**
- `SECRET_KEY`: `changeme-in-production-use-strong-random-key`
- `CORS_ORIGINS`: `*`

**Hinweis:** In Produktion IMMER durch starken Zufallsschlüssel und spezifische Origins ersetzen!

**Generierung:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

---

### Authentication (RPBAC)

```bash
# Authentifizierung aktivieren (Standard: deaktiviert für Entwicklung)
AUTH_ENABLED=false

# JWT Token Gültigkeitsdauer
ACCESS_TOKEN_EXPIRE_MINUTES=1440       # 24 Stunden
REFRESH_TOKEN_EXPIRE_DAYS=30

# Passwort-Policy
PASSWORD_MIN_LENGTH=8

# Registrierung erlauben
ALLOW_REGISTRATION=true

# Standard-Admin Zugangsdaten (nur beim ersten Start verwendet)
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=changeme

# Voice Authentication
VOICE_AUTH_ENABLED=false
VOICE_AUTH_MIN_CONFIDENCE=0.7
```

**Defaults:**
- `AUTH_ENABLED`: `false` (für einfache Entwicklung)
- `ACCESS_TOKEN_EXPIRE_MINUTES`: `1440` (24 Stunden)
- `REFRESH_TOKEN_EXPIRE_DAYS`: `30`
- `PASSWORD_MIN_LENGTH`: `8`
- `ALLOW_REGISTRATION`: `true`
- `DEFAULT_ADMIN_USERNAME`: `admin`
- `DEFAULT_ADMIN_PASSWORD`: `changeme`
- `VOICE_AUTH_ENABLED`: `false`
- `VOICE_AUTH_MIN_CONFIDENCE`: `0.7`

**Produktion:**
```bash
# EMPFOHLEN für Produktion:
AUTH_ENABLED=true
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
DEFAULT_ADMIN_PASSWORD=starkes-zufalls-passwort
ALLOW_REGISTRATION=false  # Nur Admin erstellt Benutzer
```

**Hinweis:** Beim ersten Start wird automatisch ein Admin-Benutzer erstellt, wenn noch keine Benutzer existieren. Das Passwort MUSS in Produktion geändert werden!

**Voice Authentication:**
- Ermöglicht Login per Stimmerkennung
- Sprecher muss mit einem User-Account verknüpft sein
- Confidence-Threshold verhindert falsche Identifikation

**Dokumentation:** Siehe `ACCESS_CONTROL.md` für Details zum Berechtigungssystem.

---

### WebSocket Security

```bash
# WebSocket Authentifizierung aktivieren (für Produktion empfohlen!)
WS_AUTH_ENABLED=false

# Token-Gültigkeitsdauer in Minuten
WS_TOKEN_EXPIRE_MINUTES=60

# Rate Limiting aktivieren
WS_RATE_LIMIT_ENABLED=true

# Maximale Messages pro Sekunde/Minute (Audio-Streaming sendet ~12.5 Chunks/Sek.)
WS_RATE_LIMIT_PER_SECOND=50
WS_RATE_LIMIT_PER_MINUTE=1000

# Maximale WebSocket-Verbindungen pro IP
WS_MAX_CONNECTIONS_PER_IP=10

# Maximale Message-Größe in Bytes (Standard: 1MB)
WS_MAX_MESSAGE_SIZE=1000000

# Maximale Audio-Buffer-Größe pro Session in Bytes (Standard: 10MB)
WS_MAX_AUDIO_BUFFER_SIZE=10000000

# WebSocket Protokoll-Version
WS_PROTOCOL_VERSION=1.0
```

**Defaults:**
- `WS_AUTH_ENABLED`: `false` (für Entwicklung)
- `WS_TOKEN_EXPIRE_MINUTES`: `60`
- `WS_RATE_LIMIT_ENABLED`: `true`
- `WS_RATE_LIMIT_PER_SECOND`: `50` (Audio-Streaming benötigt ~12.5/Sek.)
- `WS_RATE_LIMIT_PER_MINUTE`: `1000`
- `WS_MAX_CONNECTIONS_PER_IP`: `10`
- `WS_MAX_MESSAGE_SIZE`: `1000000` (1MB)
- `WS_MAX_AUDIO_BUFFER_SIZE`: `10000000` (10MB)
- `WS_PROTOCOL_VERSION`: `1.0`

**Produktion:**
```bash
# EMPFOHLEN für Produktion:
WS_AUTH_ENABLED=true
CORS_ORIGINS=https://yourdomain.com
```

**Token-Generierung (wenn WS_AUTH_ENABLED=true):**
```bash
# Token für ein Gerät anfordern
curl -X POST "http://localhost:8000/api/ws/token?device_id=my-device&device_type=web_browser"
```

**WebSocket-Verbindung mit Token:**
```javascript
// JavaScript
const ws = new WebSocket(`ws://localhost:8000/ws?token=${token}`);
```

---

## Integrationen

### Home Assistant

```bash
# Home Assistant URL
HOME_ASSISTANT_URL=http://homeassistant.local:8123

# Long-Lived Access Token
HOME_ASSISTANT_TOKEN=eyJhbGci...
```

**Erforderlich:** Ja
**Token erstellen:**
1. Home Assistant öffnen
2. Profil → Lange Zugangstoken erstellen
3. Token kopieren und in `.env` einfügen

---

### n8n

```bash
# n8n Webhook URL
N8N_WEBHOOK_URL=http://192.168.1.78:5678/webhook
```

**Erforderlich:** Optional
**Format:** `http://<n8n-host>:<port>/webhook`

---

### Frigate

```bash
# Frigate URL
FRIGATE_URL=http://frigate.local:5000
```

**Erforderlich:** Optional
**Format:** `http://<frigate-host>:<port>`

---

## MCP Server Configuration

MCP (Model Context Protocol) Server stellen externe Tools für den Agent Loop bereit. Konfiguration in `config/mcp_servers.yaml`.

### System-Kontrolle

```bash
# MCP System aktivieren
MCP_ENABLED=true
```

**Default:** `false`

---

### MCP-Server aktivieren

```bash
# Weather (OpenWeatherMap)
WEATHER_ENABLED=true

# Search (SearXNG)
SEARCH_ENABLED=true

# News (NewsAPI)
NEWS_ENABLED=true

# Jellyfin (Media Server)
JELLYFIN_ENABLED=true

# n8n (Workflow Automation)
N8N_MCP_ENABLED=true

# Home Assistant (Smart Home)
HA_MCP_ENABLED=true
```

**Defaults:** Alle `false`

### MCP-Server Secrets (Produktion: Docker Secrets)

| Variable | Beschreibung | Docker Secret |
|----------|-------------|---------------|
| `OPENWEATHER_API_KEY` | OpenWeatherMap API Key | `secrets/openweather_api_key` |
| `NEWSAPI_KEY` | NewsAPI Key | `secrets/newsapi_key` |
| `JELLYFIN_TOKEN` | Jellyfin API Token | `secrets/jellyfin_token` |
| `JELLYFIN_BASE_URL` | Jellyfin Server URL | `secrets/jellyfin_base_url` |
| `N8N_API_KEY` | n8n API Key | `secrets/n8n_api_key` |
| `HOME_ASSISTANT_TOKEN` | HA Long-Lived Access Token | `secrets/home_assistant_token` |

### MCP-Server URLs (nicht-sensitiv, in .env)

```bash
# Home Assistant URL
HOME_ASSISTANT_URL=http://homeassistant.local:8123

# n8n Base URL
N8N_BASE_URL=http://192.168.1.78:5678

# SearXNG URL
SEARXNG_URL=http://cuda.local:3002
```

**Hinweis:** In Produktion werden Secrets über Docker Compose File-Based Secrets bereitgestellt und von `mcp_client.py` automatisch in `os.environ` injiziert. Siehe `docs/SECRETS_MANAGEMENT.md`.

---

## Plugin System (Legacy YAML)

> **Hinweis:** YAML-Plugins werden durch MCP-Server ersetzt. Plugins verwenden `*_PLUGIN_ENABLED` zur Aktivierung, um Konflikte mit MCP-Server `*_ENABLED` Variablen zu vermeiden.

### System-Kontrolle

```bash
# Plugin System aktivieren/deaktivieren
PLUGINS_ENABLED=true

# Plugin-Verzeichnis (relativ zum Backend)
PLUGINS_DIR=integrations/plugins
```

**Defaults:**
- `PLUGINS_ENABLED`: `true`
- `PLUGINS_DIR`: `integrations/plugins`

**Hinweis:** Wenn `PLUGINS_ENABLED=false`, werden KEINE Plugins geladen.

---

## Verfügbare Plugins

### Weather Plugin (OpenWeatherMap)

```bash
# Plugin aktivieren
WEATHER_PLUGIN_ENABLED=true

# API-Konfiguration
OPENWEATHER_API_URL=https://api.openweathermap.org/data/2.5
OPENWEATHER_API_KEY=your_api_key_here
```

**Erforderlich:**
- `WEATHER_PLUGIN_ENABLED` - Boolean
- `OPENWEATHER_API_URL` - API-Basis-URL
- `OPENWEATHER_API_KEY` - API-Schlüssel

**API-Key erhalten:**
1. https://openweathermap.org/api registrieren
2. Free Tier auswählen (60 calls/minute)
3. API-Key kopieren

**Intents:**
- `weather.get_current` - Aktuelles Wetter
- `weather.get_forecast` - Wettervorhersage

---

### News Plugin (NewsAPI)

```bash
# Plugin aktivieren
NEWS_PLUGIN_ENABLED=true

# API-Konfiguration
NEWSAPI_URL=https://newsapi.org/v2
NEWSAPI_KEY=your_api_key_here
```

**Erforderlich:**
- `NEWS_PLUGIN_ENABLED` - Boolean
- `NEWSAPI_URL` - API-Basis-URL
- `NEWSAPI_KEY` - API-Schlüssel

**API-Key erhalten:**
1. https://newsapi.org/register registrieren
2. Free Tier: 100 requests/day
3. API-Key kopieren

**Intents:**
- `news.get_headlines` - Top-Schlagzeilen
- `news.search` - Artikel-Suche

---

### Search Plugin (SearXNG)

```bash
# Plugin aktivieren
SEARCH_PLUGIN_ENABLED=true

# SearXNG-Instanz URL (kein Key nötig!)
SEARXNG_API_URL=http://cuda.local:3002
```

**Erforderlich:**
- `SEARCH_PLUGIN_ENABLED` - Boolean
- `SEARXNG_API_URL` - SearXNG-Instanz-URL

**API-Key:** Nicht erforderlich! ✅

**Hinweis:** Benötigt eine laufende SearXNG-Instanz.
SearXNG ist eine Privacy-respektierende Metasearch-Engine.

**Setup:** https://docs.searxng.org/

**Intents:**
- `search.web` - Web-Suche
- `search.instant_answer` - Schnelle Antworten

---

### Music Plugin (Spotify)

```bash
# Plugin aktivieren
MUSIC_PLUGIN_ENABLED=true

# API-Konfiguration
SPOTIFY_API_URL=https://api.spotify.com
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_ACCESS_TOKEN=your_access_token
```

**Erforderlich:**
- `MUSIC_PLUGIN_ENABLED` - Boolean
- `SPOTIFY_API_URL` - API-Basis-URL
- `SPOTIFY_ACCESS_TOKEN` - User Access Token

**Optional:**
- `SPOTIFY_CLIENT_ID` - OAuth Client ID
- `SPOTIFY_CLIENT_SECRET` - OAuth Client Secret

**Access Token erhalten:**
1. https://developer.spotify.com/console/ öffnen
2. Gewünschte Scopes auswählen
3. "Get Token" klicken
4. Token kopieren

**Intents:**
- `music.search` - Musik suchen
- `music.play` - Abspielen
- `music.pause` - Pausieren
- `music.resume` - Fortsetzen
- `music.next` - Nächster Track
- `music.previous` - Vorheriger Track
- `music.volume` - Lautstärke setzen
- `music.current` - Aktuellen Track anzeigen

---

### Jellyfin Plugin (DLNA/UPnP Media Server)

```bash
# Plugin aktivieren
JELLYFIN_PLUGIN_ENABLED=true

# API-Konfiguration
JELLYFIN_URL=http://192.168.1.123:8096
JELLYFIN_API_KEY=your_api_key_here
JELLYFIN_USER_ID=your_user_id_here
```

**Erforderlich:**
- `JELLYFIN_PLUGIN_ENABLED` - Boolean
- `JELLYFIN_URL` - Jellyfin Server URL (inkl. Port, Standard: 8096)
- `JELLYFIN_API_KEY` - API-Schlüssel
- `JELLYFIN_USER_ID` - Benutzer-ID für personalisierte Bibliothek

**API-Key erhalten:**
1. Jellyfin Dashboard öffnen → Administration → API Keys
2. "+" klicken, Namen vergeben (z.B. "Renfield")
3. API-Key kopieren

**User-ID erhalten:**
1. Jellyfin Dashboard → Administration → Users
2. Gewünschten Benutzer auswählen
3. User-ID aus der URL kopieren (z.B. `d4f8...`)
4. Oder: `curl "http://192.168.1.123:8096/Users?api_key=YOUR_KEY"` → `Id` Feld

**Intents:**
- `jellyfin.search_music` - Musik suchen (Songs, Alben, Künstler)
- `jellyfin.list_albums` - Alle Alben anzeigen
- `jellyfin.list_artists` - Alle Künstler anzeigen
- `jellyfin.get_album_tracks` - Tracks eines Albums
- `jellyfin.get_artist_albums` - Alben eines Künstlers
- `jellyfin.get_genres` - Alle Genres anzeigen
- `jellyfin.get_recent` - Kürzlich hinzugefügte Musik
- `jellyfin.get_favorites` - Favoriten anzeigen
- `jellyfin.get_playlists` - Playlists anzeigen
- `jellyfin.get_stream_url` - Streaming-URL abrufen
- `jellyfin.library_stats` - Bibliotheks-Statistiken

**Beispiele:**
- "Suche nach Musik von Queen"
- "Zeige mir alle Alben"
- "Welche Künstler habe ich?"
- "Neue Musik anzeigen"
- "Meine Lieblingssongs"

---

## Best Practices

### 1. Niemals Secrets committen

**❌ Falsch:**
```bash
git add .env
git commit -m "Add config"
```

**✅ Richtig:**
```bash
# .env in .gitignore
echo ".env" >> .gitignore
git add .gitignore
```

---

### 2. .env.example verwenden

Erstelle `.env.example` ohne echte Werte:

```bash
# .env.example
WEATHER_ENABLED=false
OPENWEATHER_API_URL=https://api.openweathermap.org/data/2.5
OPENWEATHER_API_KEY=your_api_key_here
```

Committe nur `.env.example`, nie `.env`!

---

### 3. Starke Secrets verwenden

**Generiere starke Zufallswerte:**

```bash
# Passwort generieren
openssl rand -base64 32

# Secret Key generieren
python3 -c "import secrets; print(secrets.token_urlsafe(64))"

# UUID generieren
uuidgen
```

---

### 4. Verschiedene Werte pro Umgebung

```bash
# Entwicklung (.env.development)
OLLAMA_URL=http://localhost:11434
LOG_LEVEL=DEBUG

# Produktion (.env.production)
OLLAMA_URL=http://cuda.local:11434
LOG_LEVEL=INFO
```

---

### 5. Dokumentiere Custom-Variablen

Wenn du eigene Plugins erstellst, dokumentiere die Variablen:

```yaml
# In plugin YAML:
config:
  url: MY_PLUGIN_API_URL
  api_key: MY_PLUGIN_API_KEY

# In .env:
MY_PLUGIN_ENABLED=true
MY_PLUGIN_API_URL=https://api.example.com
MY_PLUGIN_API_KEY=abc123
```

---

## Troubleshooting

### Variable wird nicht geladen

**Problem:** Plugin findet API-Key nicht

**Prüfen:**
```bash
# Ist die Variable gesetzt?
docker exec renfield-backend env | grep WEATHER

# Container neu erstellen (nicht nur restart!)
docker compose up -d --force-recreate backend
```

---

### Falsche Werte

**Problem:** URL oder Key falsch formatiert

**Prüfen:**
```bash
# Variable direkt testen
docker exec renfield-backend python3 -c "import os; print(os.getenv('WEATHER_API_KEY'))"

# Sollte den Key ausgeben, nicht None
```

---

### Plugin lädt nicht

**Problem:** `*_PLUGIN_ENABLED` Variable nicht gesetzt

**Prüfen:**
```bash
# Logs checken
docker logs renfield-backend | grep -i plugin

# Sollte zeigen:
# ✅ Loaded plugin: weather v1.0.0
# Nicht:
# ⏭️  Skipped disabled plugin: weather
```

---

### Umlaute/Sonderzeichen

**Problem:** Encoding-Fehler in .env

**Lösung:**
```bash
# .env MUSS UTF-8 encoded sein
file .env
# Sollte ausgeben: .env: UTF-8 Unicode text

# Falls nicht, konvertieren:
iconv -f ISO-8859-1 -t UTF-8 .env > .env.utf8
mv .env.utf8 .env
```

---

## Template für neue Plugins

```bash
# =============================================================================
# {PLUGIN_NAME} Plugin
# =============================================================================

# Plugin aktivieren
{PLUGIN_NAME}_PLUGIN_ENABLED=false

# API-Konfiguration
{PLUGIN_NAME}_API_URL=https://api.example.com
{PLUGIN_NAME}_API_KEY=your_api_key_here

# Optionale Zusatz-Konfiguration
{PLUGIN_NAME}_REGION=eu-central-1
{PLUGIN_NAME}_TIMEOUT=10
```

---

## Vollständige .env Beispiel-Datei

```bash
# =============================================================================
# Renfield Environment Configuration
# =============================================================================

# -----------------------------------------------------------------------------
# Core System
# -----------------------------------------------------------------------------
POSTGRES_PASSWORD=changeme_secure_password
LOG_LEVEL=INFO
SECRET_KEY=changeme-in-production

# -----------------------------------------------------------------------------
# Security (WebSocket & CORS)
# -----------------------------------------------------------------------------
CORS_ORIGINS=*
WS_AUTH_ENABLED=false
WS_RATE_LIMIT_ENABLED=true
WS_MAX_CONNECTIONS_PER_IP=10

# -----------------------------------------------------------------------------
# Ollama LLM
# -----------------------------------------------------------------------------
OLLAMA_URL=http://cuda.local:11434
OLLAMA_MODEL=qwen3:14b

# -----------------------------------------------------------------------------
# Sprache & Voice
# -----------------------------------------------------------------------------
DEFAULT_LANGUAGE=de
SUPPORTED_LANGUAGES=de,en
WHISPER_MODEL=base
PIPER_VOICE=de_DE-thorsten-high
# PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium  # Multi-Voice

# -----------------------------------------------------------------------------
# Integrationen
# -----------------------------------------------------------------------------
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=eyJhbGci...

N8N_WEBHOOK_URL=http://192.168.1.78:5678/webhook

FRIGATE_URL=http://frigate.local:5000

# -----------------------------------------------------------------------------
# RAG (Wissensspeicher)
# -----------------------------------------------------------------------------
RAG_ENABLED=true
# RAG_CHUNK_SIZE=512
# RAG_CHUNK_OVERLAP=50
# RAG_TOP_K=5
# RAG_SIMILARITY_THRESHOLD=0.4
RAG_HYBRID_ENABLED=true              # Dense + BM25 via RRF
# RAG_HYBRID_BM25_WEIGHT=0.3
# RAG_HYBRID_DENSE_WEIGHT=0.7
# RAG_HYBRID_FTS_CONFIG=simple       # simple/german/english
RAG_CONTEXT_WINDOW=1                 # Benachbarte Chunks pro Richtung

# -----------------------------------------------------------------------------
# Agent Loop (ReAct — Multi-Step Tool Chaining)
# -----------------------------------------------------------------------------
AGENT_ENABLED=false
# AGENT_MAX_STEPS=5
# AGENT_STEP_TIMEOUT=30.0
# AGENT_TOTAL_TIMEOUT=120.0
# AGENT_MODEL=                       # Optional: eigenes Modell für Agent

# -----------------------------------------------------------------------------
# Satellite System
# -----------------------------------------------------------------------------
WAKE_WORD_DEFAULT=alexa
WAKE_WORD_THRESHOLD=0.5

# -----------------------------------------------------------------------------
# Audio Output Routing
# -----------------------------------------------------------------------------
# Hostname/IP die externe Dienste (z.B. HA) erreichen können
ADVERTISE_HOST=demeter.local
ADVERTISE_PORT=8000

# -----------------------------------------------------------------------------
# Plugin System
# -----------------------------------------------------------------------------
PLUGINS_ENABLED=true

# -----------------------------------------------------------------------------
# MCP Server (bevorzugt)
# -----------------------------------------------------------------------------
MCP_ENABLED=true
WEATHER_ENABLED=true
SEARCH_ENABLED=true
NEWS_ENABLED=true
JELLYFIN_ENABLED=true
N8N_MCP_ENABLED=true
HA_MCP_ENABLED=true

# MCP-Server URLs (nicht-sensitiv)
N8N_BASE_URL=http://192.168.1.78:5678
SEARXNG_URL=http://cuda.local:3002

# MCP-Server Secrets: In Produktion als Docker Secrets!
# OPENWEATHER_API_KEY=...  → secrets/openweather_api_key
# NEWSAPI_KEY=...          → secrets/newsapi_key
# JELLYFIN_TOKEN=...       → secrets/jellyfin_token
# JELLYFIN_BASE_URL=...    → secrets/jellyfin_base_url
# N8N_API_KEY=...          → secrets/n8n_api_key

# -----------------------------------------------------------------------------
# YAML-Plugins (Legacy — deaktiviert wenn MCP aktiv)
# -----------------------------------------------------------------------------
# WEATHER_PLUGIN_ENABLED=false
# NEWS_PLUGIN_ENABLED=false
# SEARCH_PLUGIN_ENABLED=false
# MUSIC_PLUGIN_ENABLED=false
```

---

**Hinweis:** Passe die Werte an deine Umgebung an und committe NIE echte Secrets ins Repository!
