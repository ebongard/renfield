# YAML Plugin Schema Reference

Vollständige Referenz für die YAML Plugin Definition.

---

## Struktur Übersicht

```yaml
name: string                      # REQUIRED
version: string                   # REQUIRED
description: string               # REQUIRED
author: string                    # OPTIONAL
enabled_var: string               # REQUIRED

config:                           # REQUIRED
  url: string                     # OPTIONAL
  api_key: string                 # OPTIONAL
  additional:                     # OPTIONAL
    key: string

intents:                          # REQUIRED (min. 1 intent)
  - name: string                  # REQUIRED
    description: string           # REQUIRED
    parameters:                   # OPTIONAL
      - name: string              # REQUIRED
        type: string              # REQUIRED
        required: boolean         # REQUIRED
        description: string       # REQUIRED
        default: any              # OPTIONAL
        enum: array               # OPTIONAL
        pattern: string           # OPTIONAL
    examples:                     # REQUIRED (min. 1 example)
      - string
    api:                          # REQUIRED
      method: string              # REQUIRED
      url: string                 # REQUIRED
      headers:                    # OPTIONAL
        key: string
      body:                       # OPTIONAL
        key: any
      timeout: integer            # OPTIONAL
      response_mapping:           # OPTIONAL
        key: string

error_mappings:                   # OPTIONAL
  - code: integer                 # REQUIRED
    message: string               # REQUIRED

rate_limit: integer               # OPTIONAL
```

---

## Top-Level Felder

### `name` (Required)
**Type:** `string`

Eindeutiger Plugin-Name. Wird als Prefix für Intents verwendet.

**Regeln:**
- Nur Kleinbuchstaben
- Keine Leerzeichen
- Keine Punkte
- Alphanumerisch + Unterstrich

**Beispiele:**
```yaml
name: weather        # ✅ Gut
name: my_plugin      # ✅ Gut
name: Weather        # ❌ Großbuchstaben
name: weather.api    # ❌ Punkt
name: weather api    # ❌ Leerzeichen
```

---

### `version` (Required)
**Type:** `string`

Semantic Versioning (MAJOR.MINOR.PATCH)

**Format:** `X.Y.Z`

**Beispiele:**
```yaml
version: 1.0.0       # ✅ Initial release
version: 1.2.3       # ✅ Bug fix
version: 2.0.0       # ✅ Breaking change
version: v1.0.0      # ❌ Kein 'v' Prefix
version: 1.0         # ❌ Muss 3 Teile haben
```

---

### `description` (Required)
**Type:** `string`

Kurze Beschreibung des Plugins (1-2 Sätze).

**Beispiele:**
```yaml
description: Get weather information using OpenWeatherMap
description: Control Spotify playback and search for music
```

---

### `author` (Optional)
**Type:** `string`

Name des Plugin-Autors.

**Beispiel:**
```yaml
author: Renfield Team
author: John Doe
```

---

### `enabled_var` (Required)
**Type:** `string`

Name der Umgebungsvariable zum Aktivieren des Plugins.

**Naming Convention:**
- `{PLUGIN_NAME}_ENABLED` (UPPERCASE)

**Beispiele:**
```yaml
enabled_var: WEATHER_ENABLED      # ✅ Gut
enabled_var: MY_PLUGIN_ENABLED    # ✅ Gut
enabled_var: weather_enabled      # ❌ Kleinbuchstaben
enabled_var: WEATHER              # ❌ Fehlendes _ENABLED
```

---

## Config Section

### `config` (Required)
**Type:** `object`

Definition der benötigten Umgebungsvariablen.

**Felder:**
- `url` (optional): Env var für API-URL
- `api_key` (optional): Env var für API-Key
- `additional` (optional): Weitere Konfigurationswerte

**Beispiel:**
```yaml
config:
  url: OPENWEATHER_API_URL
  api_key: OPENWEATHER_API_KEY
  additional:
    token: SPOTIFY_ACCESS_TOKEN
    region: API_REGION
```

**Hinweis:** Die Werte sind die **Namen** der Umgebungsvariablen, nicht die Werte selbst!

---

## Intents Section

### `intents` (Required)
**Type:** `array`

Liste der Plugin-Intents (mindestens 1).

---

### Intent Felder

#### `name` (Required)
**Type:** `string`

Eindeutiger Intent-Name.

**Format:** `{plugin_name}.{action}`

**Beispiele:**
```yaml
name: weather.get_current          # ✅ Gut
name: news.search                  # ✅ Gut
name: weather_get_current          # ❌ Falsches Format
name: get_current                  # ❌ Fehlt plugin_name
```

---

#### `description` (Required)
**Type:** `string`

Was dieser Intent tut (1 Satz).

**Beispiel:**
```yaml
description: Get current weather for a location
description: Search news articles by keyword
```

---

#### `parameters` (Optional)
**Type:** `array`

Liste der Intent-Parameter.

**Parameter Felder:**

##### `name` (Required)
**Type:** `string`

Parameter-Name.

**Regeln:**
- Kleinbuchstaben + Unterstrich
- Alphanumerisch

**Beispiele:**
```yaml
name: query          # ✅
name: location       # ✅
name: user_id        # ✅
name: Query          # ❌ Großbuchstaben
name: user-id        # ❌ Bindestrich
```

---

##### `type` (Required)
**Type:** `string`

Datentyp des Parameters.

**Erlaubte Werte:**
- `string`
- `integer`
- `float`
- `boolean`
- `array`
- `object`

**Beispiel:**
```yaml
type: string         # ✅
type: integer        # ✅
type: String         # ❌ Großbuchstaben
type: int            # ❌ Verwende 'integer'
```

---

##### `required` (Required)
**Type:** `boolean`

Ist der Parameter erforderlich?

**Beispiel:**
```yaml
required: true       # ✅ Pflicht-Parameter
required: false      # ✅ Optionaler Parameter
required: yes        # ❌ Verwende true/false
```

---

##### `description` (Required)
**Type:** `string`

Beschreibung des Parameters.

**Beispiel:**
```yaml
description: City name (e.g., Berlin) or coordinates
description: Number of results to return (1-10)
```

---

##### `default` (Optional)
**Type:** `any`

Standard-Wert wenn Parameter nicht angegeben.

**Hinweis:** Nur bei `required: false` verwenden!

**Beispiel:**
```yaml
- name: limit
  type: integer
  required: false
  default: 5         # ✅ Standard-Wert

- name: city
  type: string
  required: true
  default: Berlin    # ⚠️  Unsinnig bei required: true
```

---

##### `enum` (Optional)
**Type:** `array`

Liste erlaubter Werte.

**Beispiel:**
```yaml
- name: category
  type: string
  required: false
  enum: [general, business, technology, sports]

- name: sort_order
  type: string
  required: false
  enum: [asc, desc]
```

---

##### `pattern` (Optional)
**Type:** `string`

Regex-Pattern zur Validierung.

**Beispiel:**
```yaml
- name: email
  type: string
  required: true
  pattern: "^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$"

- name: zip_code
  type: string
  required: true
  pattern: "^\\d{5}$"
```

**Hinweis:** Backslashes müssen escaped werden (`\\`)

---

#### `examples` (Required)
**Type:** `array`

Liste von Beispiel-Anfragen (min. 1, empfohlen 2-5).

**Regeln:**
- Natürliche deutsche Sprache
- Deckt verschiedene Parameter-Kombinationen ab
- Realistisch und vollständig

**Beispiel:**
```yaml
examples:
  - "Wie ist das Wetter in Berlin?"
  - "Wie warm ist es draußen?"
  - "Wettervorhersage für München"
```

---

### API Definition

#### `api` (Required)
**Type:** `object`

Definition des API-Calls.

---

##### `method` (Required)
**Type:** `string`

HTTP-Methode.

**Erlaubte Werte:**
- `GET`
- `POST`
- `PUT`
- `DELETE`
- `PATCH`

**Beispiel:**
```yaml
method: GET          # ✅
method: POST         # ✅
method: get          # ❌ Großbuchstaben verwenden
method: Http.GET     # ❌ Nur Methodenname
```

---

##### `url` (Required)
**Type:** `string`

API-Endpoint URL mit Template-Platzhaltern.

**Template-Syntax:**
- `{config.key}` - Aus config section
- `{params.key}` - Aus parameters

**Beispiele:**
```yaml
url: "{config.url}/weather?q={params.location}&appid={config.api_key}"
url: "https://api.example.com/search?q={params.query}"
url: "{config.url}/v1/endpoint"
```

**Wichtig:**
- Parameter werden automatisch URL-encoded
- Config-Werte werden NICHT encoded

---

##### `headers` (Optional)
**Type:** `object`

HTTP-Headers als key-value Paare.

**Beispiel:**
```yaml
headers:
  Authorization: "Bearer {config.api_key}"
  Content-Type: "application/json"
  User-Agent: "Renfield/1.0"
```

---

##### `body` (Optional)
**Type:** `object`

Request Body (nur für POST/PUT/PATCH).

**Beispiel:**
```yaml
body:
  query: "{params.search_term}"
  limit: "{params.limit}"
  filter:
    type: "article"
    language: "de"
```

**Hinweis:** Body wird automatisch als JSON serialisiert.

---

##### `timeout` (Optional)
**Type:** `integer`

Timeout in Sekunden.

**Default:** `10`
**Maximum:** `300` (5 Minuten)

**Beispiel:**
```yaml
timeout: 10          # ✅ 10 Sekunden
timeout: 30          # ✅ 30 Sekunden
timeout: "10"        # ❌ Muss integer sein
```

---

##### `response_mapping` (Optional)
**Type:** `object`

Mapping von API-Response zu strukturierten Daten.

**JSONPath-Notation:**
- `field` - Direkt zugreifen
- `nested.field` - Nested object
- `array[0]` - Array element
- `nested.array[0].field` - Kombination

**Beispiel:**
```yaml
response_mapping:
  temperature: "main.temp"                    # response['main']['temp']
  conditions: "weather[0].description"        # response['weather'][0]['description']
  city: "name"                                # response['name']
  humidity: "main.humidity"                   # response['main']['humidity']
```

**Wenn nicht angegeben:** Vollständige API-Response wird zurückgegeben.

---

## Error Mappings

### `error_mappings` (Optional)
**Type:** `array`

Mapping von HTTP Status Codes zu benutzerfreundlichen Nachrichten.

**Felder:**
- `code` (required): HTTP Status Code
- `message` (required): Benutzerfreundliche deutsche Nachricht

**Beispiel:**
```yaml
error_mappings:
  - code: 401
    message: "API-Schlüssel ungültig. Bitte überprüfen."
  - code: 404
    message: "Nicht gefunden."
  - code: 429
    message: "Zu viele Anfragen. Bitte später erneut versuchen."
  - code: 500
    message: "Server-Fehler. Bitte später erneut versuchen."
```

**Häufige Status Codes:**
- `400` - Bad Request
- `401` - Unauthorized
- `403` - Forbidden
- `404` - Not Found
- `429` - Too Many Requests
- `500` - Internal Server Error
- `503` - Service Unavailable

---

## Rate Limiting

### `rate_limit` (Optional)
**Type:** `integer`

Maximale Anzahl Requests pro Minute.

**Default:** Unbegrenzt
**Empfohlen:** 60-180

**Beispiel:**
```yaml
rate_limit: 60       # 60 requests/minute
rate_limit: 180      # 180 requests/minute
rate_limit: 0        # ❌ Verwende nicht 0, lass es weg
```

---

## Vollständiges Beispiel

```yaml
name: weather
version: 1.0.0
description: Get weather information using OpenWeatherMap
author: Renfield Team
enabled_var: WEATHER_ENABLED

config:
  url: OPENWEATHER_API_URL
  api_key: OPENWEATHER_API_KEY

intents:
  - name: weather.get_current
    description: Get current weather for a location
    parameters:
      - name: location
        type: string
        required: true
        description: City name (e.g., Berlin) or coordinates
    examples:
      - "Wie ist das Wetter in Berlin?"
      - "Wie warm ist es draußen?"
      - "Wetter München"
    api:
      method: GET
      url: "{config.url}/weather?q={params.location}&appid={config.api_key}&units=metric&lang=de"
      timeout: 10
      response_mapping:
        temperature: "main.temp"
        feels_like: "main.feels_like"
        conditions: "weather[0].description"
        humidity: "main.humidity"
        wind_speed: "wind.speed"
        city: "name"

  - name: weather.get_forecast
    description: Get weather forecast for next days
    parameters:
      - name: location
        type: string
        required: true
        description: City name for forecast
      - name: days
        type: integer
        required: false
        default: 3
        description: Number of days to forecast (1-5)
    examples:
      - "Wettervorhersage für Berlin"
      - "Wie wird das Wetter morgen?"
    api:
      method: GET
      url: "{config.url}/forecast?q={params.location}&appid={config.api_key}&units=metric&lang=de&cnt=24"
      timeout: 10
      response_mapping:
        city: "city.name"
        country: "city.country"
        forecast: "list"

error_mappings:
  - code: 401
    message: "API-Schlüssel ungültig. Bitte OPENWEATHER_API_KEY überprüfen."
  - code: 404
    message: "Stadt nicht gefunden. Bitte Schreibweise überprüfen."
  - code: 429
    message: "Zu viele Anfragen. Bitte später erneut versuchen."
  - code: 500
    message: "Wetterdienst vorübergehend nicht verfügbar."

rate_limit: 60
```

---

## Validierung

Die YAML-Datei wird automatisch mit Pydantic validiert:

### Validierungsfehler

**Fehlende Pflichtfelder:**
```
ValidationError: Field 'name' required
```

**Falscher Typ:**
```
ValidationError: Field 'timeout' must be integer
```

**Ungültiger Enum-Wert:**
```
ValidationError: Value 'invalid' not in enum [option1, option2]
```

### YAML-Syntax prüfen

Online: https://www.yamllint.com/
Oder mit yamllint:
```bash
pip install yamllint
yamllint backend/integrations/plugins/weather.yaml
```

---

## Best Practices

### 1. Naming
- Plugin name: `lowercase_with_underscores`
- Intent name: `plugin.action_name`
- Env vars: `UPPERCASE_WITH_UNDERSCORES`

### 2. Documentation
- Klare, prägnante Beschreibungen
- Mindestens 2-3 realistische Beispiele
- Dokumentiere alle Parameter ausführlich

### 3. Error Handling
- Mappe häufige Fehler (401, 404, 429, 500)
- Deutsche, benutzerfreundliche Nachrichten
- Gib Hinweise zur Fehlerbehebung

### 4. Rate Limiting
- Setze realistisches Limit basierend auf API-Provider
- Typische Werte: 60-180 requests/minute
- Dokumentiere im Plugin-README

### 5. Response Mapping
- Mappe nur benötigte Felder
- Verwende aussagekräftige Feld-Namen
- Teste Mapping mit echten API-Responses

### 6. Security
- NIE API-Keys ins YAML schreiben
- IMMER Umgebungsvariablen verwenden
- Config-Werte werden nicht URL-encoded

---

## Weitere Ressourcen

- **Plugin Development Guide:** [README.md](README.md)
- **YAML Tutorial:** https://learnxinyminutes.com/docs/yaml/
- **JSONPath Guide:** https://goessner.net/articles/JsonPath/
- **HTTP Status Codes:** https://httpstatuses.com/

---

**Happy Plugin Development! 🚀**
