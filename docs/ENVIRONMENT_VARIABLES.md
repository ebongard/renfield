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
- `qwen3-embedding:4b` - Embedding-Modell mit exzellentem Deutsch (2560 dim)

---

### Vision LLM (Satellite Camera)

```bash
# Vision-fähiges Modell für Kamera-Snapshots von Satellites
# Leer = Visual Queries deaktiviert (Bilder werden ignoriert)
OLLAMA_VISION_MODEL=qwen3-vl:8b

# Optional: Separate Ollama-URL für das Vision-Modell
# Nützlich wenn Vision auf einer anderen GPU läuft als Chat
OLLAMA_VISION_URL=http://host.docker.internal:11434
```

**Defaults:**
- `OLLAMA_VISION_MODEL`: `""` (deaktiviert) — Code-Default. **Produktion setzt `qwen3-vl:8b`** (siehe `k8s/configmap.yaml`); der Vision-Tier ist seit 2026-05-22 aktiv und läuft auf dem cluster-internen `ollama`-Pod (k8s-gpu-1).
- `OLLAMA_VISION_URL`: `None` (verwendet Standard-OLLAMA_URL)

**Empfohlenes Modell:** `qwen3-vl:8b` (~12 GB VRAM, passt auf 16 GB Karten, gutes Deutsch).

Siehe [SATELLITE_CAMERA.md](SATELLITE_CAMERA.md) für Setup und Modellvergleich.

---

### Sprache & Voice

```bash
# Standard-Sprache für STT/TTS
DEFAULT_LANGUAGE=de

# Unterstützte Sprachen (kommasepariert)
SUPPORTED_LANGUAGES=de,en,it

# Whisper STT Modell
WHISPER_MODEL=base

# Piper Multi-Voice Konfiguration (pro Sprache)
PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium

# Fallback-Stimme, wenn die angeforderte Sprache nicht in PIPER_VOICES enthalten ist
PIPER_DEFAULT_VOICE=de_DE-thorsten-high
```

**Defaults:**
- `DEFAULT_LANGUAGE`: `de`
- `SUPPORTED_LANGUAGES`: `de,en,it`
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

### Folgefragen-Chips (Follow-up Chips)

```bash
FOLLOWUP_CHIPS_ENABLED=false
FOLLOWUP_CHIPS_MODEL=            # leer → OLLAMA_INTENT_MODEL (kleines/schnelles Modell)
FOLLOWUP_CHIPS_COUNT=3           # 1-5
FOLLOWUP_CHIPS_TIMEOUT_SECONDS=5.0  # 1-30; Best-Effort-Obergrenze
```

**Defaults:**
- `FOLLOWUP_CHIPS_ENABLED`: `false` (Opt-in/dark)
- `FOLLOWUP_CHIPS_MODEL`: `""` (fällt auf `OLLAMA_INTENT_MODEL` zurück)
- `FOLLOWUP_CHIPS_COUNT`: `3`
- `FOLLOWUP_CHIPS_TIMEOUT_SECONDS`: `5.0`

**Wann aktivieren:** Schlägt nach jeder substanziellen Chat-Antwort 2-4 anklickbare
Folgefragen vor (Tipp füllt das Eingabefeld, kein Auto-Senden). Erzeugt einen
**zusätzlichen kleinen LLM-Aufruf pro Turn** — läuft im Hintergrund nach dem
`done`-Frame, verzögert also Antwort/TTS/Wakeword nicht. Bei gesprochenen
(TTS-)Antworten übersprungen (Chips sind nur visuell). Auf einer ausgelasteten
gemeinsamen GPU die zusätzliche Inferenzlast bedenken.

### Befehlspalette (Command Palette)

```bash
COMMAND_PALETTE_ENABLED=false
```

**Default:** `false` (Opt-in/dark). Rein Frontend-Gate (`/api/config/features`) — das
backendseitige `role_hint`-Handling ist immer aktiv (No-op ohne Hint), daher braucht
das Umschalten **kein** Backend-Redeploy.

### Nachrichten-Branching (Edit-and-Fork)

```bash
CHAT_BRANCHING_ENABLED=false
```

**Default:** `false` (Opt-in/dark; Frontend-Gate via `/api/config/features`). Schaltet
die Edit-/Regenerate-Affordances (Phase 1: letzte Nutzernachricht bearbeiten / letzten
Assistenten-Turn neu generieren → Branch) frei. Das Konversationsbaum-**Schema** und die
aktive-Pfad-CTE laufen **immer** (der einmalige Backfill macht den Flag-aus-Zustand
byte-identisch), nur die Fork-Affordances/Endpunkte sind gegated. Design:
`docs/design/chat-branching.md`.

### Nachrichtensuche (Message Search)

```bash
MESSAGE_SEARCH_ENABLED=false
```

**Default:** `false` (Frontend-Gate via `/api/config/features`). Schaltet das
Suchfeld in der Konversations-Seitenleiste frei. Der Backend-Endpunkt
(`GET /api/chat/messages/search`) und die `messages.search_vector`-Spalte sind
immer vorhanden (harmlos), daher braucht das Umschalten **kein** Backend-Redeploy —
nur die Migration `pc20260617_messages_search_vector` muss angewendet sein. Suche ist
strikt nach Konversations-Eigentümerschaft gefiltert (nicht über `circle_sql`).

### Projekte (Business-Instanz, Phase 1)

```bash
PROJECTS_ENABLED=false
```

**Default:** `false` (Opt-in/dark; auch via `/api/config/features` ans Frontend gemeldet).
Schaltet das minimale **Projekt**-Modell frei: pro Projekt genau **eine** dedizierte
KnowledgeBase (1:1) plus CRUD (`/api/projects`) und die `/projects`-Seite. Ist das Flag
aus, geben alle `/api/projects`-Routen 404 zurück und der Nav-Eintrag fehlt — die
Household-Instanz ist byte-identisch. Owner-scoped (Auth an → nur eigene Projekte;
Auth aus → Single-User sieht alle). Das Löschen eines Projekts entfernt nur die
Projekt-Zeile, **nicht** die KnowledgeBase oder deren Dokumente. Meetings,
Transkription, Timeline und die Protokoll-Pipeline sind spätere Phasen. Migration
`pc20260713_projects`.

**Wann aktivieren:** Blendet im Chat eine `/`-getriggerte (bzw. per Touch-Button
geöffnete) Aktions-/Navigations-Palette ein. Tool-Aktionen werden ins Eingabefeld
**vorbereitet** (kein Auto-Senden); der Anzeige-Filter folgt den Berechtigungen, die
echte Rechte-Prüfung bleibt serverseitig. Der „Rolle setzen"-Hint ist eine weiche
Routing-Präferenz für den nächsten Turn (keine Rechte-Eskalation).

### Chat-Artefakte (Artifacts — Lane A)

```bash
ARTIFACTS_TYPED_ENABLED=false        # Lane A: typed table/list/keyvalue/chart inline
ARTIFACTS_HTML_SANDBOX_ENABLED=false # Lane B: free-form HTML/SVG — NICHT verdrahtet, NICHT aktivieren
```

**Default:** beide `false` (Opt-in/dark). `ARTIFACTS_TYPED_ENABLED` schaltet die
**Lane-A**-Artefakte frei: generierte Tabellen/Listen/Key-Value/Charts werden inline
im Chat-Turn als **typisierte JSON-Daten → echte React-Komponenten** gerendert (kein
Modell-HTML, kein Modell-SVG — React's Escape-Boundary ist die gesamte
Sicherheitsstory; siehe `docs/design/chat-artifacts-sandbox.md`). Gated sind **beide**
Seiten: der Backend-`artifact`-WS-Frame-Emit (nur aus dem Hook-/Sub-Intent-/
Orchestrierungs-Pfad, nie aus Agent-Freitext) **und** der Frontend-Renderer
(`/api/config/features`). Ungültige/zu große Payloads fallen auf einen escapten
Code-Block zurück (fail-closed). Umschalten braucht **kein** Backend-Redeploy.

`ARTIFACTS_HTML_SANDBOX_ENABLED` ist ein **Platzhalter** für die zurückgestellte
**Lane B** (free-form HTML/SVG in einer sandboxed iframe) — in dieser Auslieferung an
nichts verdrahtet, erfordert ein eigenes Security-Review vor dem Bau. **Nicht
aktivieren.**

**Voraussetzung (Lane A):** Die baseline-CSP in `nginx.conf` ist enforcing (Frontend
≥ v2.15.16) — gute Hygiene für Lane A, Pflicht für ein späteres Lane B.

### Raum-Handoff-Hinweis (Chat-UI Item 8)

```bash
ROOM_HANDOFF_ENABLED=false
```

**Default:** `false` (Opt-in/dark). Schaltet die inline Chat-Anzeige frei, wenn die
**Medienwiedergabe dem Nutzer in einen anderen Raum folgt** (Media-Follow): eine
dezente Meta-Zeile "🔊 Wiedergabe folgt nach {Raum}" im Chat-Thread. Gated sind
**beide** Seiten — der Backend-`media_handoff`-Frame (`media_follow_service`, emittiert
NUR bei erfolgreichem Resume, **raum-scoped** an die Zielraum-Geräte = dieselbe
Privacy-Reichweite wie der bestehende Info-Toast) **und** der Frontend-Renderer
(`MediaHandoffIndicator`, `/api/config/features`). Transient (12 s TTL, max 3, nie in
der Historie). Umschalten braucht **kein** Backend-Redeploy. (Der „conversation
continued in {Raum}"-Fall ist als Frame-Kind `continued` reserviert, aber noch nicht
backend-verdrahtet — Follow-up.)

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

#### Obligation-Deadline Notifier (Schicht A)

```bash
# Tägliche, besitzer-adressierte Fristen-Erinnerungen aus document_facts.
# Benötigt zusätzlich PROACTIVE_ENABLED=true (Zustellung läuft über das
# Proactive-Subsystem); sonst liefe der Scan ins Leere und würde Meilensteine
# im Ledger verbrauchen, ohne zuzustellen.
OBLIGATION_NOTIFIER_ENABLED=false
OBLIGATION_NOTIFIER_INTERVAL=86400            # Scan-Intervall in Sekunden (täglich)
OBLIGATION_NOTIFIER_OVERDUE_GRACE_DAYS=30     # wie weit zurück "überfällig" noch feuert
```

Ein Tages-Scan berechnet pro Verpflichtung den einen aktuellen Vorlauf-Meilenstein
(`14d`/`7d`/`3d`/`1d`/`due`/`overdue`) und feuert ihn genau einmal (Ledger
`obligation_acknowledgements`, restart-fest). Rechtliche Fristen werden gemeldet,
aber human-gated (`/brain/review`), nie automatisch erledigt. Die Agenda-Bestätigung
(`POST/DELETE /api/atoms/obligations/{id}/confirm`) nutzt dasselbe Ledger.

```bash
# Wöchentliches Sammel-Digest — das Sicherheitsnetz UNTER dem Notifier. Ein
# besitzer-adressiertes Wochen-Digest ALLER offenen Verpflichtungen OHNE untere
# Datumsgrenze, fängt also spät extrahierte / lange überfällige Fristen ab, die
# das Scan-Fenster des Notifiers verpasst. Benötigt ebenfalls PROACTIVE_ENABLED.
OBLIGATION_DIGEST_ENABLED=false
OBLIGATION_DIGEST_INTERVAL=604800             # wöchentlich (Sekunden)
OBLIGATION_DIGEST_HORIZON_DAYS=30             # kommende Fristen bis N Tage (überfällige immer dabei)
```

Dedup pro `(user, ISO-Woche)` über `obligation_digest_log` (restart-fest); die
Kalenderwoche steht im Titel, damit zwei legitime Wochen-Digests nicht über die
Content-Hash-Dedup kollidieren. Ein never-extracted Fall bleibt ungedeckt (muss
upstream sichtbar bleiben).

```bash
# Obligation → Kalender-Auto-Push (Calendar MCP). Per-User opt-in: nur Nutzer
# mit einer Kalender-Präferenz (GET/PUT /api/atoms/obligations/calendar-pref)
# bekommen ihre offenen Fristen als Kalendereinträge gespiegelt (create/update/
# delete-Reconciler). Benötigt das Calendar MCP (CALENDAR_ENABLED) erreichbar.
OBLIGATION_CALENDAR_SYNC_ENABLED=false
OBLIGATION_CALENDAR_SYNC_INTERVAL=86400        # täglich (Sekunden)
OBLIGATION_CALENDAR_EVENT_HOUR=9               # Uhrzeit des (terminierten) Events; all-day vom MCP nicht unterstützt
OBLIGATION_CALENDAR_HORIZON_DAYS=90            # Fristen bis N Tage voraus synchronisieren
OBLIGATION_CALENDAR_RETAIN_PAST_DAYS=30        # vergangene Events so lange behalten
OBLIGATION_CALENDAR_MAX_OPS_PER_RUN=100        # Cap der create/update-MCP-Aufrufe je Nutzer je Lauf
```

Reconciler-Ledger `obligation_calendar_events` (fact→event_id, FK ON DELETE SET
NULL für Waisen-Bereinigung); besitzer-adressiert (MCP erzwingt Kalenderzugriff
per user_id); restart-fest + advisory-locked. Bekannt: ohne Idempotenz-Key des
MCP kann ein Crash zwischen erfolgreichem create und Ledger-Commit ein Duplikat
hinterlassen (at-least-once; P2).

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
PRESENCE_HYSTERESIS_SCANS=2              # Aufeinanderfolgende Scans vor Raumwechsel (Legacy-Fallback, nur wenn der RSSI-Filter aus ist)
PRESENCE_RSSI_THRESHOLD=-80              # dBm, schwächere Signale werden für Raumzuweisung ignoriert
# Asymmetrischer RSSI-Filter + Margin-Hysterese (#10, ESPresense/Bermuda-Ansatz)
PRESENCE_RSSI_FILTER_ENABLED=true       # false → Legacy raw-mean + N-Scan-Verhalten
PRESENCE_RSSI_FILTER_ALPHA_UP=0.5       # EWMA-Gewicht wenn ein Raum STÄRKER wird (schnell — snappy beim Betreten)
PRESENCE_RSSI_FILTER_ALPHA_DOWN=0.1     # EWMA-Gewicht wenn SCHWÄCHER (langsam — dämpft Abgänge + Streu-Werte)
PRESENCE_FILTER_FRESH_SECONDS=35.0      # ein Raum, der so lange nicht gehört wurde, verfällt Richtung Boden
PRESENCE_SWITCH_ENTER_MARGIN_DB=8.0     # dB, um die der GEFILTERTE Wert des Herausforderers den aktuellen Raum schlagen muss (ersetzt den Scan-Count)
PRESENCE_HOUSEHOLD_ROLES="Admin,Familie" # Rollen die als Haushaltsmitglieder gelten (für Privacy-TTS)
PRESENCE_ANALYTICS_TIMEZONE="Europe/Berlin" # Lokale Zeitzone für Heatmap-/Prognose-Stunden+Tage (Events werden UTC gespeichert); ungültiger Wert => UTC

# Presence Webhooks (Automation-Hooks)
PRESENCE_WEBHOOK_URL=""                  # URL für Presence-Events (leer = deaktiviert). Unterstützt n8n Webhook-Trigger
PRESENCE_WEBHOOK_SECRET=""               # Shared Secret als X-Webhook-Secret Header für Webhook-Authentifizierung

# Nachricht ausrichten (internal.announce_in_room): Kamera-Belegungs-Check
# Bei einer persönlichen Nachricht wird nach dem BLE-Gate (falls eine Kamera im
# Raum ist) ein Snapshot gemacht und per Vision-Modell die Personenzahl gezählt,
# um einen NICHT per BLE getrackten Anwesenden zu erkennen. Siehe docs/MESSAGE_RELAY.md.
ANNOUNCE_CAMERA_OCCUPANCY_CHECK=true     # Kamera-Check nutzen, wenn eine Kamera im Raum ist
ANNOUNCE_CAMERA_CHECK_FAIL_CLOSED=false  # Bei Snapshot-/Vision-Fehler: true=blockieren, false=auf BLE-Gate zurückfallen
ANNOUNCE_SNAPSHOT_TIMEOUT=8.0            # Sekunden Timeout für Satelliten-Snapshot + Vision (jeweils)
```

**Satellite-Konfiguration** (in `satellite.yaml`):
```yaml
ble:
  enabled: true
  scan_interval: 30          # Sekunden zwischen Scans
  scan_duration: 5           # Sekunden pro Scan
  rssi_threshold: -80        # Schwächere Signale ignorieren
  classic_rssi: true         # Echtes Classic-BT-RSSI lesen (hcitool cc/rssi via sudo); false => synthetisch -50
  classic_rssi_interval: 300 # Sekunden zwischen echten RSSI-Reads pro Gerät (begrenzt Verbindungs-Churn)
```

> **Classic-BT-RSSI:** Classic-BT-Präsenz beruht auf `hcitool name` (binär an/aus) und lieferte
> früher ein konstantes synthetisches `-50` von jedem Satelliten — bei zwei Satelliten gab das ein
> Unentschieden und der Raum „flatterte". Mit `classic_rssi: true` liest der Satellit per kurzlebiger
> ACL-Verbindung (`hcitool cc/rssi/dc` über passwortloses `sudo`) ein echtes RSSI, gedrosselt auf
> einmal pro `classic_rssi_interval` (häufiges Verbinden lässt das Telefon sonst aufhören, auf
> `name` zu antworten → Präsenz fällt auf „abwesend"). Schlägt der Read fehl, greift der synthetische
> Fallback — Präsenz geht nie verloren.

**Endpunkte:**
- `GET /api/presence/rooms` — Alle Räume mit Anwesenden
- `GET /api/presence/room/{id}` — Anwesende in einem Raum
- `GET /api/presence/user/{id}` — Standort + allein?
- `GET /api/presence/devices` — Registrierte BLE-Geräte (Admin)
- `POST /api/presence/devices` — BLE-Gerät registrieren (Admin)
- `DELETE /api/presence/devices/{id}` — BLE-Gerät entfernen (Admin)

---

### Satellite-Enrollment (Security Review H1)

Per-Satellite-Identität: ein Satellit weist sich mit einem eigenen PSK
(256-bit, server-seitig nur als bcrypt-Hash gespeichert) im register-Frame aus,
statt eine `satellite_id` nur zu *behaupten*. Schließt die Wurzel von H1
(IRK-Disclosure + Raum-Hijack durch ein beliebiges LAN-Gerät).

```bash
# Effektiv-Modus-Zustandsmaschine (siehe docs/private/security/satellite-trust-design.md):
#   aus (Default)            → Legacy, keine PSK-Prüfung, register-Pfad byte-identisch
#   an + nicht erzwingend    → PERMISSIVE/Soak: vorgelegter PSK wird geprüft
#                              (falsch/unbekannt/widerrufen → abgelehnt), kein PSK →
#                              erlaubt aber als unenrolled geloggt; IRKs nur an verifizierte Sats
#   ENFORCING (Auto-Flip)    → kein gültiger PSK → abgelehnt
SATELLITE_ENROLLMENT_ENABLED=false
# Auto-Flip PERMISSIVE→ENFORCING, sobald JEDE eingeschriebene Zeile sich mindestens
# einmal authentifiziert hat (nicht nur die aktuell verbundenen) — dann persistent
# verriegelt. Default aus, bis die Flotte vollständig eingeschrieben ist.
SATELLITE_ENROLLMENT_AUTOFLIP_ENABLED=false

# Stop-gap aus der chirurgischen H1-Mitigation (greift nur wenn ENROLLMENT aus):
# Komma-Liste der satellite_ids, die per-Person-IRKs empfangen dürfen. Leer =
# ungated (Legacy) mit lauter Einmal-Warnung pro Satellit.
SATELLITE_IRK_ALLOWLIST=""
```

**Satellite-Seite** (`satellite.yaml` → `server.enrollment_token`, oder Env
`RENFIELD_ENROLLMENT_TOKEN`; bare-metal: gitignored host_var
`satellite_enrollment_token`; k8s: per-Pod-Secret). PSK ausstellen mit
`bin/enroll_satellite.py <satellite_id>` oder über die Admin-UI
(`/api/satellite-enrollment/enroll`, ADMIN-gated; Token wird genau **einmal**
angezeigt). Rollout gestaffelt: dark → Flotte einschreiben → `…_ENABLED=true`
(PERMISSIVE) → alle Sats authentifiziert (`GET /api/satellite-enrollment/status`)
→ `…_AUTOFLIP_ENABLED=true` → ENFORCING verriegelt. Break-glass: `…_ENABLED=false`.

**Endpunkte (alle ADMIN-gated):**
- `GET /api/satellite-enrollment` — eingeschriebene Satelliten + Status (nie das Token)
- `GET /api/satellite-enrollment/status` — Flotten-Readiness für den Rollout-Gate
- `POST /api/satellite-enrollment/enroll` — PSK ausstellen/rotieren (Token einmalig)
- `DELETE /api/satellite-enrollment/{satellite_id}` — Enrollment widerrufen

---

### Satellite-Audio-Transport + Voice-Identity P0 (C1, `docs/design/voice-identity-wakeword-verification.md`)

```bash
# C1 binärer Opus-Transport (dark). Aus (Default): ein Satellit, der beim
# register audio_codec=opus anbietet, bekommt "pcm" zurück und bleibt auf dem
# Legacy-base64-PCM-JSON-Pfad — Flottenverhalten byte-identisch. An: der Satellit
# streamt binäre WS-Frames; das Backend puffert die rohen Opus-Pakete und leitet
# sie an den Voice-Server (/api/voice/stt-opus) weiter, der sie dort dekodiert
# (Opus-Decode liegt seit C2 Phase 1 auf der Media-Schicht, nicht mehr im Backend;
# opuslib/libopus0 sind aus dem Backend-Image entfernt). STT/Speaker-Pfade
# unverändert. Setzt voice_server_url voraus. Satellit-Seite: satellite.yaml
# audio.codec: "opus" PLUS opuslib + libopus0 auf dem Satelliten (Encode-Seite):
# bare-metal über die dedizierte opuslib-Task in provision.yml ([python, app],
# damit auch ein Code-only `--tags app`-Deploy sie mitnimmt) + libopus0 in
# satellite_system_packages ([system]), k8s-Pod-Sat (Esszimmer) über das
# Satelliten-Image (Dockerfile). Fehlt opuslib/libopus0, fällt der Satellit
# graceful auf pcm zurück. Flotte auf Opus (2026-07-09): Fitnessraum,
# Arbeitszimmer, Wohnzimmer, Kinderbad (bare-metal) + Esszimmer (Pod).
#
# DEPLOY-INVARIANTE: Vor dem Aktivieren MUSS das Voice-Server-Image opuslib +
# libopus0 enthalten (seit C2 Phase 1 im voice-server Dockerfile/requirements).
# Fehlt es (Image-Skew), antwortet /api/voice/stt-opus mit 503 statt still leerem
# Transkript, und der Voice-Server loggt beim Start eine Warnung. Also: erst
# Voice-Server neu bauen/ausrollen, dann SATELLITE_OPUS_ENABLED=true.
SATELLITE_OPUS_ENABLED=false

# Voice-Server-Auth. Default true = das Backend signiert für jeden Voice-Server-
# Call (STT/TTS/Meeting) ein Service-JWT mit dem eigenen SECRET_KEY, das der
# Voice-Server im local-Modus validiert. Auf false setzen, wenn diese Instanz den
# Voice-Server EINER ANDEREN Instanz mitbenutzt (z.B. renfield-xidra → Household-
# Voice-Server): der eigene SECRET_KEY unterscheidet sich, ein selbst signiertes
# Token würde an der Signaturprüfung scheitern (401) — auch bei auth_required=false
# (das überspringt Auth nur bei FEHLENDEM Token). false → Backend sendet KEIN
# Token → auth_required=false-Voice-Server behandelt den Call als anonym. Interim
# bis zum Voice-Server-Topologie-Redesign (echte Multi-Tenant-Auth).
VOICE_SERVER_AUTH_ENABLED=true

# P0 Fail-loud-Fallback: das In-Process-SpeechBrain-ECAPA und das
# voice-server-ONNX-ECAPA teilen KEINEN Repräsentationsraum. Default aus =
# das Backend verweigert SpeechBrain-Embeddings (Extraktion/Vergleich/
# Speicherung) mit WARNING + Metrik
# renfield_speaker_inprocess_embedding_blocked_total; Transkription selbst
# läuft weiter. Nur in Dev-Umgebungen OHNE voice-server auf true setzen.
SPEAKER_INPROCESS_EMBEDDINGS_ENABLED=false
```

---

### Signierte OTA-Pakete (Security Review H6)

Code-Authentizität unabhängig von Transport/Backend: ein Release wird über ein
**signiertes Quell-Manifest** (Version + sortierte Pro-Datei-SHA256) abgesichert,
das **offline** mit einem Ed25519-Release-Key signiert wird. Das Backend
**leitet** Manifest+Signatur nur weiter (kann nicht signieren); der Satellit
prüft Signatur (gegen gepinnte Public Keys) + Datei-Hashes + Version **vor** dem
Install.

```bash
# Backend: signiertes OTA erzwingen (fail-closed) — Default aus, bis die
# Signing-Pipeline + Public Keys auf der Flotte sind.
SATELLITE_OTA_REQUIRE_SIGNATURE=false
```

**Satellite-Seite** (`satellite.yaml` → `update:`, Public Keys sind git-safe):
```yaml
update:
  require_signature: false      # fail-closed wenn true (auch Env RENFIELD_OTA_REQUIRE_SIGNATURE)
  release_pubkeys:              # hex Ed25519 Public Keys (mehrere = Rotation;
    - ""                        # auch Env RENFIELD_RELEASE_PUBKEYS, kommagetrennt)
```

**Release-Signing (offline, manuell):**
- `bin/sign_satellite_release.py --gen-key --out <keyfile>` — Keypair erzeugen,
  Public-Key-Hex in group_vars `satellite_release_pubkeys` eintragen.
- `bin/sign_satellite_release.py --sign --key <keyfile>` — schreibt
  `src/satellite/RELEASE_MANIFEST.json` + `.sig` (committen; ins Backend-Image
  gebacken). Privater Key bleibt OFFLINE (nie auf Build-Box/Backend).
- `bin/sign_satellite_release.py --verify --pubkey <hex>` — CI/Pre-Build-Check
  (Manifest == aktuelle Quelle + Signatur gültig).

**Dark by default:** kein committetes Manifest + `require_signature=false`
→ Backend leitet `None` weiter, Satellit prüft nur Checksum (Legacy). Rollout:
gen-key → Public Key in group_vars → sign + Image bauen → re-provisionieren →
`require_signature` flippen.

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

**FTS Config (Legacy / informational only, ab pc20260529):**
Seit der Multilingual-FTS-Umstellung (Migrationen `pc20260528` + `pc20260529`) konsultiert KEIN Query-Pfad mehr `RAG_HYBRID_FTS_CONFIG`. Beide FTS-Spalten — `document_chunks.search_vector` UND `conversation_memories.search_vector` — sind `GENERATED STORED`-Spalten, deren Ausdruck `to_tsvector` über alle in `services/fts_languages.FTS_LANGUAGES` definierten Configs unioniert (`german`, `english`, `french`, `italian`, `spanish`, `dutch`). Beide Retriever (`RAGRetrieval._search_bm25`, `LexicalRetrieval.search_*_lexical`) unionieren `websearch_to_tsquery` über dieselbe Menge.

`RAG_HYBRID_FTS_CONFIG` bleibt als deklarative Angabe der "primären Sprache des Deployments" erhalten — bei einem Wert ausserhalb von `FTS_LANGUAGES` warnt der Startup-Hook (`services/lexical_retrieval.py::_check_fts_config_at_startup`), damit Config-Drift sichtbar bleibt. Empfehlung: leeren oder auf die Hauptsprache des Haushalts setzen (`german` als Default).

`POST /api/knowledge/reindex-fts` triggert seit pc20260529 ein `REINDEX INDEX CONCURRENTLY` auf den GIN-Index — keine Row-Repopulation mehr nötig (Spalte ist GENERATED und immer aktuell).

Eine 7. Sprache hinzufügen: `services/fts_languages.FTS_LANGUAGES`-Tuple erweitern UND zwei Folge-Migrationen schreiben (je eine für `conversation_memories` und `document_chunks`), die die GENERATED-Spalte droppen und mit dem neuen Ausdruck neu anlegen (Postgres erlaubt kein `ALTER` auf einem GENERATED-Spalten-Body). Vorlagen: `pc20260528` (DROP+ADD) und `pc20260529` (atomic-swap).

**Context Window:**
Erweitert jeden Treffer-Chunk um benachbarte Chunks aus demselben Dokument für mehr Kontext. Bei `RAG_CONTEXT_WINDOW=1` wird ein Chunk links und rechts hinzugefügt. Deduplizierung verhindert doppelte Chunks wenn benachbarte Chunks beide Treffer sind.

---

### Folder Auto-Ingest (Watch-Folder → KB + Paperless)

Ein dedizierter Filesystem-MCP-Server überwacht Ordner (lokal/SMB/NFS) und
**PUSHT** neue Dateien per REST an `POST /api/folder-ingest/document` (Bearer).
Das Backend mountet die Shares NICHT — die Bytes reisen im Multipart-Body. Siehe
`docs/FOLDER_INGEST.md`. Off by default; flag-aus = byte-identisch.

```bash
FOLDER_INGEST_ENABLED=false           # Feature-Schalter (Push-Route + internal.ingest_file)
FOLDER_INGEST_KB_NAME=Eingang         # Ziel-Knowledge-Base (wird bei Bedarf angelegt)
FOLDER_INGEST_TARGET_USER=            # Owner der auto-abgelegten Dokumente (Username/ID; leer → Admin/erster User)
FOLDER_INGEST_DEFAULT_TIER=0          # Circle-Tier beim Anlegen (0=self … 4=public)
FOLDER_INGEST_TO_PAPERLESS=true       # zusätzlich in Paperless ablegen
FOLDER_INGEST_NOTIFY_ON_FILED=true    # Bestätigungs-Notification nach Ablage

# Async Paperless-Reconciler (Design Z): der Push legt paperless_state='pending' an
# und gibt sofort zurück; das eigentliche Ablegen läuft im document-worker
# (post_document_ingest-Hook, der dessen Docling-OCR wiederverwendet). Dieser
# periodische Backend-Reconciler (services/paperless_reconciler.py) RE-ENQUEUED nur
# die Nachzügler, die 'pending' geblieben sind, als 'paperless_refile'-Worker-Tasks —
# er führt selbst KEIN Docling aus (das lief zuvor im Backend und sprengte dessen
# 6-Gi-Limit). Der Push wartet NIE inline auf die Paperless-Runde auf einer
# Pool-Verbindung (das war die Outage vom 2026-07-01). Läuft, wenn folder- ODER
# email-ingest→Paperless an ist.
PAPERLESS_RECONCILER_INTERVAL=120              # Sekunden zwischen Ticks
PAPERLESS_RECONCILER_BATCH=25                  # pending-Dokumente pro Tick re-enqueued
PAPERLESS_RECONCILER_REFILE_GRACE_SECONDS=300  # Karenz, bevor ein completed+pending-Doc als Nachzügler gilt (rennt nicht mit dem initialen Filing-Hook)
PAPERLESS_RECONCILER_REFILE_LEASE_SECONDS=900  # Redis-Lease pro Doc: nur ein Refile-Versuch gleichzeitig; läuft ab → Retry (verhindert Re-Enqueue-Churn)
```

Wiederverwendet `MAX_FILE_SIZE_MB`, `ALLOWED_EXTENSIONS`, `UPLOAD_DIR` (siehe RAG/Upload).
Der Bearer-Token liegt revozierbar in `SystemSetting` (nicht in `.env`) — per
`POST /api/folder-ingest/token` (Admin, `settings.manage`) erzeugen/rotieren. Der
MCP prüft die Konfig-Ausrichtung beim Start via `GET /api/folder-ingest/health`.

Der Push wird durch `API_RATE_LIMIT_INGEST` begrenzt (siehe *REST API Rate Limiting*) —
nicht durch das strengere `API_RATE_LIMIT_DEFAULT`, da der token-authentifizierte
MCP-Push durch sein eigenes Push-Concurrency-Semaphor gebändigt wird.

**Der Filesystem-MCP** (`filesystem-mcp`-Image) hat eigene Env-Vars (nicht Backend):
```bash
FILES_MAX_CONCURRENT_PUSHES=4         # parallele Pushes über alle Roots + Retries (Flood-Schutz)
FILES_HEALTH_POLL_SECONDS=30          # Backend-Health-Poll; bei down→up wird pro Root re-reconciled
```

**Defaults:**
- `FOLDER_INGEST_ENABLED`: `false`
- `FOLDER_INGEST_KB_NAME`: `Eingang`
- `FOLDER_INGEST_TARGET_USER`: `""` (leer → Admin/erster User)
- `FOLDER_INGEST_DEFAULT_TIER`: `0`
- `FOLDER_INGEST_TO_PAPERLESS`: `true`
- `FOLDER_INGEST_NOTIFY_ON_FILED`: `true`
- `PAPERLESS_RECONCILER_INTERVAL`: `120` · `PAPERLESS_RECONCILER_BATCH`: `25` · `PAPERLESS_RECONCILER_REFILE_GRACE_SECONDS`: `300` · `PAPERLESS_RECONCILER_REFILE_LEASE_SECONDS`: `900`
- `FILES_MAX_CONCURRENT_PUSHES`: `4` · `FILES_HEALTH_POLL_SECONDS`: `30`

---

### Email Auto-Ingest (Watch-Mailbox → KB + Paperless)

Der dedizierte `renfield-mcp-email-ingest`-Watcher überwacht IMAP-Postfächer per
**IMAP IDLE** (event-driven, KEIN Polling) und **PUSHT** die Anhänge neuer Mails
per REST an `POST /api/email-ingest/document` (Bearer). Das Backend hält die
IMAP-Credentials NICHT — der Watcher schickt nur eine Routing-`mailbox_id`, nie
Tier/Owner. Das Backend löst pro Mailbox **server-autoritativ** `mailbox_id →
owner/tier/kb` auf (ein geleakter Push-Token kann das Tier also nicht eskalieren).
Siehe `docs/EMAIL_INGEST.md`. Off by default; flag-aus = byte-identisch.

```bash
EMAIL_INGEST_ENABLED=false            # Feature-Schalter (Push-Route)
# Routing-Tabelle (server-autoritativ), JSON-String, ein Eintrag pro Mailbox.
# id=Routing-Key (KEIN Credential), owner=Username/ID (leer → ownerless),
# tier=Circle-Tier 0-4, kb=Ziel-Knowledge-Base (wird bei Bedarf angelegt).
EMAIL_INGEST_MAILBOXES_JSON=          # z.B. [{"id":"buchhaltung","owner":"evdb","tier":0,"kb":"xidra"}]
EMAIL_INGEST_TO_PAPERLESS=true        # Anhänge zusätzlich in Paperless ablegen
```

Wiederverwendet `MAX_FILE_SIZE_MB`, `ALLOWED_EXTENSIONS` (siehe RAG/Upload) und die
gesamte folder-ingest-Bridge (`services/folder_ingest.py`). Der Bearer-Token liegt
revozierbar in `SystemSetting` (eigener Key, getrennt vom folder-ingest-Token) —
per `POST /api/email-ingest/token` (Admin, `settings.manage`) erzeugen/rotieren.
Die IMAP-Zugangsdaten + die zu überwachenden Postfächer (`mailboxes.yaml`, nur
Verbindung + Routing-`id`, KEIN owner/tier/kb) leben ausschließlich im Watcher.

**Defaults:**
- `EMAIL_INGEST_ENABLED`: `false`
- `EMAIL_INGEST_MAILBOXES_JSON`: `""` (leer → keine Mailbox geroutet; unbekannte `mailbox_id` → `failed`)
- `EMAIL_INGEST_TO_PAPERLESS`: `true`

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

# Memory→KG Bridge (Structured Memory Phase 3) — opt-in, dark by default
MEMORY_KG_BRIDGE_ENABLED=false              # Memory-Subjekte an kanonische KG-Entitaeten binden (save-time + entity-augmentiertes Retrieval)
MEMORY_RETRIEVAL_SUBJECT_UNION_LIMIT=5      # Max deterministische subjekt-verlinkte Memories pro Turn (1-50)
MEMORY_SUBSUME_TO_KG=false                  # Phase 3-subsume: zerlegbare Fakten (category=fact + Subjekt) NUR im KG, kein flaches Duplikat. Aggressiv, opt-in.
```

**Memory→KG Bridge (Phase 3):** Wenn `MEMORY_KG_BRIDGE_ENABLED=true`, verlinkt der
Hintergrund-Extraktionspfad zerlegbare Memories (`fact`/`preference`) mit ihrer
kanonischen KG-Entitaet (`conversation_memories.subject_entity_id`) — typ- und
tier-bewusst (`resolve_entity(create_tier=memory.tier, match_entity_type=True)`),
nie im synchronen Turn. Retrieval wird entity-augmentiert: im Query genannte
Entitaeten (Exact+Surface-Form, kein LLM) ziehen ihre subjekt-verlinkten Memories
deterministisch in die Embedding-Treffer (similarity-Floor, eigenes Limit,
canonical_id-Tombstone-Chase) — **immer durch denselben `circle_sql`-Filter**.
Bestandsdaten: `python bin/backfill_subject_entity_ids.py --dry-run` (Schaetzung,
keine Writes) → `--commit`. Aus = Retrieval/Extraktion byte-identisch zu vorher.

**Subsume (`MEMORY_SUBSUME_TO_KG`, Phase 3-subsume, aggressiv):** Wenn `true`, werden
zerlegbare `fact`-Memories mit Subjekt **gar nicht** mehr flach gespeichert — sie
leben nur noch im KG (Entitaeten + Relationen, die der KG-Hook im selben Turn
extrahiert). Praeferenzen/Instruktionen/Kontext bleiben flach. **Risiko:** ein Fakt,
dessen Objekt keine benannte Entitaet ist (z. B. „Anna ist müde"), wird ggf. nicht
als KG-Relation erfasst und geht verloren. Erst aktivieren, wenn die Fakt-Erfassung
der KG-Extraktion an echten Transkripten validiert ist. Aus (default) = unveraendert.

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

### Procedural Skills (Self-Learning Phase 1)

```bash
# Master-Schalter — ohne dies passiert nichts
SKILLS_ENABLED=false

# Auto-Extraktion nach Agent-Turns
SKILL_EXTRACT_ENABLED=true               # LLM-Skill-Extraktion nach komplexen Turns
SKILL_EXTRACT_MIN_TOOL_CALLS=3           # Schwellwert "komplexer Turn"
SKILL_EXTRACT_MODEL=                      # Leer = ollama_chat_model

# Prompt-Injection — gelernte Skills in den Agent-Prompt einfuegen
SKILL_INJECT_ENABLED=true
SKILL_INJECT_TOP_K=3                      # Max injizierte Skills pro Turn
SKILL_INJECT_SIMILARITY_THRESHOLD=0.75   # Min cosine similarity

# Auto-Demote — wiederholt fehlgeschlagene Skills deaktivieren
SKILL_AUTO_DEMOTE_THRESHOLD=5            # Failures bis zum Check
SKILL_AUTO_DEMOTE_SUCCESS_RATE=0.10      # success_rate < dieser Wert -> deaktivieren

# Seed-Skills aus src/backend/seed_skills/*.md beim Boot laden
SKILL_SEED_LOAD_ON_BOOT=true
SKILL_SEED_DIRECTORY=seed_skills          # Relativ zu src/backend/
```

**Verhalten:**
Wenn `SKILLS_ENABLED=true`, laeuft nach jedem Agent-Turn ein Background-Task: er prueft die Trace-Heuristik (>= `SKILL_EXTRACT_MIN_TOOL_CALLS` erfolgreiche Tool-Calls, mehrere unterschiedliche Tools, sauberer final_answer) und schickt erfolgreiche Traces an den `SkillExtractor`-LLM-Call. Liefert dieser ein JSON-Objekt mit `{title, body_md, trigger_examples, tool_sequence}`, wird die Skill in `procedural_skills` (Atom-Typ `procedural_skill`, Owner-Tier `self`) gespeichert.

Bei zukuenftigen Anfragen sucht der Agent vor dem LLM-Call mit dem User-Message-Embedding nach den Top-K aktiven Skills (eigene + public seeds) und injiziert sie als `{learned_skills}`-Block in den Prompt — analog zur bestehenden `{tool_corrections}`-Injection. Bei jedem Turn der eine Skill nutzt, wird `success_count` oder `failure_count` aktualisiert; Skills mit ueberwiegend Fehlschlaegen werden automatisch deaktiviert (ausser `pinned=true`).

Owner-Sichtbarkeit ueber `/api/skills` (CRUD + pin/unpin + Tier-Aenderung).

---

### Trajectory Capture (Self-Learning Phase 2)

```bash
# Master-Schalter — wenn aus, kein Capture, kein Export, kein Cleanup
TRAJECTORY_CAPTURE_ENABLED=false

# Welche Outcomes erfasst werden (Komma-separiert)
TRAJECTORY_CAPTURE_OUTCOMES=success,tool_fail

# Auto-Cleanup
TRAJECTORY_RETENTION_DAYS=30                  # nicht-flagged Rows werden aelter geloescht
TRAJECTORY_CLEANUP_INTERVAL=86400             # Sekunden zwischen Cleanup-Laeufen (default 1d)
TRAJECTORY_MAX_PER_USER=10000                 # Soft-Cap; aelteste nicht-flagged Rows werden gedroppt

# Phase-4-Vorbereitung — wenn true, exportiert /export.jsonl nur Rows
# mit gesetztem redacted_payload. v1 schreibt nie redacted_payload, dh
# bei =true bleibt der Export leer (kontrollierter Privacy-Gate).
TRAJECTORY_REDACT_PII=false
```

**Verhalten:**
Wenn `TRAJECTORY_CAPTURE_ENABLED=true` und `SKILLS_ENABLED=true`, persistiert der Post-Turn-Background-Task in `agent_service.py` nach jedem Agent-Turn die vollstaendige Trace (`user_message`, `tools_available`, `steps[]`, `final_answer`, Outcome) als JSON in `agent_trajectories`. Outcomes werden ueber `outcome_from_steps()` abgeleitet:
- `success` — final_answer + keine Tool-Fehler
- `tool_fail` — final_answer + mindestens ein fehlgeschlagener Tool-Call (Agent hat trotzdem geantwortet)
- `abort` — kein final_answer (Loop-Exhaustion, Circuit-Breaker, Timeout)

Nur Outcomes aus `TRAJECTORY_CAPTURE_OUTCOMES` werden erfasst.

Wenn der Turn eine neue Skill extrahiert hat, wird die Trajectory automatisch mit `flagged_for_retention=True` markiert — der Cleanup-Scheduler ueberspringt sie. Gold-Beispiele fuer spaeteres Fine-Tuning.

Admin-only Export-Endpunkt: `GET /api/trajectories/export.jsonl` streamt das gesamte Corpus als Line-Delimited-JSON. Filter via Query-Parametern (`outcome`, `since_days`, `flagged_only`, `require_redacted`).

---

### Tool Health Tracking (Self-Learning Phase 3)

```bash
# Master-Schalter — wenn aus, kein Counter-Update, keine Warnings
TOOL_HEALTH_TRACKING_ENABLED=false

# Prompt-Injection
TOOL_HEALTH_WARN_ENABLED=true                # {tool_health_warnings}-Block einfuegen
TOOL_HEALTH_WARN_MIN_USES=5                  # Min Tool-Calls vor Warnung
TOOL_HEALTH_WARN_SUCCESS_RATE=0.5            # Warnung wenn rate < dieser Wert
TOOL_HEALTH_WARN_TOP_K=3                     # Max gleichzeitige Warnungen
```

**Verhalten:**
Jeder `tool_result` Schritt im Agent-Loop bumpst pro (user_id, tool_name) entweder `success_count` oder `failure_count` in `tool_outcome_stats`. Die letzte Fehlermeldung wird mitgesichert (`last_failure_summary`, max 500 Zeichen).

Beim Prompt-Build wird fuer den aktuellen User die Liste der Tools geladen, die ueber `TOOL_HEALTH_WARN_MIN_USES` Aufrufe haben UND deren Success-Rate unter `TOOL_HEALTH_WARN_SUCCESS_RATE` liegt. Die Top-K (sortiert nach Fehlern absteigend) werden als `{tool_health_warnings}`-Block in den Agent-Prompt injiziert — analog zu `{tool_corrections}` und `{learned_skills}`.

Counter sind **pro User**, nicht global — ein Tool das fuer Alice gut funktioniert aber bei Bob immer scheitert (Permission-Gate fehlt) verschmutzt nicht Alices Prompt.

Admin-only Endpunkte:
- `GET /api/tool-health` — Listing der jüngsten (user, tool) Stats
- `GET /api/tool-health/warnings/{user_id}` — Vorschau auf den Warnungs-Block den der User aktuell sehen wuerde

---

### Skill Curator (Self-Learning Phase 4)

```bash
# Master-Schalter — wenn aus, kein Scheduler-Run, kein /curator/run-Endpunkt
SKILL_CURATOR_ENABLED=false

# Scheduler
SKILL_CURATOR_INTERVAL=86400                  # Sekunden zwischen Laeufen (default 1d)

# Duplikat-Merge
SKILL_CURATOR_DUPLICATE_THRESHOLD=0.92        # Cosine-Sim ab wann zwei Skills als Duplikat gelten
SKILL_CURATOR_MAX_MERGES_PER_RUN=20           # Safety-Cap pro Lauf

# Stale-Archivierung
SKILL_CURATOR_STALE_DAYS=90                   # Tage seit last_used_at nach denen "stale"
SKILL_CURATOR_STALE_SUCCESS_RATE=0.3          # Erfolgsrate unter der archiviert wird
SKILL_CURATOR_MIN_USES_TO_CONSIDER_STALE=3    # Untere Schwelle: nicht jede selten genutzte Skill ist gleich stale
```

**Verhalten:**
Wenn `SKILL_CURATOR_ENABLED=true` (und `SKILLS_ENABLED=true`), startet ein Background-Scheduler der pro `SKILL_CURATOR_INTERVAL` Sekunden ueber alle Owner mit aktiven non-seed Skills iteriert und fuer jeden `SkillCuratorService.run_for_user(user_id)` ausfuehrt. Zwei Phasen:

1. **Duplicate-Dedupe**: pgvector-Self-Join findet Skill-Paare desselben Users mit Cosine-Similarity >= `SKILL_CURATOR_DUPLICATE_THRESHOLD`. Pro Paar wird der "Winner" gewaehlt (hoehere Success-Rate gewinnt, tie-break auf Usage-Count und last_used_at), Trigger werden zusammengefuehrt (dedupliziert, max 10), Outcome-Counter ueberfuehrt, Winner-`version` gebumpt, Winner-Embedding neu berechnet. Der Loser wird `is_active=False` + `merged_into_id=<winner.id>` markiert (Audit-Trail bleibt).

2. **Stale-Archivierung**: Skills die >= `SKILL_CURATOR_STALE_DAYS` Tage ungenutzt sind, mindestens `SKILL_CURATOR_MIN_USES_TO_CONSIDER_STALE` Aufrufe haben UND eine Success-Rate unter `SKILL_CURATOR_STALE_SUCCESS_RATE` werden soft-archiviert. `pinned=true` skips immer.

Manueller Trigger: `POST /api/skills/curator/run` (admin-only). Optional `{"user_id": <id>}` im Body fuer einen einzelnen User.

---

### KG Entity Reconciler (Structured Memory)

Periodischer Per-User-Lauf, der near-duplicate KG-Entitaeten zusammenfuehrt
(das Pendant zum Skill-Curator, fuer den Knowledge Graph). Opt-in.

```bash
# Master-Schalter
KG_RECONCILER_ENABLED=false

# Scheduler
KG_RECONCILER_INTERVAL=86400                  # Sekunden zwischen Laeufen (default 1d)

# Kandidaten + Auto-Merge
KG_RECONCILER_CANDIDATE_THRESHOLD=0.85        # Cosine ab wann ein Paar ueberhaupt betrachtet wird
KG_RECONCILER_AUTO_MERGE_THRESHOLD=0.95       # Same-Tier-Auto-Merge-Schwelle (>= candidate)
KG_RECONCILER_MAX_PER_RUN=50                  # Safety-Cap pro User pro Lauf
KG_RECONCILER_EMBED_BACKFILL_PER_RUN=50       # Null-Embedding-Entitaeten pro Lauf nach-einbetten (0 deaktiviert)

# KG-Konflations-Tripwire (read-only Fruehwarnung) — opt-in, mutiert NIE
KG_CONFLATION_MONITOR_ENABLED=false           # Periodischer Scan: distinct-name same-type Paare >= Schwelle
KG_CONFLATION_MONITOR_INTERVAL=86400          # Sekunden zwischen Scans (default 1d)
KG_CONFLATION_MONITOR_THRESHOLD=0.85          # Cosine, ab der ein distinct-name same-type Paar gemeldet wird
KG_CONFLATION_MONITOR_MAX_PAIRS=100           # Cap auf gemeldete Paare pro User pro Scan

# Graph-Expansion-Retrieval (Phase 4, post-RRF) — opt-in, aus = byte-identisch
GRAPH_EXPANSION_ENABLED=false                 # Nach RRF 1-2 Hops von den fused kg_node-Pivots laufen (PolymorphicAtomStore)
GRAPH_EXPANSION_MAX_PIVOTS=8                   # Max fused kg_node-Pivots zum Expandieren
GRAPH_EXPANSION_MAX_HOPS=2                     # Traversal-Tiefe (1-3)
GRAPH_EXPANSION_MAX_EXPANDED=15               # Cap auf zusaetzliche Nachbar-Atome (Hub-Flood-Schutz)
```

**Graph-Expansion (Phase 4):** Wenn `GRAPH_EXPANSION_ENABLED=true`, expandiert
`PolymorphicAtomStore.query` **nach** der RRF-Fusion die obersten `kg_node`-Pivots
1-`GRAPH_EXPANSION_MAX_HOPS` Hops ueber `kg_relations` (level-synchrone BFS →
korrekte Min-Hop-Distanz; Circle-Filter pro Hop; Frontier-Cap; **leak-sichere
Kanten** nur wenn beide Endpunkte sichtbar; Decay = pivot/(1+hop); Cap
`GRAPH_EXPANSION_MAX_EXPANDED`). Die zusaetzlichen Nachbar-Atome tragen
`payload.expanded=true`+`hop`. Einzelner Insertion-Point (kein Doppel-Work), Decay
ueberlebt. Aus (default) = `query` byte-identisch. (Der Agent-String-Pfad
`get_relevant_context` profitiert erst, wenn er auf den fused-Pfad umgestellt wird
— offener Follow-up in `TODOS.md`.)

**KG-Konflations-Tripwire:** Wenn `KG_CONFLATION_MONITOR_ENABLED=true`, laeuft
ein Background-Scan pro `KG_CONFLATION_MONITOR_INTERVAL` Sekunden und meldet
(WARNING-Log + Gauge `renfield_kg_conflation_candidates`) **distinct-name,
same-type, same-tier NICHT-Personen**-Paare, deren Cosine >=
`KG_CONFLATION_MONITOR_THRESHOLD` ist — eine entstehende Generischer-Centroid-
Magnet-/Fehl-Embedding-Situation in einem Typ, in dem `resolve_entity` noch
embedding-matched. **Personen sind ausgeschlossen** (primaer ODER Multi-Typ):
Personennamen clustern intrinsisch ≥ Schwelle (gemessen Jutta~Anna 0.894), und
`resolve_entity` matched Personen ohnehin nicht per Embedding — ein nahes
Personen-Paar kann nicht falten, ein Treffer waere Dauer-Rauschen. Der Scan
**mutiert nie** (echte Dubletten sind Sache des Reconcilers); erwarteter Wert
ist 0. On-demand ohne Scheduler: `python bin/scan_kg_conflation.py [--user-id N]
[--threshold 0.9]`. Hintergrund: der Personen-Magnet-Bug (Entitaet #11, 127
Mentions) entstand genau so. `services/kg_conflation_monitor.py`.

**Verhalten:**
Wenn `KG_RECONCILER_ENABLED=true`, iteriert ein Background-Scheduler pro
`KG_RECONCILER_INTERVAL` Sekunden ueber alle User mit aktiven, kanonischen
Entitaeten und ruft `KgReconcilerService.run_for_user(user_id)`. Jeder Lauf ist
per-User durch einen nicht-blockierenden Advisory-Lock serialisiert (ein zweiter
ueberlappender Lauf findet den Lock gehalten und endet als No-op). Zu Beginn
werden bis zu `KG_RECONCILER_EMBED_BACKFILL_PER_RUN` aktive Entitaeten ohne
Embedding nach-eingebettet — sonst blieben sie im Self-Join unsichtbar. Ein
halfvec-Embedding-Self-Join findet dann Duplikat-Paare desselben Users (Cosine
>= `KG_RECONCILER_CANDIDATE_THRESHOLD`); Winner = mehr Erwaehnungen, tie-break
aelteres `first_seen_at`. Dann:

1. **Same-Tier + Cosine >= `KG_RECONCILER_AUTO_MERGE_THRESHOLD`** → automatischer
   Merge via `merge_entities` (absorbiert surface_forms/Multi-Typ, reparentiert
   Relationen, Tier = MIN, tombstoned den Loser mit `canonical_id`).
2. **Cross-Tier ODER Grauzone** (aehnlich, aber unter der Auto-Schwelle) →
   ein `kg_merge_proposals`-Eintrag fuer die Owner-Review (`/brain/review`).
   Wird NIE still gemergt — eine Verschmelzung darf Sichtbarkeit nie erhoehen.

Idempotent: Paare mit bereits offenem Proposal werden ausgeschlossen
(Partial-Unique auf `(loser, winner) WHERE status='pending'`). Wird ein
Proposal genehmigt, dessen Gegenseite ein paralleles Approve schon verschmolzen
hat, ist der Merge ein No-op und das Proposal schliesst als `superseded` (statt
irrefuehrend `approved`).

Manueller Trigger: `POST /api/knowledge-graph/reconciler/run` (KG_VIEW — wirkt
nur auf den eigenen Graphen des Aufrufers).
Review-Routen: `GET /api/knowledge-graph/merge-proposals`,
`POST …/{id}/approve` (optional `{"winner_id": <id>}` als Survivor-Override),
`POST …/{id}/reject`.

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
ADVERTISE_HOST=renfield.local

# URL-Schema (http|https) für die TTS-Audio-URL, die Renderer abrufen
ADVERTISE_SCHEME=http

# Port für ADVERTISE_HOST (Standard-Ports 80/443 werden in der URL weggelassen)
ADVERTISE_PORT=80
```

`ADVERTISE_HOST`/`ADVERTISE_SCHEME`/`ADVERTISE_PORT` bauen die URL, die das
Backend an DLNA-Renderer und HA Media Player übergibt, damit diese die
TTS-Audiodatei (`/api/voice/tts-cache/{id}.wav`) abrufen
(`AudioOutputService._get_backend_url()`).

**Defaults:**
- `ADVERTISE_HOST`: None (muss gesetzt werden für HA Media Player / DLNA Output)
- `ADVERTISE_SCHEME`: `http` (Literal `http|https`; ein Tippfehler wird beim Start abgelehnt)
- `ADVERTISE_PORT`: `8000`

**Standard-Ports 80 und 443 werden in der URL immer weggelassen** — so kann ein
`ADVERTISE_PORT`, das nicht zum Schema passt, keine kaputte URL erzeugen. Nur
Nicht-Standard-Ports (8000, 8443) erscheinen.

**Produktion (k8s, aktuell):** `ADVERTISE_HOST=renfield.local`,
`ADVERTISE_SCHEME=http`, `ADVERTISE_PORT=80` → `http://renfield.local/api/voice/tts-cache/{id}.wav`.
Die `backend-tts-cache-http` IngressRoute (eigener `web`-Entrypoint-Route ohne
`http→https`-Redirect) bedient diesen Pfad plain. **Bewusst http, nicht https:**
Samsung-TVs akzeptieren das self-signed Zertifikat nicht; http funktioniert auf
allen Renderern.

**Pro-Renderer-Status (gemessen über http://renfield.local):**
- **Linn / openHome + Samsung TV (Q60CA / 8 Series):** funktionieren nativ — lösen
  `renfield.local` per Router-DNS auf, kein Geräte-Setup. (Samsung erst nach dem
  DLNA-Compliance-Fix: HEAD-Support + `audio/x-wav` + `.wav` — vorher UPnP 716.)
- **HiFiBerry (gmediarender/gstreamer):** braucht nur den `/etc/hosts`-Eintrag
  `192.168.1.230 renfield.local` (systemd-resolved fängt `.local` als mDNS ab) —
  via `provision-hifiberry.yml`. **Über http keine CA nötig** (die war nur für die
  https-Episode da).
- **55" Signage Flip:** eigener Quirk (404 im dlna-mcp-Confirm), separat.

Details + Messungen: `docs/MESSAGE_RELAY.md` → „TTS-Audio-Auslieferung an Renderer".

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

**Default:** `""` (leer = alle Proxies vertraut, `X-Forwarded-For[0]` wird gelesen, rückwärtskompatibel — **spoofbar**)

**Wann setzen:** Hinter einem Reverse Proxy (nginx, Traefik), um die **spoof-sichere** Client-IP-Auflösung zu aktivieren. Ist `TRUSTED_PROXIES` gesetzt, werden `X-Forwarded-For` / `X-Real-IP` nur gelesen, wenn der direkte Peer eine Trusted-Proxy-CIDR ist, und die Client-IP wird durch **Rechts-nach-Links-Durchlauf** der XFF-Kette ermittelt (die rechteste Adresse, die **kein** Trusted Proxy ist) — so kann keine gefälschte XFF-Identität eingeschleust werden.

> **Hinweis (#693):** Der leere Default bleibt bewusst rückwärtskompatibel (liest `X-Forwarded-For[0]`), damit per-Client-Limiting hinter einem Proxy out-of-the-box funktioniert — ein Umschalten auf die direkte Socket-IP würde alle Clients in den einen IP-Bucket des Proxys zusammenfallen lassen (cluster-weiter Rate-Limit-DoS). Die Härtung ist opt-in über `TRUSTED_PROXIES` (z. B. beim Auth-on-Cutover).

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
API_RATE_LIMIT_INGEST=1200/minute     # folder-/email-ingest PUSH-Routen (token-auth, MCP-Semaphor bändigt Durchsatz)

# Storage-Backend des Rate-Limiters (slowapi/limits-URI)
API_RATE_LIMIT_STORAGE_URI=memory://  # per-Pod; für per-Cluster: ${REDIS_URL}
```

**`API_RATE_LIMIT_STORAGE_URI`** (Default `memory://`): Zähler sind standardmäßig
pro Pod. Ein Multi-Replica-Deployment zählt zu niedrig (jeder Pod limitiert
eigenständig) — auf die Redis-URL setzen (z. B. `redis://redis:6379`) für
geteiltes **per-Cluster**-Limiting, sobald mehr als ein Backend-Pod läuft.

### Account Lockout (Login)

```bash
LOGIN_LOCKOUT_ENABLED=true            # Pro-Username-Sperre nach wiederholten Fehlversuchen
LOGIN_LOCKOUT_MAX_ATTEMPTS=5          # Fehlversuche im Fenster bis zur Sperre
LOGIN_LOCKOUT_WINDOW_SECONDS=900      # rollierendes Fehler-Fenster
LOGIN_LOCKOUT_DURATION_SECONDS=900    # Sperrdauer nach Auslösung
```

Ergänzt das per-IP-Rate-Limit: sperrt einen **Username** nach wiederholten
Fehl-Logins (stoppt Credential-Stuffing über wechselnde Quell-IPs). Redis-basiert
(`services/login_lockout.py`), **fail-OPEN** bei Redis-Ausfall (ein Ausfall darf
nicht den ganzen Haushalt aussperren). Eine gesperrte Anmeldung liefert dasselbe
opake 401 wie falsche Zugangsdaten (kein Enumerations-Oracle); sichtbar nur über
Log + `renfield_login_failure_total{reason="locked_out"}`.

`API_RATE_LIMIT_INGEST` gilt für die `/document`-Push-Routen von folder- und
email-ingest. Diese werden vom vertrauenswürdigen, Bearer-authentifizierten MCP
(eine IP) getroffen, dessen eigenes Push-Concurrency-Semaphor der eigentliche
Durchsatz-Regler ist — nicht das für ungetrusteten User-API-Missbrauch gedachte
`API_RATE_LIMIT_DEFAULT`. Seit die Paperless-Ablage entkoppelt ist (Design Z) kehrt
der Push in ms zurück, ein Watch-Folder-Backlog burstet also weit über 100/min und
lief in 429 (der Drain blieb stehen). Das großzügige Ceiling lässt das Semaphor den
legitimen Durchsatz steuern und deckelt trotzdem einen geleakten Token.

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

**Default:** `768` (Code-Default, passend für `nomic-embed-text`). Produktion nutzt `2560` für `qwen3-embedding:4b` — siehe `k8s/configmap.yaml`. Bei Modellwechsel muss der Vektor-Index neu angelegt werden.

---

### Deployment-Posture (`RENFIELD_ENV`)

```bash
# Deployment-Marker: development (Default) | dev | test | staging | production | prod
RENFIELD_ENV=development
```

Jetzt ein **getracktes Settings-Feld** (#697, vorher nur via `os.getenv` gelesen), damit es introspektierbar/dokumentiert ist und in der ConfigMap neben den übrigen Posture-Keys gesetzt werden kann. Ein Real-Deployment-Wert (`production`/`prod`/`staging`) **scharfschaltet die JWT-Key-Boot-Sperre** auch bei ausgeschalteter Auth (#692) — vorher einen starken `SECRET_KEY` (>= 32 Zeichen) bereitstellen, sonst bootet das Backend nicht.

**Konsistenz-Assertion (#697, `assert_auth_config_consistency`):**
- **HARTER Boot-Fehler:** `AUTH_ENABLED=true` mit `WS_AUTH_ENABLED=false` — der WebSocket-Chat wäre unauthentifiziert und der WS-Session-Ownership-Check (#657) still deaktiviert. Beide Flags müssen gemeinsam an.
- **WARN (nicht fatal):** `AUTH_ENABLED=true` mit `CORS_ORIGINS='*'`; `RENFIELD_ENV=production` mit `ALLOW_REGISTRATION=true`.
- Bei der aktuellen Auth-off-Posture (alles false) greift nichts — byte-identisch.

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

# === Pluggable auth provider registry (ebongard/renfield#591) ===
# Per-provider credential-walk timeout; a provider exceeding this is
# skipped fail-open (WARNING + auth_provider_unreachable_total counter).
AUTH_PROVIDER_TIMEOUT_SECONDS=10.0

# LDAP credential provider (authn only — no group→role mapping yet).
# Default off → DB-only behavior unchanged.
LDAP_AUTH_ENABLED=false
LDAP_URL=                              # ldaps://host:636 or ldap://host:389
LDAP_BIND_DN=                          # service account DN for the user search
LDAP_BIND_PASSWORD=
LDAP_AUTH_USER_BASE_DN=                # subtree searched for the user
LDAP_AUTH_USER_FILTER=(uid={username}) # {username} is substituted (RFC4515-escaped)
LDAP_CONNECT_TIMEOUT=5
LDAP_RECEIVE_TIMEOUT=10

# Social redirect providers — all ship disabled; enabling is config-only
# (no redeploy), off the credential critical path.
OAUTH_GOOGLE_ENABLED=false
OAUTH_GOOGLE_CLIENT_ID=
OAUTH_GOOGLE_CLIENT_SECRET=
OAUTH_GOOGLE_REDIRECT_URI=
OAUTH_GITHUB_ENABLED=false
OAUTH_GITHUB_CLIENT_ID=
OAUTH_GITHUB_CLIENT_SECRET=
OAUTH_GITHUB_REDIRECT_URI=
OAUTH_APPLE_ENABLED=false
OAUTH_APPLE_CLIENT_ID=                 # Apple Services ID
OAUTH_APPLE_TEAM_ID=
OAUTH_APPLE_KEY_ID=
OAUTH_APPLE_PRIVATE_KEY=
OAUTH_APPLE_REDIRECT_URI=
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
- `AUTH_PROVIDER_TIMEOUT_SECONDS`: `10.0`
- `LDAP_AUTH_ENABLED`: `false` · `LDAP_AUTH_USER_FILTER`: `(uid={username})` · `LDAP_CONNECT_TIMEOUT`: `5` · `LDAP_RECEIVE_TIMEOUT`: `10`
- `OAUTH_{GOOGLE,GITHUB,APPLE}_ENABLED`: `false` (all social providers disabled by default — enabling is a config-only change)

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
# Frigate REST URL
FRIGATE_URL=http://frigate.local:5000

# Frigate MQTT broker (für Echtzeit-Events)
FRIGATE_MQTT_BROKER=localhost
FRIGATE_MQTT_PORT=1883
```

**Erforderlich:** Optional
**Format:** `http://<frigate-host>:<port>` für die REST-URL, getrennter MQTT-Broker für Live-Events.

**Defaults:**
- `FRIGATE_MQTT_BROKER`: `localhost`
- `FRIGATE_MQTT_PORT`: `1883`

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

# Home location for the fullscreen kiosk weather tile (/kiosk). City or postal
# code; empty = no weather tile. Env-only (never committed — no real place in git).
# Requires WEATHER_ENABLED=true.
KIOSK_WEATHER_LOCATION=

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

# Samsung Smart TV (Tizen — websocket remote, Wake-on-LAN, DLNA media)
# Agent-only: chat-driven TV control. Dedicated hostNetwork image (renfield-mcp-samsung).
# Ships DARK: the k8s configmap sets this false; flip to true only AFTER the
# samsung-mcp image is built and the one-time TV pairing is done.
SAMSUNG_MCP_ENABLED=true

# Generic output-provider registry for room media/control routing (opt-in/dark).
# When on, room output discovery + dispatch route through the pluggable provider
# registry (built-in renfield/HA + MCP-declared dlna/samsung/sonos via the
# `output_provider:` stanza) instead of the hardcoded 3-source branches. Off =>
# byte-identical legacy routing. See docs/OUTPUT_ROUTING.md + docs/design/output-providers.md.
OUTPUT_PROVIDERS_ENABLED=false
# Per-provider timeout (seconds) for the aggregated available-outputs discover
# fan-out; a provider exceeding it shows DEGRADED (not dropped). Default 5.0.
OUTPUT_PROVIDER_DISCOVER_TIMEOUT=5.0

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

# Parcel Tracking (Multi-Carrier, Direkt-APIs — kein Aggregator)
# DHL/Deutsche Post (gratis, produktiv), UPS + FedEx (OAuth). DPD/Hermes/GLS
# haben keine freie öffentliche API → Web-Deep-Link. Jeder Adapter deaktiviert
# sich selbst ohne Keys (list_carriers zeigt den configured-Status).
TRACKING_ENABLED=true
DHL_API_KEY=                           # DHL Developer Portal App-Key (DHL-API-Key)
UPS_CLIENT_ID=                         # UPS Developer App
UPS_CLIENT_SECRET=
FEDEX_CLIENT_ID=                       # FedEx Developer App
FEDEX_CLIENT_SECRET=
TRACKING_DEFAULT_CARRIER=dhl           # Fallback wenn Carrier nicht aus der Nummer erkennbar
# Optional: API-Basis-URLs überschreiben (Test-Umgebungen)
# DHL_TRACKING_BASE_URL=https://api-eu.dhl.com
# UPS_TRACKING_BASE_URL=https://wwwcie.ups.com       # CIE-Testumgebung
# FEDEX_TRACKING_BASE_URL=https://apis-sandbox.fedex.com
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
| `DHL_API_KEY` | DHL Shipment-Tracking-Unified API-Key (read-only) | `secrets/dhl_api_key` |
| `UPS_CLIENT_SECRET` | UPS OAuth Client Secret (read-only Tracking) | `secrets/ups_client_secret` |
| `FEDEX_CLIENT_SECRET` | FedEx OAuth Client Secret (read-only Tracking) | `secrets/fedex_client_secret` |
| `PRESENCE_WEBHOOK_SECRET` | Shared-Secret für `X-Webhook-Secret` Header bei ausgehenden Presence-Webhooks | `secrets/presence_webhook_secret` |

> Die kanonische Liste inkl. Consumer-Mapping und Upgrade-Hinweise liegt in [`docs/SECRETS_MANAGEMENT.md`](SECRETS_MANAGEMENT.md). Optionale Integration-Secrets (alles ausser den drei Core-Secrets) dürfen als leere Placeholder-Datei existieren — der Stack bleibt startfähig, das Feature deaktiviert sich einfach.

### MCP-Server URLs (nicht-sensitiv, in .env)

```bash
# Home Assistant URL
HOME_ASSISTANT_URL=http://homeassistant.local:8123

# DLNA MCP Server URL (läuft als Host-Service, nicht im Docker)
# Default: http://host.docker.internal:9091/mcp
DLNA_MCP_URL=http://host.docker.internal:9091/mcp

# Samsung TV MCP Server URL (dediziertes hostNetwork-Image, nicht im Backend)
# Default: http://host.docker.internal:9092/mcp ; in k8s: http://samsung-mcp:9092/mcp
SAMSUNG_MCP_URL=http://host.docker.internal:9092/mcp
# Optional: feste TV-IP (überspringt SSDP) + Identität im Pairing-Popup
# SAMSUNG_TV_HOST=192.168.1.47
# SAMSUNG_CLIENT_NAME=Renfield
# Pairing-Token-Persistenz (in k8s: PVC samsung-mcp-state an /state gemountet)
# RENFIELD_STATE_DIR=/state

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

Das Hook-System ermöglicht externen Paketen, sich an definierten Lifecycle-Stellen einzuhängen, ohne dass renfield eine Abhängigkeit zum Plugin hat.

```bash
# Entry-Point für eine Hook-basierte Extension.
# Format: "package.module:callable" — wird beim Startup aufgerufen.
# Leer = deaktiviert (Standard).
PLUGIN_MODULE=

# Mehrere Extensions: komma-separierte Liste von "package.module:callable".
# Wird nach PLUGIN_MODULE geladen und dedupliziert; ein fehlerhaftes Plugin
# wird geloggt und übersprungen, bricht den Startup also nicht ab. Der
# Lade-Ausgang (ok/Fehler) wird pro Spec festgehalten (Kiosk-Health, s. u.).
PLUGIN_MODULES=

# Optional: bindet ein Startup-Plugin an den MCP-Server, den es „backt", damit
# ein fehlgeschlagener Plugin-Load diesen Knoten auf dem Kiosk als DEGRADED
# markiert (erreichbar, aber nicht voll funktionsfähig — z. B. ein Adapter-
# Plugin, dessen Sidecar-MCP-Server zwar verbunden ist). Komma-separiert
# "plugin_prefix=server_name" (Trenner ist `=`, NICHT `:` — ein Plugin-Spec
# enthält selbst einen Doppelpunkt, `:` würde also mis-splitten). Match via
# spec.startswith(prefix), d. h. links funktioniert sowohl der Modul-Präfix
# (`twin_adapter`) als auch der volle Spec (`twin_adapter.plugin:register`).
# Leer = keine solche Bindung (Public-Build unverändert); der Plugin-Name bleibt
# aus dem generischen Default heraus — das Deployment, das das Plugin ausliefert,
# setzt die Bindung.
PLUGIN_MCP_BINDINGS=

# Beispiele
PLUGIN_MODULE=example_pkg.plugin:register
PLUGIN_MODULES=pkg_a.plugin:register,pkg_b.plugin:register
PLUGIN_MCP_BINDINGS=some_adapter=some_server
```

**Defaults:**
- `PLUGIN_MODULE`: `""` (deaktiviert)
- `PLUGIN_MODULES`: `""` (deaktiviert)
- `PLUGIN_MCP_BINDINGS`: `""` (keine Plugin→MCP-Health-Bindung)

**Hook Events:** `startup`, `shutdown`, `register_routes`, `register_tools`, `post_message`, `retrieve_context`

**Hinweis:** Das Hook-System ist der empfohlene Weg für tiefe Integrationen (Kontext-Injektion, Post-Processing, Custom Routes). Für einfache Tool-Integrationen sind MCP-Server weiterhin der bevorzugte Weg.

---

## Tageszeit, LED-Nachtdimmung, Präsenz-Historie & Bluetooth-Scan

```bash
# --- Tag/Nacht-Bewusstsein (services/daypart_service.py) ---
# Der Agent bekommt in jedem Prompt eine ZEITKONTEXT-Zeile (Tageszeit + Wochentag);
# ein 5-Minuten-Watcher feuert bei Übergängen den daypart_changed-Hook. Immer aktiv
# (kein Flag) — die Uhrzeit zu kennen ist immer korrekt. Fenster sind HH:MM lokal.
DAYPART_NIGHT_START=22:00      # Beginn "Nacht"
DAYPART_NIGHT_END=07:00        # Ende "Nacht" (umschlagend über Mitternacht, wenn > start)
DAYPART_EVENING_START=18:00    # Beginn "Abend"
DAYPART_TIMEZONE=              # leer => nutzt PRESENCE_ANALYTICS_TIMEZONE (Default Europe/Berlin), sonst UTC

# --- LED-Nachtdimmung (ha_glue/services/led_dimming_service.py) ---
# Backend-getrieben: bei daypart_changed wird die Helligkeit an alle Satelliten
# über WS gepusht (led_config). register_ack trägt die aktuelle Helligkeit, damit
# ein nachts neu verbindender Satellit gedimmt hochkommt. Symmetrisch: jeder
# Übergang AUS der Nacht stellt LED_DAY_BRIGHTNESS wieder her. Werte 0-31.
LED_DAY_BRIGHTNESS=20
LED_NIGHT_BRIGHTNESS=5

# --- Persistente Präsenz-Historie ---
# presence_events bekommt eine satellite_id-Spalte (Migration pc20260616) + Timeline-
# Routen unter /api/presence/analytics/ + das internal.presence_history Chat-Tool
# ("Wo war X", "Wer war in Raum Y"). Fremduser-Abfragen brauchen ROOMS_MANAGE.
# Die in-memory Live-Präsenz bleibt unberührt. Additiv → Default an.
PRESENCE_HISTORY_ENABLED=true

# --- Bluetooth-Geräte-Scan aus dem Chat ---
# "Scanne die Bluetooth-Geräte" → internal.bluetooth_scan fächert eine Discovery
# an alle Satelliten aus (neues bt_scan_request/bt_scan_result WS-Protokoll, wie
# capture_snapshot). Jeder Satellit: Classic-BT-Inquiry (hcitool scan) + BLE
# (BleakScanner). Backend dedupliziert per MAC, stärkstes RSSI, pro Raum, OUI→Hersteller.
# Nur sichtbare/advertisende Geräte; ~15-30 s. Privacy: zählt alle Geräte im Haus auf
# → Opt-in (Default aus). Das Tool muss in config/agent_roles.yaml der smart_home-Rolle
# stehen (ConfigMap renfield-mcp-config, nicht im Image).
BT_SCAN_ENABLED=false
```

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
SUPPORTED_LANGUAGES=de,en,it
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
