# Environment Variables Guide

Vollständige Referenz aller Umgebungsvariablen für Renfield.

---

## 📋 Inhaltsverzeichnis

- [Naming Conventions](#naming-conventions)
- [Core System](#core-system)
- [Integrationen](#integrationen)
- [Plugin System](#plugin-system)
- [Verfügbare Plugins](#verfügbare-plugins)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

---

## Naming Conventions

### Plugin-spezifische Variablen

**Format:**
```
{PLUGIN_NAME}_{PURPOSE}
```

**Beispiele:**
```bash
WEATHER_ENABLED=true              # Plugin aktivieren/deaktivieren
WEATHER_API_URL=https://...       # API-URL
WEATHER_API_KEY=abc123            # API-Schlüssel
```

### Regeln

1. **UPPERCASE_SNAKE_CASE** - Alle Buchstaben groß, Wörter mit Unterstrich getrennt
2. **Beschreibende Namen** - Klar erkennbar, wofür die Variable ist
3. **Konsistente Suffixe:**
   - `_ENABLED` - Boolean zum Aktivieren
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
OLLAMA_MODEL=llama3.2:3b
OLLAMA_MODEL=gpt-oss:latest
```

**Defaults:**
- `OLLAMA_URL`: `http://ollama:11434`
- `OLLAMA_MODEL`: `llama3.2:3b`

**Verfügbare Modelle:**
- `llama3.2:3b` - Schnell, wenig RAM (Empfohlen)
- `llama3.2:7b` - Bessere Qualität, mehr RAM
- `mixtral:8x7b` - Hohe Qualität, viel RAM
- `gpt-oss:latest` - Custom/Fine-tuned Modell

---

### Sprache & Voice

```bash
# Standard-Sprache
DEFAULT_LANGUAGE=de

# Whisper STT Modell
WHISPER_MODEL=base

# Piper TTS Voice
PIPER_VOICE=de_DE-thorsten-high
```

**Defaults:**
- `DEFAULT_LANGUAGE`: `de`
- `WHISPER_MODEL`: `base`
- `PIPER_VOICE`: `de_DE-thorsten-high`

**Whisper Modelle:**
- `tiny` - Sehr schnell, niedrige Qualität
- `base` - Schnell, gute Qualität (Empfohlen)
- `small` - Langsamer, bessere Qualität
- `medium` - Langsam, hohe Qualität
- `large` - Sehr langsam, beste Qualität

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

### Security

```bash
# Secret Key für Sessions/JWT
SECRET_KEY=changeme-in-production-use-strong-random-key
```

**Default:** `changeme-in-production-use-strong-random-key`
**Hinweis:** In Produktion IMMER durch starken Zufallsschlüssel ersetzen!

**Generierung:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
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

## Plugin System

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
WEATHER_ENABLED=true

# API-Konfiguration
OPENWEATHER_API_URL=https://api.openweathermap.org/data/2.5
OPENWEATHER_API_KEY=your_api_key_here
```

**Erforderlich:**
- `WEATHER_ENABLED` - Boolean
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
NEWS_ENABLED=true

# API-Konfiguration
NEWSAPI_URL=https://newsapi.org/v2
NEWSAPI_KEY=your_api_key_here
```

**Erforderlich:**
- `NEWS_ENABLED` - Boolean
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

### Search Plugin (DuckDuckGo)

```bash
# Plugin aktivieren
SEARCH_ENABLED=true

# API-URL (kein Key nötig!)
DUCKDUCKGO_API_URL=https://api.duckduckgo.com
```

**Erforderlich:**
- `SEARCH_ENABLED` - Boolean
- `DUCKDUCKGO_API_URL` - API-Basis-URL

**API-Key:** Nicht erforderlich! ✅

**Intents:**
- `search.web` - Web-Suche
- `search.instant_answer` - Instant Answers

---

### Music Plugin (Spotify)

```bash
# Plugin aktivieren
MUSIC_ENABLED=true

# API-Konfiguration
SPOTIFY_API_URL=https://api.spotify.com
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_ACCESS_TOKEN=your_access_token
```

**Erforderlich:**
- `MUSIC_ENABLED` - Boolean
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

**Problem:** `ENABLED` Variable nicht gesetzt

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
{PLUGIN_NAME}_ENABLED=false

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
# Ollama LLM
# -----------------------------------------------------------------------------
OLLAMA_URL=http://cuda.local:11434
OLLAMA_MODEL=gpt-oss:latest

# -----------------------------------------------------------------------------
# Sprache & Voice
# -----------------------------------------------------------------------------
DEFAULT_LANGUAGE=de
WHISPER_MODEL=base
PIPER_VOICE=de_DE-thorsten-high

# -----------------------------------------------------------------------------
# Integrationen
# -----------------------------------------------------------------------------
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=eyJhbGci...

N8N_WEBHOOK_URL=http://192.168.1.78:5678/webhook

FRIGATE_URL=http://frigate.local:5000

# -----------------------------------------------------------------------------
# Plugin System
# -----------------------------------------------------------------------------
PLUGINS_ENABLED=true

# Weather Plugin (OpenWeatherMap)
WEATHER_ENABLED=true
OPENWEATHER_API_URL=https://api.openweathermap.org/data/2.5
OPENWEATHER_API_KEY=your_key_here

# News Plugin (NewsAPI)
NEWS_ENABLED=false
NEWSAPI_URL=https://newsapi.org/v2
NEWSAPI_KEY=your_key_here

# Search Plugin (DuckDuckGo)
SEARCH_ENABLED=true
DUCKDUCKGO_API_URL=https://api.duckduckgo.com

# Music Plugin (Spotify)
MUSIC_ENABLED=false
SPOTIFY_API_URL=https://api.spotify.com
SPOTIFY_ACCESS_TOKEN=your_token_here
```

---

**Hinweis:** Passe die Werte an deine Umgebung an und committe NIE echte Secrets ins Repository!
