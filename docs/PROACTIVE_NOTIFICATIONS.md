# Proaktive Benachrichtigungen

Renfield empfängt Webhook-Benachrichtigungen von Home Assistant Automationen und liefert sie intelligent an verbundene Geräte aus — via WebSocket (Toast-UI) und TTS (Sprachausgabe).

**Kernprinzip:** Home Assistant macht die Regeln (Automationen), Renfield liefert die Intelligenz (Multi-Room-Routing, Deduplizierung, TTS).

---

## Architektur

```
HA Automation fires
  → POST /api/notifications/webhook (Bearer Token)
  → NotificationService.process_webhook()
      → Hash-basierte Deduplizierung
      → DB: Notification erstellen (status=pending)
      → Deliver:
          → Web-Clients: DeviceManager broadcast mit WS-Message
          → TTS: PiperService → OutputRoutingService → AudioOutputService
      → DB: status=delivered
```

---

## Setup

### 1. Proaktive Benachrichtigungen aktivieren

```bash
# .env
PROACTIVE_ENABLED=true
```

### 2. Webhook-Token generieren

```bash
curl -X POST http://localhost:8000/api/notifications/token
# Response: { "token": "abc123...", "message": "..." }
```

### 3. Token in Home Assistant speichern

In HA → Einstellungen → Helfer → `input_text.renfield_webhook_token` erstellen und den Token eintragen.

---

## Home Assistant Konfiguration

### rest_command (configuration.yaml)

```yaml
rest_command:
  renfield_notify:
    url: "http://renfield.local:8000/api/notifications/webhook"
    method: POST
    headers:
      Authorization: "Bearer {{ states('input_text.renfield_webhook_token') }}"
      Content-Type: "application/json"
    payload: >-
      { "event_type": "{{ event_type }}", "title": "{{ title }}",
        "message": "{{ message }}", "urgency": "{{ urgency | default('info') }}",
        "room": "{{ room | default('') }}", "tts": {{ tts | default(true) | tojson }},
        "data": {{ data | default({}) | tojson }} }
```

### Beispiel-Automation

```yaml
automation:
  - alias: "Renfield: Waschmaschine fertig"
    trigger:
      - platform: state
        entity_id: sensor.washing_machine_state
        from: "running"
        to: "idle"
        for: { minutes: 2 }
    action:
      - service: rest_command.renfield_notify
        data:
          event_type: "ha_automation"
          title: "Waschmaschine fertig"
          message: "Die Waschmaschine ist fertig."
          urgency: "info"
          room: "Wohnzimmer"
```

---

## API Endpoints

### POST /api/notifications/webhook

Empfängt Benachrichtigungen von HA-Automationen.

**Headers:** `Authorization: Bearer <token>`

**Request Body:**
```json
{
  "event_type": "ha_automation",
  "title": "Waschmaschine fertig",
  "message": "Die Waschmaschine ist fertig.",
  "urgency": "info",
  "room": "Wohnzimmer",
  "tts": true,
  "data": { "entity_id": "sensor.washing_machine_state" }
}
```

| Feld | Typ | Pflicht | Beschreibung |
|------|-----|---------|-------------|
| `event_type` | string | Ja | Kategorie der Benachrichtigung |
| `title` | string | Ja | Kurztitel |
| `message` | string | Ja | Ausführliche Nachricht |
| `urgency` | string | Nein | `critical`, `info` (default), `low`; `auto` wird angenommen, aber für Webhook-Meldungen zu `info` (siehe „LLM-Gate je Meldungsart") |
| `room` | string | Nein | Ziel-Raum (null = alle Räume) |
| `tts` | boolean | Nein | TTS-Ausgabe (default: `PROACTIVE_TTS_DEFAULT`) |
| `enrich` | boolean | Nein | Wird angenommen, ist für Webhook-Meldungen aber **wirkungslos**: die LLM-Aufbereitung läuft nur für serverseitig verbürgte technische Meldungen (siehe „LLM-Gate je Meldungsart") |
| `data` | object | Nein | Zusätzliche Metadaten |

**Response:** `201 Created`
```json
{
  "notification_id": 42,
  "status": "delivered",
  "delivered_to": ["device-abc", "device-xyz"]
}
```

**Fehler:**
- `401` — Ungültiger Token
- `429` — Duplikat innerhalb des Suppressions-Fensters
- `503` — Proaktive Benachrichtigungen deaktiviert

### Sichtbarkeit (Liste, Bestätigen, Verwerfen, Unterdrücken)

Mit `AUTH_ENABLED=true` sieht und bearbeitet ein Nutzer nur, was **an ihn adressiert** ist (`target_user_id` = er selbst) oder **öffentlich an niemanden** gerichtet ist (`target_user_id` leer und `privacy="public"`). Eine persönliche Benachrichtigung ohne Empfänger bleibt für normale Nutzer verborgen. Nutzer mit `notifications.manage` sehen alles. Die Benachrichtigung eines anderen verhält sich wie eine nicht existierende (`404`), und ohne Anmeldung gibt es `401`. Mit `AUTH_ENABLED=false` (ein Haushalt) ändert sich nichts.

Dieselbe Regel gilt auf **allen** Wegen, nicht nur für REST:
- **Bestätigen und Verwerfen über das Device-WebSocket** (`notification_ack`). Der Browser-Toast nutzt genau diesen Weg. Ein Login-Token gilt für seinen Nutzer, ein Geräte-Token ohne Nutzer darf nur öffentliche Benachrichtigungen ohne Empfänger bestätigen.
- **Unterdrückungsregeln:** Ein Nutzer sieht seine eigenen und die globalen Regeln, löschen darf er nur seine eigenen.
- **Erinnerungen:** Liste und Stornieren nur die eigenen. Eine fällige Erinnerung wird als `privacy="personal"` an ihren Besitzer ausgelöst, nicht mehr öffentlich.

Admin heißt: `admin` oder `notifications.manage`.

**Verhaltensänderung bei Login-Instanzen:** Persönliche Benachrichtigungen ohne Empfänger (z. B. eine HA-Automation mit `privacy="personal"` ohne `target_user_id`) und Erinnerungen ohne Besitzer (Sprachbefehl ohne erkannten Sprecher) sehen jetzt nur noch Admins.

Hintergrund (2026-09-14): Vorher konnte jeder angemeldete Nutzer die persönlichen Benachrichtigungen aller anderen auflisten, bestätigen, verwerfen und daraus Unterdrückungsregeln bauen. Das Präsenz-Gate schützte nur den Live-Push, nicht die REST-Liste.

### GET /api/notifications

Liste mit optionalen Filtern, auf die Sichtbarkeit des Aufrufers beschränkt.

**Query-Parameter:** `room_id`, `urgency`, `status`, `since` (ISO 8601), `limit` (default: 50), `offset` (default: 0)

### PATCH /api/notifications/{id}/acknowledge

Bestätigt eine Benachrichtigung (nur eine sichtbare, sonst `404`).

**Query-Parameter:** `acknowledged_by` (optional)

### DELETE /api/notifications/{id}

Soft-Delete (setzt Status auf `dismissed`; nur eine sichtbare, sonst `404`).

### POST /api/notifications/token

Generiert einen neuen Webhook-Token. Der vorherige Token wird ungültig.

---

## WebSocket-Protokoll

### Server → Client (notification)

```json
{
  "type": "notification",
  "notification_id": 42,
  "title": "Waschmaschine fertig",
  "message": "Die Waschmaschine ist fertig.",
  "urgency": "info",
  "source": "ha_automation",
  "room": "Wohnzimmer",
  "tts_handled": true,
  "created_at": "2026-02-05T14:30:00"
}
```

### Client → Server (notification_ack)

```json
{
  "type": "notification_ack",
  "notification_id": 42,
  "action": "acknowledged"
}
```

`action`: `"acknowledged"` oder `"dismissed"`

---

## LLM-Gate je Meldungsart (BL-0424)

Die beiden LLM-Schritte der Pipeline — Auto-Dringlichkeit (`urgency: "auto"`,
`PROACTIVE_URGENCY_AUTO_ENABLED`) und Anreicherung (`enrich: true`,
`PROACTIVE_ENRICHMENT_ENABLED`) — laufen **nur für technische Meldungen**.
Zwei Bedingungen, beide serverseitig:

1. **Vertrauensgrenze:** der Absender muss die Meldung als technisch verbürgen
   (`process_webhook(llm_eligible=True)`). Das tut allein `ops_alert.notify_admin`
   (MCP-Health, Paperless-Index, geplante Aufgaben). Der HA-Webhook und der
   MCP-Poller setzen es nie — `event_type`, `enrich` und `urgency` sind dort vom
   Aufrufer gewählt und könnten sonst persönlichen Text unter einem technischen
   Etikett durch das Modell schleusen. Für Webhook-Meldungen sind `enrich: true`
   und `urgency: "auto"` deshalb wirkungslos (`auto` → `info`, Text wörtlich).
2. **Betreiber-Filter:** `PROACTIVE_LLM_EVENT_TYPES` (Vorgabe
   `ops_health,mcp_health,scheduled_task_health`; Groß-/Kleinschreibung egal,
   leer = keine) schaltet einzelne technische Meldungsarten ab.

Damit erreicht kein persönlicher Inhalt (Erinnerung, Frist, HA-Ereignis über
Personen) ein Sprachmodell zur Umformulierung oder Einstufung. (Die semantische
Deduplizierung bettet weiterhin jede Meldung ein — das ist ein Embedding-Modell,
kein Umformulierer; sie ist von diesem Gate unberührt.)

`notify_admin` bietet jede Meldung zur Anreicherung an; ohne ausdrückliche
Dringlichkeit überlässt es die Einstufung dem Klassifikator, sobald der
Auto-Schalter an ist — aus bleibt es bei `critical`. Fällt der Klassifikator aus
(typisch: weil genau der LLM-Host gestört ist, über den die Meldung geht), gilt
der Rückfall `critical`, nie `info`. Eine ausdrücklich gesetzte Dringlichkeit
(`normal` für „läuft wieder") bleibt. Angereicherte Meldungen behalten den
Originaltext in `original_message`; gesprochen und gepusht wird der angereicherte.

Technisch heißt nicht personenfrei: Fehlertexte (`last_error`, auf 300 Zeichen
gekappt), Dateinamen oder Mailbetreffe können in einer technischen Meldung
stecken. Beide Prompts zäunen diese Felder als Daten ein und weisen das Modell
an, keine darin enthaltenen Anweisungen zu befolgen. Die Aufrufe gehen an den
Chat-Tier von `utils.llm_client.get_default_client()` — im Haushalt der lokale
llama-server, grundsätzlich aber dorthin, wohin `LLM_OPENAI_BASE_URL` zeigt.

---

## Frontend

Die Toast-Komponente (`NotificationToast`) erscheint oben rechts im Browser:

- **Urgency-Styling:** critical = rot, info = blau, low = grau
- **Auto-Dismiss:** 10 Sekunden für info/low, persistent für critical
- **Max 3 sichtbar:** Restliche werden gequeued
- **Dark Mode:** Volle Unterstützung
- **i18n:** Deutsch + Englisch

---

## Konfiguration

| Variable | Default | Beschreibung |
|----------|---------|-------------|
| `PROACTIVE_ENABLED` | `false` | Master-Switch (opt-in) |
| `PROACTIVE_SUPPRESSION_WINDOW` | `60` | Dedup-Fenster in Sekunden |
| `PROACTIVE_TTS_DEFAULT` | `true` | TTS standardmäßig aktiviert |
| `PROACTIVE_NOTIFICATION_TTL` | `86400` | Ablauf in Sekunden (24h) |

Der Webhook-Token wird in `SystemSetting` (DB) gespeichert, nicht in `.env` — Runtime-Rotation via Admin-API.

---

## Deduplizierung

Hash-basiert (SHA256 von `event_type + title + message + room`). Innerhalb des konfigurierbaren Suppressions-Fensters (default: 60s) werden identische Benachrichtigungen unterdrückt. HTTP 429 wird zurückgegeben.

---

## Dateien

| Datei | Beschreibung |
|-------|-------------|
| `services/notification_service.py` | Kern-Service: Webhook, Dedup, Delivery |
| `api/routes/notifications.py` | REST-Endpoints |
| `api/routes/notifications_schemas.py` | Pydantic Schemas |
| `models/database.py` | `Notification` Model |
| `components/NotificationToast.tsx` | Toast-UI |
| `hooks/useNotifications.ts` | WS-Integration Hook |
