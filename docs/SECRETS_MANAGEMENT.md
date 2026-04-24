# Secrets Management

Renfield unterstützt zwei Methoden zur Verwaltung von Secrets:

| Umgebung | Methode | Sicherheitslevel |
|----------|---------|------------------|
| Entwicklung | `.env` Datei | Niedrig (OK für lokale Entwicklung) |
| Produktion | Docker Compose File-Based Secrets | Hoch |

> **Upgrade-Hinweis (2026-04-24, PR #464):** `docker-compose.prod.yml` und `docker-compose.prod-cpu.yml` erwarten jetzt zwei zusätzliche Secret-Dateien: `secrets/jellyfin_user_id` und `secrets/presence_webhook_secret`. Fehlen diese, bricht `docker compose up` mit "secret file not found" ab. **Lösung:** `./bin/generate-secrets.sh` erneut laufen lassen — bestehende Secrets bleiben unangetastet, das Presence-Secret wird automatisch zufällig erzeugt, `jellyfin_user_id` wird interaktiv abgefragt; Leereingabe ist explizit erlaubt und legt eine leere Placeholder-Datei an, damit der Stack trotzdem startet (das jeweilige Feature bleibt dann deaktiviert bis ein echter Wert eingetragen wird). **k8s-Produktion ist nicht betroffen**, da dort keine Compose-Secrets, sondern k8s-Secrets verwendet werden.

## Secrets-Übersicht

| Secret | Beschreibung | Secret-Datei | Consumer |
|--------|-------------|--------------|----------|
| `postgres_password` | PostgreSQL-Passwort | `secrets/postgres_password` | Backend (`Settings.postgres_password`), Postgres-Container (`POSTGRES_PASSWORD_FILE`) |
| `secret_key` | JWT-Signierung und Security Key | `secrets/secret_key` | Backend (`Settings.secret_key` für `jwt.encode/decode`) |
| `default_admin_password` | Initiales Admin-Passwort | `secrets/default_admin_password` | Backend (`Settings.default_admin_password`, nur beim ersten Startup) |
| `home_assistant_token` | Home Assistant Long-Lived Access Token | `secrets/home_assistant_token` | HA-Glue (`HaGlueSettings.home_assistant_token`) + HA MCP-Server (`HOME_ASSISTANT_TOKEN` env) |
| `openweather_api_key` | OpenWeatherMap API Key | `secrets/openweather_api_key` | Weather MCP-Server (`OPENWEATHER_API_KEY` env) |
| `newsapi_key` | NewsAPI Key | `secrets/newsapi_key` | News MCP-Server (`NEWSAPI_KEY` env) |
| `jellyfin_api_key` | Jellyfin API Key | `secrets/jellyfin_api_key` | HA-Glue (`HaGlueSettings.jellyfin_api_key`) |
| `jellyfin_token` | Jellyfin MCP Token (= API Key für MCP-Server) | `secrets/jellyfin_token` | Jellyfin MCP-Server (`JELLYFIN_TOKEN` env) |
| `jellyfin_base_url` | Jellyfin Base URL (für MCP-Server) | `secrets/jellyfin_base_url` | Jellyfin MCP-Server (`JELLYFIN_BASE_URL` env) |
| `jellyfin_user_id` | Jellyfin User-GUID (für MCP-Server) | `secrets/jellyfin_user_id` | Jellyfin MCP-Server (`JELLYFIN_USER_ID` env) |
| `n8n_api_key` | n8n API Key (für MCP-Server) | `secrets/n8n_api_key` | Backend (`Settings.n8n_api_key`) + n8n MCP-Server (`N8N_API_KEY` env) |
| `paperless_api_token` | Paperless-NGX API Token | `secrets/paperless_api_token` | HA-Glue (`HaGlueSettings.paperless_api_token`) + Paperless MCP-Server |
| `mail_primary_password` | Primary-Mail IMAP/SMTP Passwort | `secrets/mail_primary_password` | Backend (`Settings.mail_primary_password`) + Mail MCP-Server |
| `presence_webhook_secret` | Shared-Secret für den `X-Webhook-Secret` Header ausgehender Presence-Webhooks | `secrets/presence_webhook_secret` | HA-Glue (`HaGlueSettings.presence_webhook_secret`) — wird vom `presence_webhook_dispatcher` gegen die konfigurierte `PRESENCE_WEBHOOK_URL` signiert |

**Hinweis zu optionalen Secrets**: `jellyfin_*`, `paperless_api_token`, `mail_primary_password`, `presence_webhook_secret` und die MCP-spezifischen API-Keys sind nur nötig, wenn die jeweilige Integration aktiviert ist. Fehlt eine Secret-Datei, bleibt das Feld in den Settings `None` und das Feature deaktiviert sich lautlos (kein Startup-Failure). **Ausnahmen**: `postgres_password`, `secret_key`, `default_admin_password` — diese werden vom Core gebraucht und müssen existieren.

## Produktion einrichten

### 1. Secrets generieren

```bash
./bin/generate-secrets.sh
```

Das Script erstellt das `secrets/` Verzeichnis und generiert:
- **Automatisch** (zufällig): `postgres_password`, `secret_key`, `default_admin_password`, `presence_webhook_secret`
- **Interaktiv**: `home_assistant_token`, `openweather_api_key`, `newsapi_key`, `jellyfin_api_key`, `jellyfin_token`, `jellyfin_base_url`, `jellyfin_user_id`, `n8n_api_key`, `paperless_api_token`, `mail_primary_password`

Bereits vorhandene Secrets werden nicht überschrieben — jedes mal ausführen ist sicher. Bei interaktiven Prompts bedeutet Leereingabe "überspringen" (Secret-Datei wird nicht angelegt, Feature bleibt deaktiviert).

### 2. Secrets aus .env entfernen

Entferne folgende Variablen aus der `.env` Datei auf dem Produktions-Server:

```bash
# Diese Zeilen entfernen:
POSTGRES_PASSWORD=...
HOME_ASSISTANT_TOKEN=...
SECRET_KEY=...
DEFAULT_ADMIN_PASSWORD=...
OPENWEATHER_API_KEY=...
NEWSAPI_KEY=...
JELLYFIN_API_KEY=...
JELLYFIN_TOKEN=...
JELLYFIN_BASE_URL=...
N8N_API_KEY=...
```

Nicht-sensitive Konfiguration (URLs, Model-Namen, Feature-Flags) bleibt in `.env`.

### 3. Stack starten

```bash
docker compose -f docker-compose.prod.yml up -d
```

### 4. Verifizieren

```bash
# Health Check
curl -sk https://localhost/health

# DB-Verbindung prüfen
docker exec renfield-backend python -c "from services.database import engine; print('DB OK')"
```

## Wie es funktioniert

### Pydantic SecretsSettingsSource

Der Backend verwendet Pydantic's eingebauten `SecretsSettingsSource`. In `config.py`:

```python
class Config:
    env_file = ".env"
    secrets_dir = "/run/secrets"
    case_sensitive = False
```

Docker Compose mountet Secret-Dateien nach `/run/secrets/`. Pydantic sucht automatisch nach `/run/secrets/<feldname>` für jedes Settings-Feld.

**Priorität** (höchste zuerst):
1. Environment-Variable (z.B. `POSTGRES_PASSWORD=...`)
2. Secret-Datei (`/run/secrets/postgres_password`)
3. Default-Wert aus `config.py`

### DATABASE_URL dynamisch

`DATABASE_URL` wird nicht mehr direkt in `docker-compose.prod.yml` gesetzt. Stattdessen baut `config.py` die URL aus Einzelteilen zusammen:

```python
@model_validator(mode="after")
def assemble_database_url(self) -> "Settings":
    if self.database_url is None:
        self.database_url = (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
    return self
```

Das Postgres-Passwort kommt aus `/run/secrets/postgres_password`, die anderen Felder aus `.env` oder Defaults.

### PostgreSQL POSTGRES_PASSWORD_FILE

Das offizielle PostgreSQL Docker-Image unterstützt nativ `POSTGRES_PASSWORD_FILE`:

```yaml
postgres:
  environment:
    POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
  secrets:
    - postgres_password
```

### MCP-Server und Docker Secrets

MCP-Server (stdio-Transport via `npx`) benötigen Secrets als Umgebungsvariablen. Da Pydantic `secrets_dir` Secrets nur in Settings-Felder lädt, aber **nicht** in `os.environ` injiziert, übernimmt `mcp_client.py` diese Aufgabe:

```python
# In MCPManager.load_config():
# Liest /run/secrets/* und setzt fehlende Variablen in os.environ,
# damit ${VAR} Substitution in mcp_servers.yaml funktioniert
# UND stdio-Subprozesse die Secrets erben.
```

**Betroffene Secrets für MCP-Server:**
- `openweather_api_key` → Weather MCP (`--apikey ${OPENWEATHER_API_KEY}`)
- `newsapi_key` → News MCP (via `NEWSAPI_KEY` env)
- `jellyfin_token` → Jellyfin MCP (via `JELLYFIN_TOKEN` env)
- `jellyfin_base_url` → Jellyfin MCP (via `JELLYFIN_BASE_URL` env)
- `jellyfin_user_id` → Jellyfin MCP (via `JELLYFIN_USER_ID` env)
- `n8n_api_key` → n8n MCP (via `N8N_API_KEY` env)
- `home_assistant_token` → HA MCP (via `HOME_ASSISTANT_TOKEN` auth header)
- `paperless_api_token` → Paperless MCP (via `PAPERLESS_API_TOKEN` env)
- `mail_primary_password` → Mail MCP (via `MAIL_PRIMARY_PASSWORD` env)

**Nicht MCP-bezogen, aber vom Backend konsumiert:**
- `presence_webhook_secret` → vom Backend direkt als `X-Webhook-Secret` Header gesetzt, wenn `PRESENCE_WEBHOOK_URL` konfiguriert ist. Geht an `ha_glue/services/presence_webhook.py::_dispatch`, dort via `.get_secret_value()` ausgelesen.

## Abwärtskompatibilität

- `.env`-basierte Secrets funktionieren weiterhin (höhere Priorität als Secret-Dateien)
- `docker-compose.yml` und `docker-compose.dev.yml` bleiben unverändert
- Nur `docker-compose.prod.yml` nutzt Docker Compose Secrets
- Migration ist optional — vorhandene Setups brechen nicht

## Entwicklung

Für die lokale Entwicklung reicht die `.env` Datei:

```bash
# .env (nur Entwicklung)
POSTGRES_PASSWORD=changeme
HOME_ASSISTANT_TOKEN=your_token
SECRET_KEY=dev-key
```

Keine Secret-Dateien nötig. Pydantic ignoriert `secrets_dir` wenn das Verzeichnis nicht existiert.

## Secret erneuern

```bash
# Einzelnes Secret neu generieren
rm secrets/postgres_password
./bin/generate-secrets.sh

# Stack neu starten
docker compose -f docker-compose.prod.yml restart backend postgres
```

## Sicherheitshinweise

- `secrets/` Verzeichnis ist in `.gitignore` — Secrets werden nie committed
- Secret-Dateien haben `chmod 600` (nur Owner lesen/schreiben)
- `secrets/` Verzeichnis hat `chmod 700`
- Docker Compose Secrets werden als tmpfs gemountet (nicht auf Disk)
- Default-Passwörter (`changeme`) **müssen** in Produktion geändert werden
