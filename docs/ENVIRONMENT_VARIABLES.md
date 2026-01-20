# Environment Variables Guide

Vollständige Referenz aller Umgebungsvariablen für Renfield.

---

## 📋 Inhaltsverzeichnis

- [Naming Conventions](#naming-conventions)
- [Core System](#core-system)
- [Audio Output Routing](#audio-output-routing)
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

### Search Plugin (SearXNG)

```bash
# Plugin aktivieren
SEARCH_ENABLED=true

# SearXNG-Instanz URL (kein Key nötig!)
SEARXNG_API_URL=http://cuda.local:3002
```

**Erforderlich:**
- `SEARCH_ENABLED` - Boolean
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

### Jellyfin Plugin (DLNA/UPnP Media Server)

```bash
# Plugin aktivieren
JELLYFIN_ENABLED=true

# API-Konfiguration
JELLYFIN_URL=http://192.168.1.123:8096
JELLYFIN_API_KEY=your_api_key_here
JELLYFIN_USER_ID=your_user_id_here
```

**Erforderlich:**
- `JELLYFIN_ENABLED` - Boolean
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

# Weather Plugin (OpenWeatherMap)
WEATHER_ENABLED=true
OPENWEATHER_API_URL=https://api.openweathermap.org/data/2.5
OPENWEATHER_API_KEY=your_key_here

# News Plugin (NewsAPI)
NEWS_ENABLED=false
NEWSAPI_URL=https://newsapi.org/v2
NEWSAPI_KEY=your_key_here

# Search Plugin (SearXNG)
SEARCH_ENABLED=true
SEARXNG_API_URL=http://cuda.local:3002

# Music Plugin (Spotify)
MUSIC_ENABLED=false
SPOTIFY_API_URL=https://api.spotify.com
SPOTIFY_ACCESS_TOKEN=your_token_here
```

---

**Hinweis:** Passe die Werte an deine Umgebung an und committe NIE echte Secrets ins Repository!
