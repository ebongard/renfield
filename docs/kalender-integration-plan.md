# Konsolidierter Plan: Unified Calendar MCP Server

## Context

Renfield weiß aktuell nicht, was der User vorhat. Eine Kalender-Integration macht Renfield zum Tagesbegleiter: Morgen-Briefing mit Terminen, Erinnerungen per TTS auf Satellites, Termin-Erstellung per Sprache.

**Bisherige Google Calendar Integration via n8n** funktioniert nicht mehr (OAuth2-Token abgelaufen, n8n-Login kaputt). Statt nur das Backend auszutauschen, wird eine **Multi-Kalender-Architektur** aufgebaut:

| Kalender | Backend | Protokoll | Zweck |
|----------|---------|-----------|-------|
| Firmenkalender | Exchange 2019 (IONOS) | EWS | Beruflich |
| Familienkalender | Google Calendar | Google API | Familie |
| Vereinskalender | Nextcloud | CalDAV | Verein |

**Architektur-Entscheidung (NEU):** Custom Python MCP Server (`renfield-mcp-calendar`) statt n8n als Bridge. Gleiche Architektur wie `renfield-mcp-mail` (Multi-Account via YAML-Config). Gründe:
- n8n als Bridge hat Wartungsprobleme verursacht (Token-Ablauf, Login-Probleme)
- Drei verschiedene Auth-Mechanismen (EWS NTLM, Google OAuth2, CalDAV Basic) — ein unified Server ist sauberer
- Kein n8n, kein DavMail, keine zusätzliche Infrastruktur

## Use Cases & Mehrwert

| Usecase | Beispiel | Mehrwert |
|---------|----------|----------|
| **Tagesplanung** | "Was steht heute an?" | Überblick über ALLE Kalender, morgens auf dem Satellite |
| **Nächster Termin** | "Wann ist mein nächster Termin?" | Schnelle Orientierung |
| **Verfügbarkeit** | "Bin ich morgen um 15 Uhr frei?" | Planungshilfe bei Anfragen |
| **Termin erstellen** | "Erstelle Familientermin: Zahnarzt morgen 14 Uhr" | Hands-free Terminplanung im richtigen Kalender |
| **Push-Erinnerungen** | "In 30 Minuten: Team-Meeting" (TTS) | Proaktive Ansage auf Satellite/Web |
| **Kalender-spezifisch** | "Was steht im Firmenkalender?" | Gezielter Zugriff auf einzelne Kalender |
| **Cross-Calendar** | "Was steht diese Woche an?" | Merged Events aus allen Kalendern chronologisch |
| **Agent-Kontext** | Agent weiß bei Planungsfragen um Termine | Intelligentere Antworten |

## Architektur

```
Exchange 2019 (IONOS)  ←── EWS (exchangelib) ──┐
Google Calendar        ←── Google API ──────────┼── renfield-mcp-calendar (stdio)
Nextcloud              ←── CalDAV (caldav lib) ─┘         │
                                                    MCP Protocol (stdin/stdout)
                                                           │
                                                    Renfield Backend (MCPManager)
                                                      → mcp.calendar.list_events
                                                      → mcp.calendar.create_event
                                                      → Agent Loop

Push-Erinnerungen (Phase 2):
  NotificationPollerService (Backend, alle 15 min)
    → mcp.calendar.get_pending_notifications()
    → mcp.tasks.get_pending_notifications()      ← Zukunft
    → NotificationService → WebSocket + TTS auf Satellite
```

### Bestehende Infrastruktur (kein Code nötig)

**Notification Webhook (Push-Erinnerungen):**
- `POST /api/notifications/webhook` — Bearer Token Auth, JSON-Payload
- `NotificationService` — Dedup, LLM-Enrichment, DB-Speicherung
- WebSocket-Broadcast + TTS-Synthese via Piper + Audio-Routing an Satellites
- `OutputRoutingService` — Findet besten Lautsprecher pro Raum

## Phasen

### Phase 1: Custom Calendar MCP Server (MVP)

**Ziel:** "Was steht heute an?" funktioniert End-to-End mit allen drei Kalendern.

#### 1a. Neues Repository: `renfield-mcp-calendar`

```
renfield-mcp-calendar/
├── src/
│   └── renfield_mcp_calendar/
│       ├── __init__.py              # Version + main()
│       ├── __main__.py              # python -m entry point
│       ├── server.py                # FastMCP server + unified tools
│       ├── config.py                # YAML config loading + dataclasses
│       └── backends/
│           ├── __init__.py
│           ├── base.py              # CalendarBackend Protocol + CalendarEvent
│           ├── ews.py               # Exchange Web Services (exchangelib)
│           ├── google.py            # Google Calendar API
│           └── caldav_backend.py    # CalDAV (Nextcloud, etc.)
├── tests/
├── pyproject.toml
└── README.md
```

**Dependencies:**
```toml
dependencies = [
    "mcp>=1.26.0",
    "exchangelib>=5.4",
    "google-api-python-client>=2.100",
    "google-auth-oauthlib>=1.2",
    "google-auth-httplib2>=0.2",
    "caldav>=1.4",
    "pyyaml>=6.0",
    "python-dateutil>=2.9",
]
```

**Unified Tool Interface:**

| Tool | Parameter | Beschreibung |
|------|-----------|-------------|
| `list_calendars` | — | Alle konfigurierten Kalender-Accounts auflisten |
| `list_events` | `calendar?`, `start`, `end` | Events aus einem oder allen Kalendern. Ohne `calendar` = alle. |
| `create_event` | `calendar`, `title`, `start`, `end`, `description?`, `location?` | Neuen Termin erstellen |
| `update_event` | `calendar`, `event_id`, `title?`, `start?`, `end?`, ... | Bestehenden Termin ändern |
| `delete_event` | `calendar`, `event_id` | Termin löschen |
| `get_event` | `calendar`, `event_id` | Einzelnes Event mit Details |

**Wichtig:** `list_events` ohne `calendar`-Parameter holt Events aus **allen** Kalendern und merged sie chronologisch.

**Backend Protocol (`backends/base.py`):**

```python
@dataclass
class CalendarEvent:
    id: str
    calendar: str        # Account-Name (work, family, verein)
    title: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    all_day: bool = False

class CalendarBackend(Protocol):
    async def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...
    async def create_event(self, title, start, end, description="", location="") -> CalendarEvent: ...
    async def update_event(self, event_id: str, **kwargs) -> CalendarEvent: ...
    async def delete_event(self, event_id: str) -> bool: ...
    async def get_event(self, event_id: str) -> CalendarEvent: ...
```

**Backend-Spezifika:**

| Backend | Library | Auth | Besonderheiten |
|---------|---------|------|---------------|
| EWS | `exchangelib` | NTLM/Basic (env vars) | Direkte EWS-URL, kein Autodiscover |
| Google | `google-api-python-client` | OAuth2 Desktop Flow | Token auto-refresh, einmalige Browser-Auth |
| CalDAV | `caldav` | Basic Auth (env vars) | Nextcloud, ownCloud, Radicale, etc. |

> **Hinweis:** Microsoft plant EWS-Deprecation Oktober 2026 — für IONOS on-premise erstmal irrelevant.

#### 1b. Konfiguration: `config/calendar_accounts.yaml`

```yaml
calendars:
  # --- Exchange 2019 (Firmenkalender) ---
  - name: work
    label: "Firmenkalender"
    type: ews
    ews_url: "https://exchange2019.ionos.eu/EWS/Exchange.asmx"
    username_env: CALENDAR_WORK_USERNAME
    password_env: CALENDAR_WORK_PASSWORD

  # --- Google Calendar (Familie) ---
  - name: family
    label: "Familienkalender"
    type: google
    calendar_id: "primary"
    credentials_file: "/config/google_calendar_credentials.json"
    token_file: "/data/google_calendar_token.json"

  # --- Nextcloud CalDAV (Verein) ---
  - name: verein
    label: "Vereinskalender"
    type: caldav
    url: "https://nextcloud.example.com/remote.php/dav/calendars/user/verein/"
    username_env: CALENDAR_VEREIN_USERNAME
    password_env: CALENDAR_VEREIN_PASSWORD
```

#### 1c. Renfield-Projekt Integration

**`config/mcp_servers.yaml`** — Calendar-Eintrag ersetzen:
```yaml
  # --- Unified Calendar MCP Server ---
  # Custom Python MCP server supporting Exchange EWS, Google Calendar, and CalDAV.
  # Config: config/calendar_accounts.yaml
  - name: calendar
    transport: stdio
    command: python
    args: ["-m", "renfield_mcp_calendar"]
    enabled: "${CALENDAR_ENABLED:-false}"
    refresh_interval: 300
    env:
      CALENDAR_CONFIG: "${CALENDAR_CONFIG:-/config/calendar_accounts.yaml}"
      CALENDAR_WORK_USERNAME: "${CALENDAR_WORK_USERNAME:-}"
      CALENDAR_WORK_PASSWORD: "${CALENDAR_WORK_PASSWORD:-}"
      CALENDAR_VEREIN_USERNAME: "${CALENDAR_VEREIN_USERNAME:-}"
      CALENDAR_VEREIN_PASSWORD: "${CALENDAR_VEREIN_PASSWORD:-}"
    example_intent: mcp.calendar.list_events
    prompt_tools:
      - list_calendars
      - list_events
      - create_event
    examples:
      de:
        - "Was steht heute in meinem Kalender?"
        - "Was steht im Firmenkalender diese Woche?"
        - "Erstelle einen Familientermin morgen um 14 Uhr: Zahnarzt"
        - "Wann ist das nächste Vereinstreffen?"
        - "Bin ich morgen Nachmittag frei?"
      en:
        - "What's on my calendar today?"
        - "What's in the work calendar this week?"
        - "Create a family appointment tomorrow at 2 PM: Dentist"
```

**`src/backend/requirements.txt`:**
```
renfield-mcp-calendar @ https://github.com/ebongard/renfield-mcp-calendar/archive/refs/heads/main.tar.gz
```

**`docker-compose.yml`** — Volume mounts:
```yaml
backend:
  volumes:
    - ./config/calendar_accounts.yaml:/config/calendar_accounts.yaml:ro
    - ./config/google_calendar_credentials.json:/config/google_calendar_credentials.json:ro
    - calendar-tokens:/data
volumes:
  calendar-tokens:
```

**`.env.example`:**
```bash
# === Calendar (Unified Calendar MCP Server) ===
# CALENDAR_ENABLED=false
# CALENDAR_CONFIG=/config/calendar_accounts.yaml
# Exchange (EWS): CALENDAR_WORK_USERNAME, CALENDAR_WORK_PASSWORD
# Google Calendar: credentials.json + initial OAuth (see docs)
# Nextcloud CalDAV: CALENDAR_VEREIN_USERNAME, CALENDAR_VEREIN_PASSWORD
```

#### 1d. Voraussetzungen (manuell, einmalig)

**Exchange (IONOS):**
- EWS URL: `https://exchange2019.ionos.eu/EWS/Exchange.asmx`
- Benutzername + Passwort als Secrets

**Google Calendar:**
1. Google Cloud Console → Projekt → Google Calendar API aktivieren
2. OAuth2 Credentials → Desktop App → `credentials.json` herunterladen
3. Als `config/google_calendar_credentials.json` ablegen
4. Einmalig: `docker exec -it renfield-backend python -m renfield_mcp_calendar --auth google`

**Nextcloud CalDAV:**
- CalDAV-URL des Kalenders
- App-Passwort in Nextcloud generieren

#### 1e. Altes n8n-Setup aufräumen
- n8n Workflow `oockWznqW4SiqcHG` deaktivieren
- `config/n8n-workflows/calendar-mcp-server.json` entfernen
- `CALENDAR_MCP_URL` aus Production `.env` entfernen

#### 1f. Tests
- Unit tests im MCP-Server-Repo (alle Backends mit mocks)
- Intent-Routing: `POST /debug/intent?message=Was steht heute an?` → `mcp.calendar.list_events`
- Integration: Chat "Was steht heute an?" → merged Events

---

### Phase 2: Proaktive Notifications (generisch, nicht nur Kalender)

**Ziel:** "In 30 Minuten: Team-Meeting" als TTS auf dem Satellite — aber als **generisches System** das für alle zukünftigen Integrationen funktioniert.

**Kernprinzip:** Keine Abhängigkeit zu n8n oder einem spezifischen Kalender-Backend. Stattdessen ein generischer Mechanismus der mit JEDEM MCP Server funktioniert.

#### Architektur-Analyse: Drei Ansätze

##### Ansatz A: Polling mit MCP Tool-Konvention

```
Renfield Backend (NotificationPollerService)
  ↓ alle 15 min: mcp.calendar.get_pending_notifications()
  ↓ alle 15 min: mcp.tasks.get_pending_notifications()    ← Zukunft
  ↓ alle 5 min:  mcp.shipping.get_pending_notifications()  ← Zukunft
  ↓
NotificationService → WebSocket + TTS
```

MCP Server die zeitbasierte Daten haben, exponieren ein **Standard-Tool** `get_pending_notifications`. Renfield pollt diese periodisch.

| Pro | Contra |
|-----|--------|
| Kein Protokoll-Änderung nötig (normaler Tool-Call) | Polling ist inhärent verzögert (bis zu `poll_interval`) |
| Kein n8n, keine externe Abhängigkeit | Leerlauf-Polls (meist leer) |
| Generisch — jeder MCP Server kann teilnehmen | MCP Server muss bei jedem Poll antworten |
| Einfachste Implementierung | |
| MCP Server braucht keine Callback-URL | |
| **Nutzt bestehende MCP-Infrastruktur** | |

##### Ansatz B: MCP Resource Subscriptions (Push via Protokoll)

```
Calendar MCP Server
  ↓ notifications/resources/updated (MCP Protokoll)
Renfield MCPManager (Notification Handler)
  ↓ resources/read → Notification-Daten
NotificationService → WebSocket + TTS
```

MCP Server deklariert Resources (z.B. `notifications://calendar/upcoming`). Renfield abonniert diese. Bei Änderungen sendet der Server eine Push-Notification.

| Pro | Contra |
|-----|--------|
| Push-Style (echtes Real-Time) | MCPManager handled aktuell **keine** eingehenden Notifications |
| Spec-konform (MCP Ressourcen) | Signifikanter Aufwand in `mcp_client.py` |
| Elegant | MCP Server braucht Resources-Interface (aktuell nur Tools) |
| | Background-Tasks in stdio-Servern sind komplex |
| | Notification sagt nur "was hat sich geändert" — Daten müssen separat gelesen werden |

##### Ansatz C: MCP Server POSTet an Notification Webhook

```
Calendar MCP Server (Background-Thread)
  ↓ HTTP POST /api/notifications/webhook
Renfield NotificationService → WebSocket + TTS
```

MCP Server bekommt `NOTIFICATION_WEBHOOK_URL` + Token als env vars und POSTet selbständig.

| Pro | Contra |
|-----|--------|
| Echtes Real-Time | MCP Server muss Backend-URL kennen (Kopplung) |
| Einfach für Backend (Webhook existiert) | Jeder Server re-implementiert Scheduling |
| | Bricht unidirektionales MCP-Paradigma |
| | Background-Threads in stdio-Servern |
| | Schwerer zu testen/debuggen |

#### Empfehlung: Ansatz A (Polling) jetzt, Ansatz B (Push) als Zukunftsoption

**Ansatz A ist pragmatisch und richtig für Phase 2:**
- Kalender-Erinnerungen brauchen keine Sub-Sekunden-Latenz — 15 min Polling reicht
- Nutzt die bestehende MCP Tool-Call-Infrastruktur (null Backend-Änderungen an `mcp_client.py`)
- Komplett generisch — jeder zukünftige MCP Server (Tasks, Shipping, etc.) exponiert einfach `get_pending_notifications`
- Kein n8n, keine Bridges, keine Callback-URLs

**Ansatz B ist die elegantere Langzeitlösung** wenn Real-Time nötig wird (z.B. Home Assistant Alarme). Erfordert aber signifikante MCPManager-Erweiterung.

#### 2a. MCP Tool-Konvention: `get_pending_notifications`

**Standard-Interface** für alle MCP Server die proaktive Notifications liefern wollen:

```python
@mcp.tool()
async def get_pending_notifications(
    lookahead_minutes: int = 45,
) -> list[dict]:
    """Return pending notifications for upcoming events/reminders.

    Returns list of notification objects with:
    - event_type: str (e.g. "calendar.reminder_upcoming")
    - title: str
    - message: str (human-readable, wird als TTS gesprochen)
    - urgency: str (info/warning/critical)
    - scheduled_at: str (ISO datetime — wann die Notification feuern soll)
    - dedup_key: str (eindeutiger Key zur Vermeidung von Doppel-Notifications)
    - data: dict (integration-spezifische Daten)
    """
```

Beispiel-Response vom Calendar MCP Server:
```json
[
  {
    "event_type": "calendar.reminder_upcoming",
    "title": "Team-Meeting",
    "message": "In 30 Minuten: Team-Meeting (Firmenkalender)",
    "urgency": "info",
    "scheduled_at": "2026-02-13T13:30:00+01:00",
    "dedup_key": "calendar:work:event123:30min",
    "data": {
      "calendar": "work",
      "event_id": "event123",
      "event_start": "2026-02-13T14:00:00+01:00",
      "minutes_until": 30
    }
  }
]
```

#### 2b. `NotificationPollerService` im Renfield Backend

Neuer Service der periodisch alle teilnehmenden MCP Server pollt:

**Konfiguration in `mcp_servers.yaml`:**
```yaml
- name: calendar
  # ... bestehende Felder ...
  notifications:
    enabled: true
    poll_interval: 900     # Sekunden (15 min)
    tool: get_pending_notifications  # Standard-Konvention
```

**Service-Logik:**
```python
class NotificationPollerService:
    """Polls MCP servers for pending notifications."""

    async def start(self):
        """Start polling loop for each configured server."""
        for server in self.get_pollable_servers():
            asyncio.create_task(self._poll_loop(server))

    async def _poll_loop(self, server):
        while True:
            await asyncio.sleep(server.poll_interval)
            try:
                result = await self.mcp_manager.execute_tool(
                    f"mcp.{server.name}.get_pending_notifications",
                    {"lookahead_minutes": 45}
                )
                for notification in self._parse_notifications(result):
                    if not self._is_duplicate(notification["dedup_key"]):
                        await self.notification_service.create_from_mcp(notification)
            except Exception:
                logger.warning(f"Poll failed for {server.name}", exc_info=True)
```

**Integration:**
- Gestartet in `api/lifecycle.py` (neben anderen Background Tasks)
- Nutzt bestehenden `MCPManager.execute_tool()` — keine neue Infrastruktur
- Nutzt bestehenden `NotificationService` — TTS + WebSocket funktioniert automatisch
- `dedup_key` verhindert Doppel-Notifications bei wiederholtem Polling

#### 2c. Erweiterbarkeit für zukünftige Integrationen

Jeder MCP Server der `get_pending_notifications` exponiert, wird automatisch gepollt:

```yaml
# Zukünftig: Task-Manager
- name: tasks
  notifications:
    enabled: true
    poll_interval: 1800     # 30 min
    tool: get_pending_notifications

# Zukünftig: Paket-Tracking
- name: shipping
  notifications:
    enabled: true
    poll_interval: 3600     # 1 Stunde
    tool: get_pending_notifications
```

**Kein Backend-Code nötig** — nur YAML-Config + ein Tool im MCP Server.

---

### Phase 3: User-ID Propagation (Multi-User Grundlage)

**Ziel:** ActionExecutor und MCP-Tools wissen, welcher User fragt.

> Dies ist eine **cross-cutting Änderung** die ALLEN zukünftigen per-User-Integrationen dient.

#### 3a. ActionExecutor erweitern

**`src/backend/services/action_executor.py`:**
```python
async def execute(self, intent_data: dict, user_id: int | None = None) -> dict:
    if self.mcp_manager and intent.startswith("mcp."):
        params = intent_data.get("parameters", {})
        if user_id is not None:
            params["_user_id"] = user_id
        return await self.mcp_manager.execute_tool(intent, params)
```

#### 3b. WebSocket Handler (3 Dateien)
- `chat_handler.py` — `user_id` an `executor.execute()` übergeben
- `satellite_handler.py` — Speaker Recognition → `user_id`
- `device_handler.py` — Auth-basierter `user_id`

#### 3c. Agent Service
- `run()` Methode: `user_id` Parameter → an `executor.execute()` durchreichen

#### 3d. Tests
- `test_action_executor.py`: `user_id` korrekt an MCP-Tools weitergereicht
- `test_chat_handler.py` / `test_satellite_handler.py`: Propagation testen

---

### Phase 4: Multi-User Kalender-Sichtbarkeit

**Ziel:** Jeder User sieht eigene + geteilte Kalender.

**Ansatz (YAML-basiert, kein DB-Modell):** `calendar_accounts.yaml` erweitern um `visibility`:

```yaml
calendars:
  - name: work
    label: "Firmenkalender"
    type: ews
    visibility: owner       # Nur der Owner sieht diesen Kalender
    owner: "admin"           # Username
    # ...

  - name: family
    label: "Familienkalender"
    type: google
    visibility: shared       # Alle sehen diesen Kalender
    # ...

  - name: verein
    label: "Vereinskalender"
    type: caldav
    visibility: shared
    # ...
```

Der MCP Server filtert bei `list_events` basierend auf `_user_id` (aus Phase 3) welche Kalender sichtbar sind.

#### MCP Permissions

```yaml
# In mcp_servers.yaml:
- name: calendar
  permissions:
    - mcp.calendar.read
    - mcp.calendar.manage
  tool_permissions:
    list_events: mcp.calendar.read
    list_calendars: mcp.calendar.read
    create_event: mcp.calendar.manage
    update_event: mcp.calendar.manage
    delete_event: mcp.calendar.manage
```

**Default-Rollen:** Admin + Familie → `mcp.calendar.manage`, Gast → keine Calendar-Permissions.

---

## Abhängigkeit: Raumscharfe Präsenzerkennung

Phase 2 (Raum-Routing für Push-Erinnerungen) hat eine optionale Abhängigkeit zur **raumscharfen Präsenzerkennung via BLE**. Für vertrauliche Notifications: TTS-Ausgabe nur wenn der Ziel-User im Raum ist.

**Separates Feature-Dokument:** Siehe `docs/private/presence-detection-plan.md`

**Ohne Presence Detection:** Konservativer Fallback (kein TTS für personal/confidential Notifications).

---

## Dateien-Übersicht

### Neues Repository: `renfield-mcp-calendar`
| Datei | Beschreibung |
|-------|-------------|
| `src/renfield_mcp_calendar/__init__.py` | Version + main() |
| `src/renfield_mcp_calendar/__main__.py` | python -m entry point |
| `src/renfield_mcp_calendar/server.py` | FastMCP Server + 6 unified tools |
| `src/renfield_mcp_calendar/config.py` | YAML config + dataclasses |
| `src/renfield_mcp_calendar/backends/base.py` | CalendarBackend Protocol + CalendarEvent |
| `src/renfield_mcp_calendar/backends/ews.py` | Exchange EWS via exchangelib |
| `src/renfield_mcp_calendar/backends/google.py` | Google Calendar API |
| `src/renfield_mcp_calendar/backends/caldav_backend.py` | CalDAV (Nextcloud etc.) |
| `pyproject.toml` | Package metadata + dependencies |
| `tests/` | Unit tests mit mocks |

### Geänderte Dateien im Renfield-Projekt

| Datei | Phase | Änderung |
|-------|-------|---------|
| `config/mcp_servers.yaml` | 1 | Google Calendar (SSE/n8n) → Unified Calendar (stdio) |
| `config/calendar_accounts.yaml` | 1 | **Neu:** Multi-Account YAML Config |
| `config/google_calendar_credentials.json` | 1 | **Neu:** Google OAuth2 Client Credentials |
| `src/backend/requirements.txt` | 1 | + renfield-mcp-calendar GitHub URL |
| `docker-compose.yml` | 1 | Volume mounts für calendar config + token |
| `.env.example` | 1 | Google Calendar vars → Multi-Calendar vars |
| `docs/ENVIRONMENT_VARIABLES.md` | 1 | Calendar-Sektion aktualisieren |
| `CLAUDE.md` | 1 | Calendar-Beschreibung anpassen |
| `src/backend/services/action_executor.py` | 3 | `user_id` Parameter |
| `src/backend/services/agent_service.py` | 3 | `user_id` durchreichen |
| `src/backend/api/websocket/chat_handler.py` | 3 | `user_id` an executor |
| `src/backend/api/websocket/satellite_handler.py` | 3 | `user_id` an executor |

### Neue Dateien im Renfield-Projekt (Phase 2)
| Datei | Beschreibung |
|-------|-------------|
| `src/backend/services/notification_poller.py` | NotificationPollerService — generischer MCP-Poller |

### Zu entfernen
| Datei | Grund |
|-------|-------|
| `config/n8n-workflows/calendar-mcp-server.json` | Nicht mehr benötigt |
| n8n Workflow `oockWznqW4SiqcHG` | Deaktivieren |

## Verifizierung

### Phase 1
1. **MCP-Verbindung:** Backend-Logs zeigen `MCP server 'calendar' connected: X tools discovered`
2. **Alle Kalender:** Chat "Zeige meine Kalender" → listet alle 3 Accounts
3. **Cross-Calendar:** Chat "Was steht heute an?" → merged Events aus allen Kalendern
4. **Spezifisch:** Chat "Was steht im Firmenkalender?" → nur Exchange-Events
5. **Erstellen:** Chat "Erstelle einen Familientermin morgen um 10: Arzttermin" → Google Calendar
6. **Intent:** `POST /debug/intent?message=Was steht heute an?` → `mcp.calendar.list_events`
7. **Tests:** `python -m pytest tests/` im MCP-Server-Repo
8. **Lint:** `make lint-backend` im Renfield-Projekt

### Phase 2
1. `NotificationPollerService` läuft (Backend-Logs: "Notification poller started for calendar")
2. Calendar MCP Server hat `get_pending_notifications` Tool
3. Testtermin in 30 min erstellen → TTS-Ansage auf Satellite
4. Dedup: Gleicher Termin wird nicht doppelt benachrichtigt
5. Config-Test: `notifications.poll_interval` ändern → Polling-Intervall ändert sich

### Phase 3
1. `make test-backend` — alle Tests grün
2. Chat mit Auth: `user_id` wird an MCP-Tools übergeben (Debug-Log)

## Empfohlene Reihenfolge

1. **Phase 1 zuerst** — liefert sofortigen Mehrwert (alle drei Kalender abfragen + Termine erstellen)
2. **Phase 2 danach** — Generisches Notification-Polling via `NotificationPollerService` (kein n8n nötig)
3. **Phase 3+4 als Erweiterung** — Multi-User wird erst relevant wenn mehrere Benutzer aktiv sind

## Offene Fragen (aus Original-Plan, aktualisiert)

- [x] ~~Welcher Kalender-Provider zuerst?~~ → Alle drei gleichzeitig (unified MCP Server)
- [x] ~~n8n als Bridge oder Custom MCP Server?~~ → Custom MCP Server (n8n-Bridge hatte Wartungsprobleme)
- [ ] Sollen Erinnerungen pro Kalender konfigurierbar sein oder global?
- [ ] CalDAV-URL und Zugangsdaten für den Nextcloud-Vereinskalender?