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
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

---

## Naming Conventions

### Regeln

1. **UPPERCASE_SNAKE_CASE** - Alle Buchstaben groß, Wörter mit Unterstrich getrennt
2. **Beschreibende Namen** - Klar erkennbar, wofür die Variable ist
3. **Konsistente Suffixe:**
   - `_ENABLED` - Boolean zum Aktivieren (MCP-Server)
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

# Optional: Fallback-URL wenn OLLAMA_URL nicht erreichbar (z.B. GPU-Host offline)
# Empfohlen wenn OLLAMA_URL auf ein externes Gerät zeigt.
# Im Docker-Container: http://host.docker.internal:11434 = Ollama auf dem Docker-Host
OLLAMA_FALLBACK_URL=http://host.docker.internal:11434

# Optional: Separate Ollama-Instanz nur für Embedding-Erzeugung
# Verhindert, dass Embedding-Calls mit LLM-Inferenz um GPU-Ressourcen konkurrieren
OLLAMA_EMBED_URL=http://host.docker.internal:11434

# Timeout-Konfiguration
OLLAMA_CONNECT_TIMEOUT=10.0    # TCP-Verbindungs-Timeout in Sekunden (Default: 10)
OLLAMA_READ_TIMEOUT=300.0      # Lese-Timeout für lange LLM-Antworten (Default: 300)

# Legacy Modell (Fallback für alle Rollen)
OLLAMA_MODEL=qwen3:8b

# Multi-Modell Konfiguration (überschreibt OLLAMA_MODEL pro Rolle)
OLLAMA_CHAT_MODEL=qwen3:14b          # Chat-Antworten
OLLAMA_RAG_MODEL=qwen3:14b           # RAG-Antworten
OLLAMA_EMBED_MODEL=nomic-embed-text  # Embedding-Erzeugung
OLLAMA_INTENT_MODEL=qwen3:8b         # Intent-Erkennung
OLLAMA_NUM_CTX=32768                  # Context Window für alle Ollama-Calls
```

**Defaults:**
- `OLLAMA_URL`: `http://ollama:11434`
- `OLLAMA_FALLBACK_URL`: `""` (kein Fallback)
- `OLLAMA_EMBED_URL`: `None` (verwendet `OLLAMA_URL`)
- `OLLAMA_CONNECT_TIMEOUT`: `10.0` Sekunden
- `OLLAMA_READ_TIMEOUT`: `300.0` Sekunden
- `OLLAMA_MODEL`: `llama3.2:3b` (dev fallback)
- `OLLAMA_CHAT_MODEL`: `llama3.2:3b`
- `OLLAMA_RAG_MODEL`: `llama3.2:latest`
- `OLLAMA_EMBED_MODEL`: `nomic-embed-text`
- `OLLAMA_INTENT_MODEL`: `llama3.2:3b`
- `OLLAMA_NUM_CTX`: `32768`

**Empfohlene Modelle:**
- `qwen3:14b` - Chat, RAG, Intent (empfohlen mit GPU)
- `qwen3:8b` - Gute Alternative für weniger RAM
- `qwen3-embedding:4b` - Embedding-Modell mit exzellentem Deutsch (768 dim)

Siehe `docs/LLM_MODEL_GUIDE.md` für eine vollständige Modell-Übersicht pro Rolle.

---

### Vision LLM (Satellite Camera)

```bash
# Vision-fähiges Modell für Kamera-Snapshots von Satellites
# Leer = Visual Queries deaktiviert (Bilder werden ignoriert)
OLLAMA_VISION_MODEL=qwen3-vl

# Optional: Separate Ollama-URL für das Vision-Modell
# Nützlich wenn Vision auf einer anderen GPU läuft als Chat
OLLAMA_VISION_URL=http://host.docker.internal:11434
```

**Defaults:**
- `OLLAMA_VISION_MODEL`: `""` (deaktiviert)
- `OLLAMA_VISION_URL`: `None` (verwendet Standard-OLLAMA_URL)

**Empfohlenes Modell:** `qwen3-vl` (~12 GB VRAM, passt auf 16 GB Karten, gutes Deutsch).

Siehe [SATELLITE_CAMERA.md](SATELLITE_CAMERA.md) für Setup und Modellvergleich.

---

### Sprache & Voice

```bash
# Standard-Sprache für STT/TTS
DEFAULT_LANGUAGE=de

# Unterstützte Sprachen (kommasepariert)
SUPPORTED_LANGUAGES=de,en

# Whisper STT Modell
WHISPER_MODEL=base

# Piper Multi-Voice Konfiguration (pro Sprache)
PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium

# Fallback-Stimme, wenn die angeforderte Sprache nicht in PIPER_VOICES enthalten ist
PIPER_DEFAULT_VOICE=de_DE-thorsten-high
```

**Defaults:**
- `DEFAULT_LANGUAGE`: `de`
- `SUPPORTED_LANGUAGES`: `de,en`
- `WHISPER_MODEL`: `base`
- `PIPER_VOICES`: `de:de_DE-thorsten-high,en:en_US-amy-medium`
- `PIPER_DEFAULT_VOICE`: `de_DE-thorsten-high` (Fallback, wenn die Sprache nicht in `PIPER_VOICES` ist)

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
AGENT_MAX_STEPS=12

# Timeout pro LLM-Call (Sekunden)
AGENT_STEP_TIMEOUT=30.0

# Gesamt-Timeout für gesamten Agent-Run (Sekunden)
AGENT_TOTAL_TIMEOUT=120.0

# Optionales separates Modell für Agent (Standard: OLLAMA_MODEL)
# AGENT_MODEL=qwen3:14b

# Optionale separate Ollama-Instanz für Agent
# AGENT_OLLAMA_URL=http://cuda.local:11434

# Konversations-Kontext im Agent Loop
AGENT_CONV_CONTEXT_MESSAGES=6

# Agent Router Timeout (Sekunden)
AGENT_ROUTER_TIMEOUT=30.0
```

**Defaults:**
- `AGENT_ENABLED`: `false` (Opt-in)
- `AGENT_MAX_STEPS`: `12`
- `AGENT_STEP_TIMEOUT`: `30.0`
- `AGENT_TOTAL_TIMEOUT`: `120.0`
- `AGENT_MODEL`: None (nutzt `OLLAMA_MODEL`)
- `AGENT_OLLAMA_URL`: None (nutzt `OLLAMA_URL`)
- `AGENT_CONV_CONTEXT_MESSAGES`: `6`
- `AGENT_ROUTER_TIMEOUT`: `30.0`

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

#### MCP Notification Polling

```bash
# Generic polling of MCP servers for proactive notifications (e.g. calendar reminders)
# Requires: MCP server with get_pending_notifications tool + notifications config in mcp_servers.yaml
NOTIFICATION_POLLER_ENABLED=false
NOTIFICATION_POLLER_STARTUP_DELAY=30     # Delay before first poll (seconds)
```

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

### Presence Detection

```bash
# Raum-Präsenzerkennung aus mehreren Quellen:
# 1. BLE-Scanning: Satelliten scannen nach bekannten BLE-Geräten (Telefone, Uhren) und melden RSSI-Werte
# 2. Voice Presence: Sprechererkennung auf Satelliten aktualisiert den Raum sofort (ohne Hysterese)
# 3. Web Auth Presence: Authentifizierte Web-Nutzer mit Raum-Kontext aktualisieren den Raum sofort
PRESENCE_ENABLED=false
PRESENCE_STALE_TIMEOUT=120               # Sekunden bis Benutzer als abwesend markiert
PRESENCE_HYSTERESIS_SCANS=2              # Aufeinanderfolgende Scans vor Raumwechsel
PRESENCE_RSSI_THRESHOLD=-80              # dBm, schwächere Signale werden für Raumzuweisung ignoriert
PRESENCE_HOUSEHOLD_ROLES="Admin,Familie" # Rollen die als Haushaltsmitglieder gelten (für Privacy-TTS)

# Presence Webhooks (Automation-Hooks)
PRESENCE_WEBHOOK_URL=""                  # URL für Presence-Events (leer = deaktiviert). Unterstützt n8n Webhook-Trigger
PRESENCE_WEBHOOK_SECRET=""               # Shared Secret als X-Webhook-Secret Header für Webhook-Authentifizierung
```

**Satellite-Konfiguration** (in `satellite.yaml`):
```yaml
ble:
  enabled: true
  scan_interval: 30        # Sekunden zwischen Scans
  scan_duration: 5         # Sekunden pro Scan
  rssi_threshold: -80      # Schwächere Signale ignorieren
```

**Endpunkte:**
- `GET /api/presence/rooms` — Alle Räume mit Anwesenden
- `GET /api/presence/room/{id}` — Anwesende in einem Raum
- `GET /api/presence/user/{id}` — Standort + allein?
- `GET /api/presence/devices` — Registrierte BLE-Geräte (Admin)
- `POST /api/presence/devices` — BLE-Gerät registrieren (Admin)
- `DELETE /api/presence/devices/{id}` — BLE-Gerät entfernen (Admin)

---

### Media Follow Me

```bash
# Playback folgt dem User zwischen Räumen (erfordert PRESENCE_ENABLED=true)
MEDIA_FOLLOW_ENABLED=false
MEDIA_FOLLOW_SUSPEND_TIMEOUT=600.0       # Sekunden bis suspendierte Session verfällt
MEDIA_FOLLOW_RESUME_DELAY=2.0            # Verzögerung vor Resume im neuen Raum (Sekunden)
```

**Funktionsweise:** Wenn ein User Radio im Arbeitszimmer abspielt und ins Wohnzimmer geht, stoppt die Musik im Arbeitszimmer und wird im Wohnzimmer fortgesetzt. Bei Konflikten (anderer User spielt bereits): Room-Owner > Rollen-Priorität (Admin > Familie > Gast) > First-Come.

**Per-User Opt-out:** Jeder User hat ein `media_follow_enabled` Flag (default: true). Kann in der Admin-UI deaktiviert werden.

**Room Owner:** `PATCH /api/rooms/{id}/owner` setzt den Raum-Besitzer (für Konflikt-Priorisierung).

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
# Hostname/IP die externe Dienste (HA Media Player, DLNA Renderer) erreichen können
ADVERTISE_HOST=192.168.1.159

# Port für ADVERTISE_HOST (Default: 8000, setze 80 wenn über Nginx)
ADVERTISE_PORT=80
```

**Defaults:**
- `ADVERTISE_HOST`: None (muss gesetzt werden für HA Media Player / DLNA Output)
- `ADVERTISE_PORT`: `8000`

**Wann benötigt:**
- Wenn TTS-Ausgabe auf Home Assistant Media Playern oder DLNA Renderern erfolgen soll
- Der Wert muss eine Adresse sein, die Home Assistant erreichen kann (nicht `localhost`!)

**Beispiele:**
```bash
ADVERTISE_HOST=192.168.1.159      # IP-Adresse (empfohlen für DLNA)
ADVERTISE_HOST=renfield.local     # mDNS Hostname (funktioniert NICHT für DLNA Renderer)
```

**Wichtig:** DLNA-Renderer (z.B. HiFiBerry) können mDNS-Hostnamen (`.local`) oft
nicht auflösen. **IP-Adresse verwenden** wenn DLNA-Ausgabe genutzt wird.

**Port 80 vs 8000:** Der Backend-Container exposed Port 8000 nur auf `127.0.0.1`.
Für externe Zugriffe (DLNA, HA) muss der Traffic über Nginx (Port 80) laufen.
Setze `ADVERTISE_PORT=80` in Produktion. Nginx leitet `/api/voice/tts-cache/`
über plain HTTP (ohne HTTPS-Redirect) an den Backend weiter.

**Ohne ADVERTISE_HOST:**
- TTS wird nur auf Renfield-Geräten (Satellites, Web Panels) abgespielt
- HA Media Player und DLNA Renderer können keine TTS-Dateien abrufen

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

### Trusted Proxies

```bash
# Vertrauenswürdige Reverse-Proxy CIDRs (kommasepariert)
TRUSTED_PROXIES=172.18.0.0/16,127.0.0.1
```

**Default:** `""` (leer = alle Proxies vertraut, rückwärtskompatibel)

**Wann setzen:** Hinter einem Reverse Proxy (nginx, Traefik), damit Rate Limiting die echte Client-IP nutzt statt der Proxy-IP. Nur wenn `TRUSTED_PROXIES` konfiguriert ist, werden `X-Forwarded-For` / `X-Real-IP` Header gelesen.

### REST API Rate Limiting

```bash
# Rate Limiting aktivieren
API_RATE_LIMIT_ENABLED=true

# Limits pro Endpoint-Gruppe
API_RATE_LIMIT_DEFAULT=100/minute
API_RATE_LIMIT_AUTH=10/minute
API_RATE_LIMIT_VOICE=30/minute
API_RATE_LIMIT_CHAT=60/minute
API_RATE_LIMIT_ADMIN=200/minute
```

### Circuit Breaker

```bash
# Aufeinanderfolgende Fehler bis Circuit öffnet
CB_FAILURE_THRESHOLD=3

# Recovery-Timeouts (Sekunden)
CB_LLM_RECOVERY_TIMEOUT=30.0
CB_AGENT_RECOVERY_TIMEOUT=60.0
```

**States:** `CLOSED` (normal) → `OPEN` (reject fast) → `HALF_OPEN` (testing recovery)

### Embeddings

```bash
# Embedding-Vektor-Dimension (muss zum Modell passen)
EMBEDDING_DIMENSION=768
```

**Default:** `768` (passend für `nomic-embed-text` und `qwen3-embedding:4b`)

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
# n8n Base URL (für MCP-Server)
N8N_BASE_URL=http://192.168.1.78:5678

# n8n API Key (für MCP stdio-Server)
N8N_API_KEY=your_n8n_api_key

# n8n MCP aktivieren
N8N_MCP_ENABLED=true
```

**Erforderlich:** Optional
**Hinweis:** n8n wird über einen MCP stdio-Server angebunden (`npx @anthropic/n8n-mcp`). `N8N_BASE_URL` und `N8N_API_KEY` werden als Umgebungsvariablen an den Subprocess übergeben.

---

### Frigate

```bash
# Frigate URL
FRIGATE_URL=http://frigate.local:5000
```

**Erforderlich:** Optional
**Format:** `http://<frigate-host>:<port>`

---

## Knowledge Graph

Das Knowledge Graph-System extrahiert Entitäten und Relationen aus Konversationen und Dokumenten.

### System-Kontrolle

```bash
# Knowledge Graph aktivieren
KNOWLEDGE_GRAPH_ENABLED=false
```

**Default:** `false`

### Konfiguration

```bash
# Modell für KG-Extraktion (leer = Standard-Modell verwenden)
KG_EXTRACTION_MODEL=

# Schwellenwert für Entity-Deduplizierung (Embedding-Ähnlichkeit, 0.85 mergt OCR-Varianten)
KG_SIMILARITY_THRESHOLD=0.85

# Schwellenwert für Kontext-Retrieval (Embedding-Ähnlichkeit)
KG_RETRIEVAL_THRESHOLD=0.70

# Max. persönliche Entitäten pro Benutzer (custom scopes zählen nicht)
KG_MAX_ENTITIES_PER_USER=5000

# Max. Triples im LLM-Kontext
KG_MAX_CONTEXT_TRIPLES=15
```

### Entity-Scoping

Entitäten können verschiedene Sichtbarkeits-Scopes haben:

- **`personal`** (built-in): Nur für den Besitzer sichtbar (Standard)
- **Custom Scopes**: Definiert in `config/kg_scopes.yaml` mit rollenbasierter Zugriffskontrolle
  - Beispiele: `family` (sichtbar für Familie-Rolle), `public` (für alle sichtbar)
  - Jeder Scope definiert, welche Rollen darauf zugreifen können
  - Erweiterbar: Neue Scopes können per YAML hinzugefügt werden ohne Code-Änderungen

**Entity-Auflösung:** Custom Scopes werden vor Erstellung neuer persönlicher Entitäten geprüft → verhindert Duplikate.

**Limit-Verhalten:** Nur `personal` Entitäten zählen zum `KG_MAX_ENTITIES_PER_USER` Limit. Family/Public Entitäten verbrauchen kein Benutzer-Kontingent.

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

# Radio (TuneIn)
RADIO_ENABLED=true
TUNEIN_PARTNER_ID=                     # Optional: TuneIn Partner ID für höhere Rate Limits

# DLNA (Media Renderer Control)
DLNA_MCP_ENABLED=true

# n8n (Workflow Automation)
N8N_MCP_ENABLED=true

# Home Assistant (Smart Home)
HA_MCP_ENABLED=true

# Paperless-NGX (Dokumentenverwaltung)
PAPERLESS_ENABLED=true

# Paperless Document Audit (LLM-basierte Metadaten-Prüfung)
PAPERLESS_AUDIT_ENABLED=false          # Opt-in: Dokument-Audit aktivieren
PAPERLESS_AUDIT_MODEL=                 # Leer = Default-Model
PAPERLESS_AUDIT_SCHEDULE=02:00         # Tägliche Audit-Zeit
PAPERLESS_AUDIT_FIX_MODE=review        # review | auto_threshold | auto_all
PAPERLESS_AUDIT_CONFIDENCE_THRESHOLD=0.9
PAPERLESS_AUDIT_OCR_THRESHOLD=2        # OCR-Qualität ≤ 2 → Re-OCR vorschlagen
PAPERLESS_AUDIT_BATCH_DELAY=2.0        # Sekunden zwischen Dokumenten

# Email (IMAP/SMTP)
EMAIL_MCP_ENABLED=true

# Calendar (Google Calendar via n8n)
CALENDAR_ENABLED=true
```

**Defaults:** Alle `false`

### MCP-Server Secrets (Produktion: Docker Secrets)

| Variable | Beschreibung | Docker Secret |
|----------|-------------|---------------|
| `OPENWEATHER_API_KEY` | OpenWeatherMap API Key | `secrets/openweather_api_key` |
| `NEWSAPI_KEY` | NewsAPI Key | `secrets/newsapi_key` |
| `JELLYFIN_TOKEN` | Jellyfin API Token | `secrets/jellyfin_token` |
| `JELLYFIN_BASE_URL` | Jellyfin Server URL | `secrets/jellyfin_base_url` |
| `JELLYFIN_USER_ID` | Jellyfin User-GUID | `secrets/jellyfin_user_id` |
| `N8N_API_KEY` | n8n API Key | `secrets/n8n_api_key` |
| `HOME_ASSISTANT_TOKEN` | HA Long-Lived Access Token | `secrets/home_assistant_token` |
| `PAPERLESS_API_TOKEN` | Paperless-NGX API Token | `secrets/paperless_api_token` |
| `MAIL_PRIMARY_PASSWORD` | Email IMAP/SMTP Passwort (primary mail account from `mail_accounts.yaml`) | `secrets/mail_primary_password` |
| `PRESENCE_WEBHOOK_SECRET` | Shared-Secret für `X-Webhook-Secret` Header bei ausgehenden Presence-Webhooks | `secrets/presence_webhook_secret` |

> Die kanonische Liste inkl. Consumer-Mapping und Upgrade-Hinweise liegt in [`docs/SECRETS_MANAGEMENT.md`](SECRETS_MANAGEMENT.md). Optionale Integration-Secrets (alles ausser den drei Core-Secrets) dürfen als leere Placeholder-Datei existieren — der Stack bleibt startfähig, das Feature deaktiviert sich einfach.

### MCP-Server URLs (nicht-sensitiv, in .env)

```bash
# Home Assistant URL
HOME_ASSISTANT_URL=http://homeassistant.local:8123

# DLNA MCP Server URL (läuft als Host-Service, nicht im Docker)
# Default: http://host.docker.internal:9091/mcp
DLNA_MCP_URL=http://host.docker.internal:9091/mcp

# n8n Base URL
N8N_BASE_URL=http://192.168.1.78:5678

# SearXNG URL
SEARXNG_API_URL=http://cuda.local:3002

# Paperless-NGX URL
PAPERLESS_API_URL=http://paperless.local:8000

# Calendar (Unified Calendar MCP Server — EWS, Google, CalDAV)
# Config via config/calendar_accounts.yaml
# CALENDAR_CONFIG=/config/calendar_accounts.yaml
# CALENDAR_WORK_USERNAME=user@example.com
# CALENDAR_WORK_PASSWORD=secret
# CALENDAR_VEREIN_USERNAME=user
# CALENDAR_VEREIN_PASSWORD=secret
```

**Hinweis:** In Produktion werden Secrets über Docker Compose File-Based Secrets bereitgestellt und von `mcp_client.py` automatisch in `os.environ` injiziert. Siehe `docs/SECRETS_MANAGEMENT.md`.

---

## Evolution API (WhatsApp)

Self-hosted WhatsApp API via [Evolution API](https://github.com/EvolutionAPI/evolution-api). Laeuft als Docker-Service mit Profile `whatsapp`.

```bash
# Evolution API Auth Key (starker zufaelliger Wert)
EVOLUTION_API_KEY=changeme

# Docker-interne URL (n8n → Evolution API)
EVOLUTION_API_URL=http://evolution-api:8080
```

**Defaults:**
- `EVOLUTION_API_KEY`: `changeme` (MUSS in Produktion geaendert werden!)
- `EVOLUTION_API_URL`: `http://evolution-api:8080`

**Setup:**
1. `CREATE DATABASE evolution OWNER renfield;` in PostgreSQL
2. `docker compose --profile whatsapp up -d evolution-api`
3. WhatsApp-Instanz erstellen + QR-Code scannen
4. Test-Nachricht senden zur Verifikation

**Infrastruktur:**
- Nutzt bestehende PostgreSQL (separate DB `evolution`) und Redis (Index 3)
- Nur lokal erreichbar (127.0.0.1:8080), n8n greift via Docker-Netzwerk zu
- Volume `evolution_instances` fuer WhatsApp-Session-Daten

---

## Hook / Extension System

Das Hook-System ermöglicht externen Paketen (z.B. `renfield-twin`) sich an definierten Lifecycle-Stellen einzuhängen, ohne dass renfield eine Abhängigkeit zum Plugin hat.

```bash
# Entry-Point für Hook-basierte Extensions
# Format: "package.module:callable" — wird beim Startup aufgerufen
# Leer = deaktiviert (Standard)
PLUGIN_MODULE=

# Beispiel: renfield-twin Extension
PLUGIN_MODULE=renfield_twin.hooks:register
```

**Defaults:**
- `PLUGIN_MODULE`: `""` (deaktiviert)

**Hook Events:** `startup`, `shutdown`, `register_routes`, `register_tools`, `post_message`, `retrieve_context`

**Hinweis:** Das Hook-System ist der empfohlene Weg für tiefe Integrationen (Kontext-Injektion, Post-Processing, Custom Routes). Für einfache Tool-Integrationen sind MCP-Server weiterhin der bevorzugte Weg.

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

## Troubleshooting

### Variable wird nicht geladen

**Problem:** Service findet Konfiguration nicht

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
# Ollama LLM (Multi-Modell)
# -----------------------------------------------------------------------------
OLLAMA_URL=http://cuda.local:11434
OLLAMA_MODEL=qwen3:14b
# OLLAMA_CHAT_MODEL=qwen3:14b
# OLLAMA_RAG_MODEL=qwen3:14b
# OLLAMA_EMBED_MODEL=nomic-embed-text
# OLLAMA_INTENT_MODEL=qwen3:8b
# OLLAMA_NUM_CTX=32768

# -----------------------------------------------------------------------------
# Sprache & Voice
# -----------------------------------------------------------------------------
DEFAULT_LANGUAGE=de
SUPPORTED_LANGUAGES=de,en
WHISPER_MODEL=base
PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium
PIPER_DEFAULT_VOICE=de_DE-thorsten-high  # Fallback for languages not in PIPER_VOICES

# -----------------------------------------------------------------------------
# Integrationen
# -----------------------------------------------------------------------------
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=eyJhbGci...

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
# AGENT_MAX_STEPS=12
# AGENT_STEP_TIMEOUT=30.0
# AGENT_TOTAL_TIMEOUT=120.0
# AGENT_MODEL=                       # Optional: eigenes Modell für Agent
# AGENT_OLLAMA_URL=                  # Optional: separate Ollama-Instanz

# -----------------------------------------------------------------------------
# Satellite System
# -----------------------------------------------------------------------------
WAKE_WORD_DEFAULT=alexa
WAKE_WORD_THRESHOLD=0.5

# -----------------------------------------------------------------------------
# Audio Output Routing
# -----------------------------------------------------------------------------
# Hostname/IP die externe Dienste (z.B. HA) erreichen können
ADVERTISE_HOST=192.168.1.159
ADVERTISE_PORT=80

# -----------------------------------------------------------------------------
# MCP Server
# -----------------------------------------------------------------------------
MCP_ENABLED=true
WEATHER_ENABLED=true
SEARCH_ENABLED=true
NEWS_ENABLED=true
JELLYFIN_ENABLED=true
RADIO_ENABLED=true
DLNA_MCP_ENABLED=true
N8N_MCP_ENABLED=true
HA_MCP_ENABLED=true
PAPERLESS_ENABLED=true
EMAIL_MCP_ENABLED=true

# MCP-Server URLs (nicht-sensitiv)
# DLNA_MCP_URL=http://host.docker.internal:9091/mcp  # Default
N8N_BASE_URL=http://192.168.1.78:5678
SEARXNG_API_URL=http://cuda.local:3002
PAPERLESS_API_URL=http://paperless.local:8000

# MCP-Server Secrets: In Produktion als Docker Secrets!
# OPENWEATHER_API_KEY=...     → secrets/openweather_api_key
# NEWSAPI_KEY=...             → secrets/newsapi_key
# JELLYFIN_TOKEN=...          → secrets/jellyfin_token
# JELLYFIN_BASE_URL=...       → secrets/jellyfin_base_url
# JELLYFIN_USER_ID=...        → secrets/jellyfin_user_id
# N8N_API_KEY=...             → secrets/n8n_api_key
# PAPERLESS_API_TOKEN=...     → secrets/paperless_api_token
# MAIL_PRIMARY_PASSWORD=...   → secrets/mail_primary_password
# PRESENCE_WEBHOOK_SECRET=... → secrets/presence_webhook_secret  (auto-gen via generate-secrets.sh)

```

---

**Hinweis:** Passe die Werte an deine Umgebung an und committe NIE echte Secrets ins Repository!
