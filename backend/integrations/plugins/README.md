# Renfield Plugin System

Ein dynamisches, YAML-basiertes Plugin-System für einfache Integration externer Services.

## 🚀 Quick Start

### Plugin aktivieren (3 Schritte)

1. **YAML-Datei** liegt bereits in `backend/integrations/plugins/` (z.B. `weather.yaml`)
2. **Umgebungsvariablen** in `.env` setzen:
   ```bash
   WEATHER_ENABLED=true
   OPENWEATHER_API_URL=https://api.openweathermap.org/data/2.5
   OPENWEATHER_API_KEY=your_api_key_here
   ```
3. **Backend neu starten**:
   ```bash
   docker compose up -d --force-recreate backend
   ```

**Fertig!** Das Plugin ist jetzt aktiv. Keine Code-Änderungen nötig.

---

## 📦 Verfügbare Plugins

### 1. Weather Plugin (`weather.yaml`)

**Beschreibung:** Wetterdaten von OpenWeatherMap

**Intents:**
- `weather.get_current` - Aktuelles Wetter
- `weather.get_forecast` - Wettervorhersage

**Konfiguration:**
```bash
WEATHER_ENABLED=true
OPENWEATHER_API_URL=https://api.openweathermap.org/data/2.5
OPENWEATHER_API_KEY=<dein_api_key>
```

**API-Key erhalten:** https://openweathermap.org/api

**Beispiele:**
- "Wie ist das Wetter in Berlin?"
- "Wettervorhersage für München"

---

### 2. News Plugin (`news.yaml`)

**Beschreibung:** Nachrichten von NewsAPI

**Intents:**
- `news.get_headlines` - Top-Schlagzeilen
- `news.search` - Artikel-Suche

**Konfiguration:**
```bash
NEWS_ENABLED=true
NEWSAPI_URL=https://newsapi.org/v2
NEWSAPI_KEY=<dein_api_key>
```

**API-Key erhalten:** https://newsapi.org/register

**Beispiele:**
- "Zeige mir die Nachrichten"
- "Suche Nachrichten über Tesla"

---

### 3. Search Plugin (`search.yaml`)

**Beschreibung:** Web-Suche mit SearXNG Metasearch Engine (kein API-Key nötig!)

**Intents:**
- `search.web` - Web-Suche
- `search.instant_answer` - Schnelle Antworten

**Konfiguration:**
```bash
SEARCH_ENABLED=true
SEARXNG_API_URL=http://cuda.local:3002
```

**Hinweis:** Benötigt eine laufende SearXNG-Instanz auf dem angegebenen Host.

**Beispiele:**
- "Suche nach Python Tutorials"
- "Was ist Quantencomputing?"
- "Wie funktioniert Photosynthese?"

---

### 4. Music Plugin (`music.yaml`)

**Beschreibung:** Spotify-Steuerung

**Intents:**
- `music.search` - Musik suchen
- `music.play` - Abspielen
- `music.pause` - Pausieren
- `music.resume` - Fortsetzen
- `music.next` - Nächster Track
- `music.previous` - Vorheriger Track
- `music.volume` - Lautstärke setzen
- `music.current` - Aktuellen Track anzeigen

**Konfiguration:**
```bash
MUSIC_ENABLED=true
SPOTIFY_API_URL=https://api.spotify.com
SPOTIFY_ACCESS_TOKEN=<dein_access_token>
```

**Access Token erhalten:** https://developer.spotify.com/console/get-current-user/

**Beispiele:**
- "Spiele Musik von Coldplay"
- "Nächster Song"

---

## 🏗️ Eigenes Plugin erstellen

### Minimales Plugin-Beispiel

```yaml
name: my_plugin
version: 1.0.0
description: Mein erstes Plugin
author: Dein Name
enabled_var: MY_PLUGIN_ENABLED

config:
  url: MY_PLUGIN_API_URL
  api_key: MY_PLUGIN_API_KEY

intents:
  - name: my_plugin.do_something
    description: Führt eine Aktion aus
    parameters:
      - name: query
        type: string
        required: true
        description: Suchbegriff
    examples:
      - "Führe etwas aus"
      - "Mache das"
    api:
      method: GET
      url: "{config.url}/endpoint?q={params.query}&key={config.api_key}"
      timeout: 10
      response_mapping:
        result: "data.result"

error_mappings:
  - code: 401
    message: "API-Schlüssel ungültig"
  - code: 404
    message: "Nicht gefunden"

rate_limit: 60
```

### Plugin-Struktur Referenz

#### Metadata
```yaml
name: plugin_name              # Eindeutiger Name (lowercase, keine Punkte)
version: 1.0.0                 # Semantic Versioning
description: Beschreibung      # Was das Plugin tut
author: Name                   # Optional
enabled_var: PLUGIN_ENABLED    # Umgebungsvariable zum Aktivieren
```

#### Config
```yaml
config:
  url: API_URL_ENV_VAR         # Umgebungsvariable für API-URL
  api_key: API_KEY_ENV_VAR     # Umgebungsvariable für API-Key
  additional:                  # Optional: Weitere Config-Werte
    token: ACCESS_TOKEN_VAR
```

#### Intent Definition
```yaml
intents:
  - name: plugin.action          # Format: plugin_name.action_name
    description: Was tut dieser Intent
    parameters:
      - name: param_name
        type: string|integer|float|boolean|array|object
        required: true|false
        description: Parameter-Beschreibung
        default: default_value    # Optional
        enum: [value1, value2]    # Optional: Erlaubte Werte
        pattern: "^[A-Z]+$"       # Optional: Regex-Validierung
    examples:
      - "Beispiel-Anfrage 1"
      - "Beispiel-Anfrage 2"
    api:
      method: GET|POST|PUT|DELETE|PATCH
      url: "https://api.example.com/{params.query}"  # Template mit {config.x} und {params.y}
      headers:                    # Optional
        Authorization: "Bearer {config.token}"
      body:                       # Optional (nur für POST/PUT)
        key: "{params.value}"
      timeout: 10                 # Sekunden
      response_mapping:           # Optional: Extrahiere Daten
        field_name: "json.path.to.value"
        nested: "data[0].name"
```

#### Error Mappings
```yaml
error_mappings:
  - code: 401                    # HTTP Status Code
    message: "Benutzerfreundliche Fehlermeldung"
  - code: 429
    message: "Zu viele Anfragen"
```

#### Rate Limiting
```yaml
rate_limit: 60                   # Requests pro Minute (optional)
```

---

## 🎯 Template-Substitution

Verwende Platzhalter in URLs, Headers und Body:

### Config-Variablen
```yaml
url: "{config.api_key}"          # Aus Umgebungsvariablen
headers:
  Authorization: "Bearer {config.token}"
```

### Parameter-Variablen
```yaml
url: "https://api.example.com/search?q={params.query}"
body:
  city: "{params.location}"
  limit: "{params.limit}"
```

---

## 📊 Response Mapping

Extrahiere Daten aus API-Responses mit JSONPath-Notation:

### Einfacher Zugriff
```yaml
response_mapping:
  temperature: "main.temp"       # Greift auf response['main']['temp'] zu
  city: "name"                   # Greift auf response['name'] zu
```

### Array-Zugriff
```yaml
response_mapping:
  first_condition: "weather[0].description"  # response['weather'][0]['description']
  all_weather: "weather"                     # Gesamtes Array
```

### Nested Objects
```yaml
response_mapping:
  feels_like: "main.feels_like"
  wind_speed: "wind.speed"
  sunrise: "sys.sunrise"
```

---

## 🔧 Debugging & Troubleshooting

### Plugin lädt nicht

**Problem:** Plugin wird nicht gefunden
```bash
docker logs renfield-backend | grep plugin
```

**Lösung:**
- Prüfe, dass `.yaml` Datei in `backend/integrations/plugins/` liegt
- Prüfe YAML-Syntax: https://www.yamllint.com/
- Prüfe, dass `enabled_var` in `.env` auf `true` gesetzt ist

---

### Fehlende Umgebungsvariablen

**Problem:** Plugin disabled trotz `ENABLED=true`
```
⚠️  Plugin 'weather' has missing config vars: ['OPENWEATHER_API_KEY']
```

**Lösung:**
1. Füge Variable zu `.env` hinzu
2. Container neu erstellen:
   ```bash
   docker compose up -d --force-recreate backend
   ```
3. Prüfe, ob Variable im Container ist:
   ```bash
   docker exec renfield-backend env | grep WEATHER
   ```

---

### API-Fehler

**Problem:** Plugin gibt Fehler zurück
```
❌ API error: 401 Unauthorized
```

**Lösung:**
- Prüfe API-Key Gültigkeit
- Prüfe API-URL (kein Tippfehler?)
- Teste API mit curl:
  ```bash
  curl "https://api.openweathermap.org/data/2.5/weather?q=Berlin&appid=YOUR_KEY"
  ```

---

### Rate Limit erreicht

**Problem:** Zu viele Anfragen
```
⚠️  Rate limit exceeded for weather
```

**Lösung:**
- Warte 1 Minute
- Erhöhe `rate_limit` in YAML (wenn API es erlaubt)
- Upgrade API-Plan bei Provider

---

## 🧪 Testing

### Plugin-System testen
```bash
docker exec renfield-backend python3 /app/test_plugins.py
```

### Error Handling testen
```bash
docker exec renfield-backend python3 /app/test_error_handling.py
```

### Performance testen
```bash
docker exec renfield-backend python3 /app/test_performance.py
```

---

## 📝 Best Practices

### 1. Naming Conventions
- **Plugin Name:** lowercase, keine Punkte (`weather`, nicht `Weather.Plugin`)
- **Intent Name:** `plugin_name.action` (`weather.get_current`)
- **Env Var:** `UPPERCASE_SNAKE_CASE` (`WEATHER_ENABLED`)

### 2. Error Messages
- Schreibe benutzerfreundliche Fehlermeldungen auf Deutsch
- Mappe häufige HTTP-Fehler (401, 404, 429, 500)

### 3. Response Mapping
- Mappe nur benötigte Felder
- Verwende aussagekräftige Feld-Namen
- Teste Mapping mit echten API-Responses

### 4. Rate Limiting
- Setze realistisches Limit (basierend auf API-Provider)
- Typische Werte: 60-180 requests/minute

### 5. Examples
- Gib 2-5 realistische Beispiel-Anfragen
- Verwende natürliche deutsche Sprache
- Decke verschiedene Parameter-Kombinationen ab

### 6. Documentation
- Dokumentiere alle Parameter klar
- Erkläre, wo man API-Keys bekommt
- Gib Beispiel-Konfiguration an

---

## 🔐 Sicherheit

### API-Keys
- **NIE** API-Keys direkt ins YAML schreiben
- **IMMER** Umgebungsvariablen verwenden
- Keys in `.env` speichern (nicht in Git committen)

### Validierung
- Alle Parameter werden automatisch validiert
- Type-Checking (string, integer, boolean)
- Enum-Validierung
- Regex-Pattern-Validierung

### Rate Limiting
- Schützt vor zu vielen API-Anfragen
- Sliding-Window-Algorithmus
- Pro Plugin konfigurierbar

---

## 📚 Weiterführende Links

- **OpenWeatherMap API:** https://openweathermap.org/api
- **NewsAPI:** https://newsapi.org/docs
- **SearXNG:** https://docs.searxng.org/
- **Spotify Web API:** https://developer.spotify.com/documentation/web-api
- **YAML Tutorial:** https://learnxinyminutes.com/docs/yaml/
- **JSONPath Guide:** https://goessner.net/articles/JsonPath/

---

## 🆘 Support

### Logs anschauen
```bash
docker logs renfield-backend --tail 100 -f
```

### Plugin-Registry anzeigen
```bash
docker exec renfield-backend python3 /app/test_plugins.py
```

### Container-Umgebung prüfen
```bash
docker exec renfield-backend env | sort
```

---

## 📊 Performance-Benchmarks

Basierend auf Tests mit dem Weather-Plugin:

- **Plugin Loading:** ~40ms (für 4 Plugins)
- **API Latency:** ~43ms (Durchschnitt)
- **Concurrent Requests:** 27x Speedup
- **Memory Overhead:** < 5 MB pro Plugin

---

## 🎉 Plugin-Beispiele

### Einfaches GET-Request Plugin
```yaml
name: quotes
version: 1.0.0
description: Zufällige Zitate
enabled_var: QUOTES_ENABLED

config:
  url: QUOTES_API_URL

intents:
  - name: quotes.random
    description: Zeigt ein zufälliges Zitat
    parameters: []
    examples:
      - "Zeige mir ein Zitat"
    api:
      method: GET
      url: "{config.url}/random"
      timeout: 5
      response_mapping:
        text: "content"
        author: "author"
```

### POST-Request mit Body
```yaml
name: translator
version: 1.0.0
description: Text übersetzen
enabled_var: TRANSLATOR_ENABLED

config:
  url: TRANSLATOR_API_URL
  api_key: TRANSLATOR_API_KEY

intents:
  - name: translator.translate
    description: Übersetzt Text
    parameters:
      - name: text
        type: string
        required: true
      - name: target_lang
        type: string
        required: false
        default: en
        enum: [de, en, fr, es]
    examples:
      - "Übersetze 'Hallo Welt' ins Englische"
    api:
      method: POST
      url: "{config.url}/translate"
      headers:
        Authorization: "Bearer {config.api_key}"
        Content-Type: "application/json"
      body:
        text: "{params.text}"
        target: "{params.target_lang}"
      timeout: 10
      response_mapping:
        translated: "result.text"
        detected_language: "result.detected_language"
```

---

**Viel Erfolg beim Erstellen eigener Plugins! 🚀**
