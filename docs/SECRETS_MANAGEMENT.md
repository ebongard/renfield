# Secrets Management

Renfield unterstützt zwei Methoden zur Verwaltung von Secrets:

| Umgebung | Methode | Sicherheitslevel |
|----------|---------|------------------|
| Entwicklung | `.env` Datei | Niedrig (OK für lokale Entwicklung) |
| Produktion | Docker Compose File-Based Secrets | Hoch |

## Secrets-Übersicht

| Secret | Beschreibung | Secret-Datei |
|--------|-------------|--------------|
| `postgres_password` | PostgreSQL-Passwort | `secrets/postgres_password` |
| `home_assistant_token` | Home Assistant Long-Lived Access Token | `secrets/home_assistant_token` |
| `secret_key` | JWT-Signierung und Security Key | `secrets/secret_key` |
| `default_admin_password` | Initiales Admin-Passwort | `secrets/default_admin_password` |
| `openweather_api_key` | OpenWeatherMap API Key | `secrets/openweather_api_key` |
| `newsapi_key` | NewsAPI Key | `secrets/newsapi_key` |
| `jellyfin_api_key` | Jellyfin API Key | `secrets/jellyfin_api_key` |

## Produktion einrichten

### 1. Secrets generieren

```bash
./bin/generate-secrets.sh
```

Das Script erstellt das `secrets/` Verzeichnis und generiert:
- **Automatisch**: `postgres_password`, `secret_key`, `default_admin_password`
- **Interaktiv**: `home_assistant_token`, `openweather_api_key`, `newsapi_key`, `jellyfin_api_key`

Bereits vorhandene Secrets werden nicht überschrieben.

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
