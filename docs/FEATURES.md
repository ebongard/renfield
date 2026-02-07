# Renfield - Feature Dokumentation

## Übersicht

Renfield ist ein vollständig offline-fähiger, selbst-gehosteter **digitaler Assistent** — ein persönlicher AI Hub, der Wissen, Informationsabfragen und Multi-Channel-Steuerung in einer Oberfläche bündelt. Er dient mehreren Nutzern parallel im Haushalt. Kernfähigkeiten: abfragbare Wissensbasis (RAG), gebündelte Tool-Nutzung über 8 MCP-Server, Konversations-Gedächtnis, proaktive Benachrichtigungen und Smart-Home-Steuerung.

## Chat & Konversation

### Natural Language Understanding
- **Intent Recognition**: LLM-basierte Erkennung von Benutzerabsichten mit Ranked Intents (1-3 gewichtete Intents mit Fallback-Chain)
- **Dynamische Keywords**: Geräte- und Entity-Namen aus Home Assistant werden automatisch in den Intent-Prompt injiziert
- **MCP Tool Prompt Filtering**: `prompt_tools` in `mcp_servers.yaml` beschränkt den Intent-Prompt auf ~20 relevante Tools (alle bleiben ausführbar)

### Streaming Responses
- **WebSocket-basiert**: Echtzeit-Antworten mit Token-für-Token Streaming
- **Session-Persistenz**: `session_id` im WebSocket für Konversations-Kontext
- **Fallback auf HTTP**: REST API (`POST /api/chat/send`) als Alternative

### Chat-Historie
- **Session Management**: Getrennte Gespräche mit Datumsgruppierung (Heute, Gestern, Letzte 7 Tage, Älter)
- **Persistente Speicherung**: Alle Nachrichten in PostgreSQL
- **Follow-up Kontext**: LLM erhält Konversationshistorie; versteht "Mach es aus" oder "Und dort?" ohne explizite Referenzen
- **Volltext-Suche**: Durchsuche frühere Konversationen
- **Satellite-Sessions**: Tägliche Sessions für Voice-Commands

## Sprach-Interface

### Speech-to-Text (STT)
- **Whisper Integration**: OpenAI's Whisper für Offline-Transkription
- **Modell-Auswahl**: tiny, base, small, medium, large
- **Audio-Preprocessing**: Rauschunterdrückung und Normalisierung (opt-in)
- **GPU-Beschleunigung**: Optional mit NVIDIA GPU

### Text-to-Speech (TTS)
- **Piper Integration**: Natürlich klingende Stimmen, lokal generiert
- **Mehrsprachig**: Separate Stimmen pro Sprache (z.B. `de:de_DE-thorsten-high,en:en_US-amy-medium`)

### Voice Chat
- **End-to-End Voice**: Sprechen → Transkription → Verarbeitung → Antwort → Vorlesen
- **Sprechererkennung**: Automatische Identifikation mit SpeechBrain ECAPA-TDNN (192-dim Embeddings, Cosine Similarity)
- **Auto-Discovery**: Unbekannte Sprecher werden automatisch als Profile angelegt
- **Continuous Learning**: Verbesserte Erkennung durch jede Interaktion

Siehe [SPEAKER_RECOGNITION.md](SPEAKER_RECOGNITION.md) für Details.

## Konversations-Gedächtnis (Langzeit)

Renfield kann sich Dinge über Nutzer langfristig merken — Präferenzen, Fakten und Anweisungen werden als semantische Embeddings gespeichert und bei relevanten zukünftigen Gesprächen automatisch eingeblendet.

### Memory-Kategorien

| Kategorie | Beschreibung | Beispiel |
|-----------|-------------|---------|
| `preference` | Vorlieben und Stil | "Ich bevorzuge kurze Antworten" |
| `fact` | Gelernte Fakten | "Meine Katze heißt Luna" |
| `instruction` | Benutzerdefinierte Regeln | "Antworte immer auf Deutsch" |
| `correction` | Korrigierte Aussagen | Via Widerspruchserkennung |

### Funktionsweise
1. **Extraktion** — Memories werden automatisch aus Konversationen extrahiert (opt-in: `MEMORY_EXTRACTION_ENABLED`)
2. **Speicherung** — 768-dim Embeddings via pgvector mit semantischer Deduplizierung (Threshold: 0.9)
3. **Retrieval** — Bei neuen Nachrichten werden semantisch ähnliche Memories abgerufen (Cosine Similarity ≥ 0.7, max 3)
4. **Context Injection** — Relevante Memories werden in den LLM-Prompt eingefügt
5. **Decay** — Context-Kategorie Memories verfallen nach konfigurierbarer Zeit (default: 30 Tage)

### Widerspruchserkennung

Opt-in Feature (`MEMORY_CONTRADICTION_RESOLUTION=true`): Beim Speichern neuer Memories werden bestehende auf semantische Ähnlichkeit geprüft. Memories im Threshold-Bereich (0.6–0.89) werden dem LLM zur Widerspruchsprüfung vorgelegt. Bei Widerspruch wird die alte Memory aktualisiert oder archiviert.

### Audit Trail

Jede Änderung an Memories wird in der History dokumentiert:
- **Aktionen**: `created`, `updated`, `deleted`
- **Quellen**: `system`, `user`, `contradiction_resolution`
- **API**: `GET /api/memory/history/{id}` liefert die vollständige Änderungshistorie

### Konfiguration

```env
MEMORY_ENABLED=false                    # Master-Switch (opt-in)
MEMORY_EXTRACTION_ENABLED=false         # Auto-Extraktion aus Konversationen
MEMORY_RETRIEVAL_LIMIT=3                # Max Memories pro Query
MEMORY_RETRIEVAL_THRESHOLD=0.7          # Cosine-Similarity Schwellwert
MEMORY_MAX_PER_USER=500                 # Max aktive Memories pro Nutzer
MEMORY_CONTEXT_DECAY_DAYS=30            # Verfall für Context-Kategorie
MEMORY_DEDUP_THRESHOLD=0.9              # Deduplizierungs-Schwellwert
MEMORY_CONTRADICTION_RESOLUTION=false   # LLM-basierte Widerspruchserkennung
MEMORY_CONTRADICTION_THRESHOLD=0.6      # Untere Grenze für Widerspruchs-Check
```

## Agent System (ReAct)

### Übersicht

Komplexe Anfragen werden automatisch erkannt und über einen ReAct-Loop (Reason + Act) bearbeitet. Der Agent Router klassifiziert jede Nachricht in eine spezialisierte Rolle, die bestimmt welche Tools und Modelle verwendet werden.

### Agent Router

Jede Nachricht wird vom Router in genau eine Rolle klassifiziert:

| Rolle | MCP-Server | Max Steps | Beschreibung |
|-------|-----------|-----------|--------------|
| `smart_home` | homeassistant | 4 | Licht, Schalter, Sensoren, Klima |
| `research` | search, news, weather | 6 | Web-Suche, Nachrichten, Wetter |
| `documents` | paperless, email | 8 | Dokument-Suche, E-Mail |
| `media` | jellyfin | 6 | Musik, Filme, Serien |
| `workflow` | n8n | 10 | Workflow-Automation |
| `knowledge` | *(RAG-Pfad)* | — | Wissensbasis-Suche (kein Agent Loop) |
| `general` | alle Server | 12 | Komplexe domänenübergreifende Anfragen |
| `conversation` | *(kein Agent)* | — | Smalltalk, allgemeines Wissen |

Rollen werden in `config/agent_roles.yaml` definiert. Pro Rolle sind separate Modelle und Ollama-URLs konfigurierbar.

### Complexity Detection

Der `ComplexityDetector` erkennt per Regex, ob eine Nachricht den Agent Loop benötigt (Zero-Cost, kein LLM-Call):

| Muster | Erkennt | Beispiel |
|--------|---------|---------|
| Bedingung | Wenn-Dann-Konstrukte | "Wenn es regnet, schließe die Fenster" |
| Sequenz | Aufeinanderfolgende Aktionen | "Hole Wetter und dann suche ein Restaurant" |
| Vergleich | Schwellwert-Vergleiche | "Wärmer als 20 Grad" |
| Multi-Aktion | Zwei Aktionsverben mit "und" | "Schalte das Licht ein und stelle die Heizung ein" |
| Kombiniert | Zwei Fragewörter mit "und" | "Wie ist das Wetter und was gibt es Neues?" |

Nachrichten unter 10 Zeichen werden immer als einfach eingestuft. Alle Muster unterstützen Deutsch und Englisch.

### ReAct Loop

```
User → ComplexityDetector → einfach? → Single-Intent (schneller Pfad)
                          → komplex? → Agent Router → Rolle auswählen
                                        → ReAct Loop:
                                          ├─ LLM: Plan → Tool Call 1
                                          ├─ Tool Result → zurück zum LLM
                                          ├─ LLM: Reasoning → Tool Call 2
                                          └─ LLM: Final Answer → Stream
```

### WebSocket Messages (Agent Loop)

| Type | Beschreibung |
|------|-------------|
| `agent_thinking` | Agent analysiert die Anfrage |
| `agent_tool_call` | Tool-Name, Parameter, Begründung |
| `agent_tool_result` | Ergebnis (Erfolg/Fehler, Daten) |
| `stream` | Finale Antwort (Token-für-Token) |
| `done` | Abschluss mit `agent_steps` Count |

### Konfiguration

```env
AGENT_ENABLED=false               # Master-Switch (opt-in)
AGENT_MAX_STEPS=12                # Max Reasoning-Schritte
AGENT_STEP_TIMEOUT=30.0           # Per-Step LLM Timeout (Sekunden)
AGENT_TOTAL_TIMEOUT=120.0         # Gesamt-Timeout
AGENT_MODEL=                      # Optional: separates Modell
AGENT_OLLAMA_URL=                 # Optional: separate Ollama-Instanz
AGENT_CONV_CONTEXT_MESSAGES=6     # Konversations-Kontext im Agent Loop
AGENT_ROUTER_TIMEOUT=30.0         # Router-Klassifikation Timeout
```

## Intent Feedback Learning

Renfield lernt aus Nutzer-Korrekturen und verbessert die Intent-Erkennung über semantisches Matching.

### Korrektur-Typen

| Typ | Beschreibung | Beispiel |
|-----|-------------|---------|
| `intent` | Falsche Intent-Klassifikation | "Das war kein Wetter-Intent, sondern Smart Home" |
| `agent_tool` | Falsches Tool im Agent Loop | "Falsches Tool gewählt" |
| `complexity` | Falsche Einfach/Komplex-Einstufung | "Das hätte der Agent machen sollen" |

### Funktionsweise

1. **Korrektur speichern** — Nutzer gibt Feedback über `POST /api/feedback/correction` oder den UI-Button
2. **Embedding erstellen** — Die ursprüngliche Nachricht wird als 768-dim Vektor gespeichert
3. **Ähnlichkeitssuche** — Bei zukünftigen Nachrichten werden semantisch ähnliche Korrekturen abgerufen (Cosine Similarity ≥ 0.75)
4. **Few-Shot Injection** — Gefundene Korrekturen werden als Beispiele in den Intent-Prompt injiziert

### Konfiguration

```env
INTENT_FEEDBACK_CACHE_TTL=300     # Cache-TTL für Korrektur-Counts (Sekunden)
```

## MCP Integration (Model Context Protocol)

### Übersicht

Alle externen Integrationen laufen als MCP-Server. Tools werden automatisch als `mcp.<server>.<tool>` Intents registriert — keine Code-Änderung nötig.

### Verfügbare Server

| Server | Transport | Beschreibung | Tools |
|--------|-----------|-------------|-------|
| **weather** | stdio (Python) | OpenWeatherMap | 17 (Vorhersage, Standort) |
| **search** | stdio (npx) | SearXNG Metasearch | 1 |
| **news** | stdio (npx) | NewsAPI | 2 (Suche, Top Headlines) |
| **jellyfin** | stdio (Python) | Media Server | 13 (Musik, Filme, Serien) |
| **n8n** | stdio (npx) | Workflow Automation | 12 (Workflow CRUD, Templates) |
| **paperless** | stdio (Python) | Dokumenten-Management | 1+ |
| **email** | stdio (Python) | Multi-Account IMAP/SMTP | 4 (List, Search, Read, Send) |
| **homeassistant** | streamable_http | Smart Home | 5+ (Steuerung, Status) |

### Konfiguration

Server werden in `config/mcp_servers.yaml` definiert:

```yaml
servers:
  - name: weather
    command: ["python3", "-m", "renfield_mcp_weather"]
    transport: stdio
    enabled: "${WEATHER_ENABLED:-false}"
    refresh_interval: 300
    prompt_tools:
      - get_weather
    examples:
      de: ["Wie wird das Wetter morgen?"]
      en: ["What's the weather forecast?"]
```

**YAML-Felder:**

| Feld | Pflicht | Beschreibung |
|------|---------|-------------|
| `name` | Ja | Server-ID, genutzt als `mcp.<name>.<tool>` |
| `transport` | Ja | `streamable_http`, `sse` oder `stdio` |
| `enabled` | Ja | Env-Var Toggle (z.B. `"${WEATHER_ENABLED:-false}"`) |
| `prompt_tools` | Nein | Tool-Namen für den LLM-Intent-Prompt (alle bleiben ausführbar) |
| `examples` | Nein | Bilinguale Beispiel-Queries für den LLM-Prompt |

### Features

- **Eager Connection**: Verbindung beim Startup, nicht pro Request
- **Background Refresh**: Automatischer Health-Check und Tool-Refresh (konfigurierbar)
- **Partial Failure**: Ein fehlender Server blockiert nicht die anderen
- **Env-Var Substitution**: `${VAR}` und `${VAR:-default}` in der YAML-Konfiguration
- **Input-Validierung**: MCP-Antworten werden auf Größe begrenzt (`MCP_MAX_RESPONSE_SIZE`, default: 10KB)
- **Rate Limiting**: MCP-Tool-Calls unterliegen dem REST API Rate Limiting

### Admin-Endpoints

- `GET /api/mcp/status` — Server-Verbindungen, Tool-Counts, Fehler
- `GET /api/mcp/tools` — Alle entdeckten MCP-Tools mit Schemas
- `POST /api/mcp/refresh` — Tool-Listen manuell refreshen

### Konfiguration

```env
MCP_ENABLED=false                 # Master-Switch
MCP_CONFIG_PATH=config/mcp_servers.yaml
MCP_REFRESH_INTERVAL=60           # Background-Refresh (Sekunden)
MCP_CONNECT_TIMEOUT=10.0          # Verbindungs-Timeout
MCP_CALL_TIMEOUT=30.0             # Tool-Call-Timeout
MCP_MAX_RESPONSE_SIZE=10240       # Max Response-Größe (Bytes)
```

## Proaktive Benachrichtigungen & Erinnerungen

### Übersicht

Externe Systeme (Home Assistant, n8n) senden Events per Webhook an Renfield. Nutzer sehen Benachrichtigungen im Frontend und können sie per Sprache oder UI verarbeiten.

### Webhook-Integration

```
Home Assistant Automation → POST /api/notifications/webhook
                            (Bearer Token Auth)
                            → Renfield verarbeitet, dedupliziert, enriched
                            → Frontend zeigt Benachrichtigung
```

### Features

- **Webhook-Empfang**: `POST /api/notifications/webhook` mit Bearer Token Authentifizierung
- **Semantische Deduplizierung**: Ähnliche Benachrichtigungen innerhalb eines Zeitfensters werden zusammengefasst (pgvector, opt-in)
- **Urgency-Klassifikation**: Automatische Dringlichkeitseinstufung (opt-in)
- **LLM-Enrichment**: Benachrichtigungen werden durch LLM-Kontext angereichert (opt-in)
- **Suppressions**: Nutzer können bestimmte Benachrichtigungs-Typen unterdrücken (semantisch)
- **Feedback Learning**: System lernt aus Nutzer-Interaktionen mit Benachrichtigungen (opt-in)

### Erinnerungen

- **Zeitgesteuert**: Reminder mit Fälligkeitsdatum
- **API**: `POST /api/notifications/reminders` zum Erstellen, `GET /api/notifications/reminders` zum Auflisten
- **Hintergrund-Prüfung**: Fällige Erinnerungen werden automatisch als Benachrichtigungen ausgelöst

### Konfiguration

```env
PROACTIVE_ENABLED=false                     # Master-Switch (opt-in)
PROACTIVE_SUPPRESSION_WINDOW=60             # Dedup-Fenster (Sekunden)
PROACTIVE_TTS_DEFAULT=true                  # TTS standardmäßig aktiv
PROACTIVE_NOTIFICATION_TTL=86400            # Ablauf (24h)
PROACTIVE_SEMANTIC_DEDUP_ENABLED=false      # Semantische Deduplizierung
PROACTIVE_URGENCY_AUTO_ENABLED=false        # Auto-Urgency
PROACTIVE_ENRICHMENT_ENABLED=false          # LLM-Enrichment
PROACTIVE_REMINDERS_ENABLED=false           # Erinnerungen
PROACTIVE_REMINDER_CHECK_INTERVAL=15        # Prüf-Intervall (Sekunden)
```

## Wissensspeicher (RAG)

### Übersicht

Renfield verarbeitet Dokumente und nutzt sie als Wissensbasis für kontextbasierte Antworten. Hybrid Search kombiniert semantische Vektor-Suche mit BM25 Full-Text-Search.

### Unterstützte Formate
PDF, DOCX, PPTX, XLSX, HTML, Markdown, TXT — verarbeitet mit IBM Docling.

### Pipeline

1. **Upload** → Automatische Verarbeitung und Duplikat-Erkennung (SHA256)
2. **Chunking** → Semantische Textaufteilung (konfigurierbare Chunk-Größe und Overlap)
3. **Embedding** → Jeder Chunk wird mit dem konfigurierten Modell vektorisiert (768-dim default)
4. **Hybrid Search** → Dense Embeddings (pgvector) + BM25 (PostgreSQL tsvector), kombiniert via Reciprocal Rank Fusion (RRF)
5. **Context Window** → Benachbarte Chunks werden automatisch zum Treffer hinzugefügt (±1 default)

### Features

- **Knowledge Bases** — Organisiere Dokumente in thematischen Sammlungen
- **KB-Sharing** — Teile Wissensdatenbanken mit anderen Nutzern (RPBAC)
- **Follow-up-Fragen** — RAG-Kontext bleibt für Nachfragen erhalten
- **Quellen-Zitation** — Antworten verweisen auf Quelldokumente
- **Re-Embedding** — `POST /admin/reembed` nach Modellwechsel

### Konfiguration

```env
RAG_ENABLED=true
RAG_CHUNK_SIZE=512                # Chunk-Größe (64-4096)
RAG_CHUNK_OVERLAP=50              # Overlap zwischen Chunks
RAG_TOP_K=5                       # Max Ergebnisse
RAG_SIMILARITY_THRESHOLD=0.4      # Mindest-Ähnlichkeit

# Hybrid Search
RAG_HYBRID_ENABLED=true           # Dense + BM25
RAG_HYBRID_BM25_WEIGHT=0.3
RAG_HYBRID_DENSE_WEIGHT=0.7
RAG_HYBRID_FTS_CONFIG=simple      # simple/german/english

# Context Window
RAG_CONTEXT_WINDOW=1              # Benachbarte Chunks (0=deaktiviert)
```

## Multi-Room Device System

### Unterstützte Gerätetypen

| Typ | Beschreibung | Verbindung |
|-----|-------------|------------|
| Satellite | Raspberry Pi Hardware | `/ws/satellite` |
| Web Panel | Stationäre Wand-Tablets | `/ws/device` |
| Web Tablet | Mobile Tablets | `/ws/device` |
| Web Browser | Desktop/Mobile Browser | `/ws/device` |
| Web Kiosk | Kiosk-Terminals | `/ws/device` |

### Raspberry Pi Satellites
- **Pi Zero 2 W** — Kostengünstige (~63€) Satellite-Einheiten
- **ReSpeaker 2-Mics HAT** — Mikrofonerfassung mit 3m Reichweite
- **Lokale Wake-Word-Erkennung** — OpenWakeWord mit ONNX Runtime (~20% CPU)
- **LED-Feedback** — Visuelles Feedback: Idle (Blau), Listening (Grün), Processing (Gelb), Speaking (Cyan), Error (Rot)
- **Hardware-Button** — Manuelle Aktivierung
- **Auto-Discovery** — Backend-Erkennung via Zeroconf/mDNS
- **OTA-Updates** — Version-Tracking und Update-Pakete

### Zentrale Wake-Word-Verwaltung
- Admin-UI für zentrale Konfiguration
- Automatische Synchronisation per WebSocket an alle Geräte
- Konfigurierbare Keywords (Alexa, Hey Mycroft, Hey Jarvis, etc.)

Siehe [WAKEWORD_CONFIGURATION.md](WAKEWORD_CONFIGURATION.md) für Details.

### Audio-Output-Routing
Intelligentes TTS-Routing zum optimalen Ausgabegerät pro Raum (prioritätsbasiert, mit Verfügbarkeitsprüfung). Unterstützt Renfield-Geräte und HA Media Players.

Siehe [OUTPUT_ROUTING.md](OUTPUT_ROUTING.md) für Details.

### Automatische Raum-Erkennung
- **IP-basiert**: Stationäre Geräte werden anhand der IP-Adresse erkannt
- **Kontext-Weitergabe**: Raum-Kontext wird an LLM übergeben
- **Implizite Befehle**: "Schalte das Licht ein" funktioniert ohne Raum-Angabe

### Frontend-Verbindungsarchitektur

Das Frontend nutzt **zwei unabhängige WebSocket-Verbindungen**:

| Verbindung | Endpoint | Zweck |
|------------|----------|-------|
| Chat WS | `/ws` | Chat-Nachrichten, Session-Persistenz |
| Device WS | `/ws/device` | Geräte-Registrierung, Raum-Zuweisung, Capabilities |

Chat funktioniert ohne Geräte-Registrierung, aber Raum-Kontext erfordert diese.

## Raum-Management

- **CRUD-Operationen**: Räume erstellen, bearbeiten, löschen
- **Alias-System**: Normalisierte Namen für Sprachbefehle
- **Home Assistant Area Sync**: Import und Export von Areas mit Konfliktlösung
- **Source-Tracking**: Ursprung des Raums (Renfield, Home Assistant, Satellite)
- **Geräte pro Raum**: Übersicht, Online-Status, Geräte verschieben

## Home Assistant Integration

Steuerung erfolgt über den Home Assistant MCP-Server (`HA_MCP_ENABLED=true`) oder die direkte REST API (`/api/homeassistant`).

### Gerätesteuerung
- **Lichter**: Ein/Aus/Dimmen/Farbsteuerung
- **Schalter**: Beliebige Schalter steuern
- **Klimaanlagen**: Temperatur und Modi
- **Rollläden**: Öffnen/Schließen/Position
- **Sensoren**: Status abfragen

### Natural Language Control
```
"Schalte das Licht im Wohnzimmer ein"
"Mach die Heizung im Schlafzimmer auf 21 Grad"
"Sind alle Fenster geschlossen?"
"Aktiviere Filmabend"
```

### Entity Discovery
- Automatische Erkennung aller Home Assistant Entities
- Keyword-Refresh: `POST /admin/refresh-keywords`
- Domain-Filterung, Echtzeitstatus

## Kamera-Überwachung

### Frigate Integration
- **Event-Erkennung**: Person, Auto, Tier, etc.
- **Snapshot-Zugriff**: Bilder von Events abrufen
- **Zone-Überwachung**: Verschiedene Bereiche
- **Event-Historie**: Zeitliche Suche, Objekt-Filterung, Konfidenz-Werte

### Konfiguration
```env
FRIGATE_URL=http://frigate.local:5000
FRIGATE_TIMEOUT=10.0
```

## Zugriffskontrolle (RPBAC)

Optional aktivierbares JWT-basiertes Role-Permission System.

### Berechtigungs-Hierarchie
```
Knowledge Bases: kb.all > kb.shared > kb.own > kb.none
Smart Home:      ha.full > ha.control > ha.read > ha.none
Kameras:         cam.full > cam.view > cam.none
```

### Standard-Rollen

| Rolle | KB | Smart Home | Kameras |
|-------|----|-----------|---------|
| Admin | Vollzugriff | Vollzugriff | Vollzugriff |
| Familie | Eigene + geteilte | Vollzugriff | Ansehen |
| Gast | Keine | Nur lesen | Keine |

### Features
- JWT Access + Refresh Tokens
- Voice Authentication (optional)
- Resource Ownership (KBs, Konversationen)
- KB-Sharing zwischen Nutzern

### Konfiguration
```env
AUTH_ENABLED=false                 # Master-Switch (opt-in)
SECRET_KEY=changeme               # JWT Secret
VOICE_AUTH_ENABLED=false          # Stimm-Authentifizierung
```

Siehe [ACCESS_CONTROL.md](ACCESS_CONTROL.md) für vollständige Dokumentation.

## Mehrsprachigkeit (i18n)

### Unterstützte Sprachen
- **Deutsch (de)**: Vollständig übersetzt (Standard)
- **Englisch (en)**: Vollständig übersetzt

### Implementierung
- **react-i18next** für Frontend-Internationalisierung
- **Automatische Erkennung** der Browsersprache
- **Persistente Speicherung** in localStorage
- **Header-Dropdown** mit Globus-Icon für Sprachwechsel

### Übersetzte Bereiche
Navigation, Chat, Dashboard, Einstellungen, Geräteverwaltung, Benutzer & Rollen, Fehlermeldungen

Siehe [MULTILANGUAGE.md](MULTILANGUAGE.md) für die vollständige Anleitung.

## Dark Mode

- **Drei Modi**: Hell, Dunkel, System (folgt OS-Präferenz)
- **Tailwind CSS**: Class-basiertes Dark Mode mit `dark:` Prefix
- **ThemeContext**: React Context für globale Theme-Verwaltung
- **Persistenz**: Einstellung wird in localStorage gespeichert
- **FOUC-Prevention**: Kein Flackern durch Pre-Render-Script

## Progressive Web App

- **Multi-Platform**: Desktop, Tablet, Smartphone
- **Installierbar**: Home-Screen auf iOS/Android
- **Full-Screen**: Ohne Browser-UI
- **Responsive**: Mobile-First Design mit adaptivem Layout
- **Offline**: Funktioniert ohne Internet (Service Worker)

## Sicherheit

### Offline-First
- Alle Daten bleiben lokal, keine Cloud-Verbindungen für Kernfunktionen
- Keine Telemetrie, kein Tracking

### Rate Limiting
```env
# REST API
API_RATE_LIMIT_DEFAULT=100/minute
API_RATE_LIMIT_AUTH=10/minute       # Login/Register (strenger)
API_RATE_LIMIT_VOICE=30/minute
API_RATE_LIMIT_CHAT=60/minute
API_RATE_LIMIT_ADMIN=200/minute

# WebSocket
WS_RATE_LIMIT_PER_SECOND=50        # Ermöglicht Audio-Streaming
WS_RATE_LIMIT_PER_MINUTE=1000
WS_MAX_CONNECTIONS_PER_IP=10
WS_MAX_MESSAGE_SIZE=1000000         # 1MB
```

### Circuit Breaker

Automatische Ausfallsicherung für LLM- und Agent-Aufrufe:

| Zustand | Beschreibung |
|---------|-------------|
| CLOSED | Normal — Requests werden durchgeleitet |
| OPEN | Service ausgefallen — Requests sofort abgelehnt |
| HALF_OPEN | Recovery-Test — Einzelne Requests durchgelassen |

```env
CB_FAILURE_THRESHOLD=3              # Fehler bis OPEN
CB_LLM_RECOVERY_TIMEOUT=30.0       # LLM Recovery (Sekunden)
CB_AGENT_RECOVERY_TIMEOUT=60.0     # Agent Recovery (Sekunden)
```

### Secrets Management

Produktion nutzt Docker Compose file-based Secrets (`/run/secrets/`) statt `.env` für sensitive Werte. Pydantic Settings lädt aus `secrets_dir="/run/secrets"`, und MCP-Client injiziert Secrets in `os.environ` für YAML-Substitution und stdio-Subprozesse.

Siehe [SECRETS_MANAGEMENT.md](SECRETS_MANAGEMENT.md) für Details.

### Weitere Sicherheitsfeatures
- **CORS**: Konfigurierbare Origins (`CORS_ORIGINS`)
- **Trusted Proxies**: CIDR-basiert (`TRUSTED_PROXIES`)
- **WebSocket Auth**: Optional aktivierbar (`WS_AUTH_ENABLED`)
- **Passwort-Hashing**: bcrypt
- **MCP Response Limits**: Max Response-Größe begrenzt (`MCP_MAX_RESPONSE_SIZE`)

## Monitoring

### Prometheus Metrics

Opt-in Endpoint für Prometheus-kompatible Metriken:

```env
METRICS_ENABLED=false               # Aktivieren: true
```

Endpoint: `GET /metrics` (Prometheus Exposition Format)

### Health Checks
- `GET /health` — Backend Health Check
- `GET /api/mcp/status` — MCP-Server Status
- Docker Compose Health Checks für alle Container

### Logging
- **Strukturierte Logs**: Konfigurierbar via `LOG_LEVEL` (DEBUG, INFO, WARNING, ERROR)
- **Container-Logs**: `docker compose logs -f backend`

## LLM-Konfiguration

### Multi-Modell Support

Renfield unterstützt separate Modelle pro Aufgabe. Jedes kann auf einer anderen Ollama-Instanz laufen.

```env
# Basis
OLLAMA_URL=http://ollama:11434
OLLAMA_NUM_CTX=32768                # Context Window

# Pro Aufgabe
OLLAMA_CHAT_MODEL=qwen3:14b        # Chat-Antworten
OLLAMA_INTENT_MODEL=qwen3:8b       # Intent-Erkennung
OLLAMA_RAG_MODEL=qwen3:14b         # RAG-Antworten
OLLAMA_EMBED_MODEL=nomic-embed-text # Embeddings (768 Dim.)
OLLAMA_MODEL=llama3.2:3b           # Legacy Fallback

# Agent (optional)
AGENT_MODEL=                        # Separates Agent-Modell
AGENT_OLLAMA_URL=                   # Separate Ollama-Instanz
```

### Externe Ollama-Instanz

Ollama kann auf einem separaten GPU-Server laufen:

```env
OLLAMA_URL=http://cuda.local:11434
```

Siehe [LLM_MODEL_GUIDE.md](LLM_MODEL_GUIDE.md) für Modell-Empfehlungen und Benchmarks.

### LLM Client Factory

Alle Services nutzen eine zentrale Factory (`utils/llm_client.py`) mit URL-basiertem Caching (gleiche URL → gleiche Client-Instanz) und einem `LLMClient` Protocol.

## Plugin System (Legacy)

YAML-basierte REST-API-Integrationen für einfache Dienste ohne eigenen MCP-Server.

```yaml
name: mein_plugin
version: 1.0.0
enabled_var: MEIN_PLUGIN_ENABLED
config:
  url: MEIN_PLUGIN_API_URL
intents:
  - name: mein_plugin.aktion
    api:
      method: GET
      url: "{config.url}/endpoint"
```

> **Hinweis:** MCP-Server sind der bevorzugte Integrationsweg. YAML-Plugins nutzen `*_PLUGIN_ENABLED` Variablen, um Konflikte mit MCP-Server `*_ENABLED` zu vermeiden.

---

Ausführliche Entwickler-Dokumentation (Architektur, Patterns, Commands) in [CLAUDE.md](../CLAUDE.md).
