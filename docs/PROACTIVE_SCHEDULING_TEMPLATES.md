# Proactive Scheduling Templates

Renfield delegates cron-based scheduling to external systems that are purpose-built for it:
- **n8n** for data-aggregation workflows (weather, calendar, HA states) with LLM summarisation
- **Home Assistant** for time-based automations and simple notifications

Both deliver notifications via `POST /api/notifications/webhook` with Bearer token authentication.

---

## Webhook API Reference

```
POST /api/notifications/webhook
Authorization: Bearer <token>
Content-Type: application/json
```

Generate a token via the Admin API:
```bash
curl -X POST http://localhost:8000/api/notifications/token \
  -H "Authorization: Bearer <admin-jwt>"
```

**Payload:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `event_type` | string | yes | Event identifier (e.g. `scheduled.briefing`) |
| `title` | string | yes | Notification title |
| `message` | string | yes | Notification body |
| `urgency` | string | no | `critical`, `info` (default), `low`, or `auto` (LLM classifies) |
| `room` | string | no | Target room name (e.g. `Wohnzimmer`) |
| `tts` | bool | no | Override TTS setting (default from `PROACTIVE_TTS_DEFAULT`) |
| `data` | object | no | Arbitrary metadata |
| `enrich` | bool | no | Let the LLM rephrase the message naturally (default: `false`) |

---

## n8n Workflow Template: Morning Briefing

A Cron-triggered n8n workflow that aggregates data from multiple sources and delivers a summarised briefing to Renfield.

### Importable Workflow

**File:** [`docs/n8n-workflows/morning-briefing.json`](n8n-workflows/morning-briefing.json)

**Import into n8n:**
1. Open your n8n instance
2. Go to **Workflows** → **Import from File**
3. Select `morning-briefing.json`
4. Configure the required environment variables (see below)
5. **Activate** the workflow (it is imported as inactive by default)

### Workflow Structure

```
[Schedule Trigger] ──┬── [Fetch HA States] ──┐
                     │                        ├── [Merge Data] → [Build Briefing] → [POST to Renfield]
                     └── [Fetch Weather]  ────┘
```

HA States and Weather run in parallel, then merge before the Code node composes the briefing. The final POST uses `enrich: true` so Renfield's LLM rephrases the raw data into natural language.

### n8n Environment Variables

Set these in **Settings → Environment Variables** in your n8n instance:

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `HOME_ASSISTANT_URL` | yes | `http://homeassistant.local:8123` | HA base URL |
| `HOME_ASSISTANT_TOKEN` | yes | `eyJ0eX...` | HA long-lived access token |
| `OPENWEATHERMAP_API_KEY` | yes | `abc123...` | OpenWeatherMap API key (free tier) |
| `WEATHER_CITY` | yes | `Berlin` | City name for weather lookup |
| `RENFIELD_URL` | yes | `http://renfield.local:8000` | Renfield backend URL |
| `RENFIELD_WEBHOOK_TOKEN` | yes | *(from /api/notifications/token)* | Webhook Bearer token |
| `BRIEFING_ROOM` | no | `Schlafzimmer` | Target room (default: Schlafzimmer) |

### Entity ID Customization

The Code node ("Build Briefing") reads HA entity states by entity ID. Edit the `ENTITY_IDS` object at the top of the Code node to match your Home Assistant setup:

```javascript
const ENTITY_IDS = {
  indoor_temp:   'sensor.indoor_temperature',   // your indoor temp sensor
  outdoor_temp:  'sensor.outdoor_temperature',   // your outdoor temp sensor
  energy_today:  'sensor.daily_energy',          // daily energy consumption
  windows:       /binary_sensor\..*window/i,     // regex matching window sensors
};
```

To find your entity IDs, check **HA Developer Tools → States** or run:
```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  http://homeassistant.local:8123/api/states | \
  jq '.[].entity_id' | grep -i temp
```

### Step-by-step

1. **Schedule Trigger** — Weekdays 07:00, weekends 08:00
2. **Fetch HA States** — `GET $HOME_ASSISTANT_URL/api/states` (parallel)
3. **Fetch Weather** — `GET openweathermap.org/data/2.5/weather` (parallel)
4. **Merge Data** — Combine both HTTP responses
5. **Build Briefing** (Code node) — Extract temps, energy, windows, weather → compose German-language message
6. **POST to Renfield** — `POST /api/notifications/webhook` with `enrich: true`

### Advantages over built-in scheduler

- n8n has a full-featured Cron parser with ranges, steps, and L/W/# modifiers
- Data aggregation from real sources (HA, weather) instead of blind LLM call
- Visual workflow editor for easy customisation
- Error handling, retries, and logging built-in
- Can trigger different briefings for different rooms/users

---

## Home Assistant Automation Template: Time-based Notification

For simple time-based notifications without data aggregation, HA automations are sufficient.

### REST Command (configuration.yaml)

```yaml
rest_command:
  renfield_notify:
    url: "http://renfield.local:8000/api/notifications/webhook"
    method: POST
    headers:
      Authorization: "Bearer YOUR_WEBHOOK_TOKEN"
      Content-Type: "application/json"
    payload: >
      {
        "event_type": "{{ event_type }}",
        "title": "{{ title }}",
        "message": "{{ message }}",
        "urgency": "{{ urgency | default('info') }}",
        "room": "{{ room | default('') }}",
        "tts": {{ tts | default(true) | tojson }}
      }
```

### Example: Trash Collection Reminder

```yaml
automation:
  - alias: "Müllabfuhr-Erinnerung"
    trigger:
      - platform: time
        at: "19:00:00"
    condition:
      - condition: template
        value_template: >
          {{ now().weekday() == 0 }}  # Monday evening
    action:
      - service: rest_command.renfield_notify
        data:
          event_type: "reminder.trash"
          title: "Müllabfuhr morgen"
          message: "Denk daran, die Tonnen rauszustellen!"
          urgency: "info"
          room: "Wohnzimmer"
          tts: true
```

### Example: Daily Summary at Sunset

```yaml
automation:
  - alias: "Tägliche Zusammenfassung"
    trigger:
      - platform: sun
        event: sunset
        offset: "+00:30:00"
    action:
      - service: rest_command.renfield_notify
        data:
          event_type: "scheduled.daily_summary"
          title: "Tagesbericht"
          message: >
            Energieverbrauch heute: {{ states('sensor.daily_energy') }} kWh.
            Temperatur draußen: {{ states('sensor.outdoor_temperature') }}°C.
          urgency: "info"
          room: "Wohnzimmer"
          tts: true
          enrich: true
```

### Example: Morning Briefing (simple, without n8n)

```yaml
automation:
  - alias: "Einfaches Morgenbriefing"
    trigger:
      - platform: time
        at: "07:00:00"
    condition:
      - condition: time
        weekday:
          - mon
          - tue
          - wed
          - thu
          - fri
    action:
      - service: rest_command.renfield_notify
        data:
          event_type: "scheduled.briefing"
          title: "Guten Morgen"
          message: >
            Draußen sind es {{ states('sensor.outdoor_temperature') }}°C.
            {% if states('sensor.outdoor_temperature') | float < 5 %}Zieh dich warm an!{% endif %}
          urgency: "info"
          room: "Schlafzimmer"
          tts: true
          enrich: true
```

**Tip:** Set `enrich: true` to let Renfield's LLM rephrase the raw HA template output into natural language.

---

## When to use which

| Use Case | Tool | Why |
|----------|------|-----|
| Morning briefing with weather + calendar | **n8n** | Needs data aggregation from multiple APIs |
| Simple time reminder (trash, medication) | **HA** | Pure schedule, no external data needed |
| Complex conditional scheduling | **n8n** | If/else logic, multiple branches |
| Sensor-triggered notification | **HA** | HA already has the state data |
| Recurring report with LLM summary | **n8n** | Aggregation + LLM call in one workflow |
