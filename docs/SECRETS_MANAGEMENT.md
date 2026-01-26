# Secrets Management

Dieses Dokument beschreibt Best Practices für die Verwaltung von Secrets in Renfield.

## Entwicklung vs. Produktion

| Umgebung | Methode | Sicherheitslevel |
|----------|---------|------------------|
| Entwicklung | `.env` Datei | Niedrig (OK für lokale Entwicklung) |
| Produktion | Docker Secrets / Vault | Hoch |

## Aktuelle Secrets in `.env`

```bash
# Datenbank
POSTGRES_PASSWORD=changeme

# JWT
SECRET_KEY=changeme-in-production-use-strong-random-key

# Home Assistant
HOME_ASSISTANT_TOKEN=your-long-lived-access-token

# Optional: API Keys
OPENWEATHER_API_KEY=...
NEWSAPI_KEY=...
SPOTIFY_CLIENT_SECRET=...
```

## Option 1: Docker Secrets (empfohlen für Docker Swarm)

Docker Secrets sind die native Lösung für Docker Swarm Deployments.

### Secrets erstellen

```bash
# Secret aus Datei erstellen
echo "my-secure-password" | docker secret create postgres_password -
echo "my-jwt-secret-key" | docker secret create jwt_secret_key -
echo "ha-token" | docker secret create ha_token -

# Secrets auflisten
docker secret ls
```

### docker-compose.prod.yml anpassen

```yaml
version: '3.8'

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets:
      - postgres_password

  backend:
    image: renfield-backend
    environment:
      DATABASE_URL_FILE: /run/secrets/database_url
      SECRET_KEY_FILE: /run/secrets/jwt_secret_key
      HOME_ASSISTANT_TOKEN_FILE: /run/secrets/ha_token
    secrets:
      - database_url
      - jwt_secret_key
      - ha_token

secrets:
  postgres_password:
    external: true
  database_url:
    external: true
  jwt_secret_key:
    external: true
  ha_token:
    external: true
```

### Backend-Code anpassen

Der Backend-Code muss angepasst werden, um `*_FILE` Environment-Variablen zu unterstützen:

```python
# utils/config.py
import os

def get_secret(env_var: str, default: str = None) -> str:
    """
    Liest Secret aus Environment-Variable oder Datei.

    Unterstützt:
    - Direkte Werte: SECRET_KEY=value
    - Datei-Referenz: SECRET_KEY_FILE=/run/secrets/secret_key
    """
    # Prüfe ob _FILE Version existiert
    file_env = f"{env_var}_FILE"
    if file_path := os.environ.get(file_env):
        try:
            with open(file_path, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            pass

    # Fallback auf direkte Environment-Variable
    return os.environ.get(env_var, default)
```

## Option 2: HashiCorp Vault

Für komplexere Setups mit Rotation, Audit-Logs und feingranularer Zugriffskontrolle.

### Vault Setup

```bash
# Vault Container starten
docker run -d --name vault \
  -p 8200:8200 \
  -e 'VAULT_DEV_ROOT_TOKEN_ID=myroot' \
  -e 'VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200' \
  vault

# Secrets speichern
vault kv put secret/renfield \
  postgres_password="secure-password" \
  jwt_secret="jwt-secret-key" \
  ha_token="home-assistant-token"
```

### Python-Integration

```python
import hvac

client = hvac.Client(url='http://vault:8200', token='myroot')
secrets = client.secrets.kv.v2.read_secret_version(path='renfield')
postgres_password = secrets['data']['data']['postgres_password']
```

## Option 3: Kubernetes Secrets

Für Kubernetes-Deployments.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: renfield-secrets
type: Opaque
stringData:
  postgres-password: "secure-password"
  jwt-secret: "jwt-secret-key"
  ha-token: "home-assistant-token"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: renfield-backend
spec:
  template:
    spec:
      containers:
      - name: backend
        env:
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: renfield-secrets
              key: postgres-password
```

## Empfehlungen

### Für Heimnetzwerk (Self-Hosted)

1. **Minimum**: Starke Passwörter in `.env`, Datei nicht committen
2. **Besser**: Docker Secrets mit Docker Compose
3. **Am besten**: Vault für automatische Rotation

### Für Cloud/Produktion

1. **AWS**: AWS Secrets Manager + IAM Roles
2. **GCP**: Google Secret Manager
3. **Azure**: Azure Key Vault
4. **Kubernetes**: External Secrets Operator + Vault/Cloud Provider

### Checkliste für Produktion

- [ ] Alle Default-Passwörter geändert (`changeme`)
- [ ] `.env` nicht in Git committed (in `.gitignore`)
- [ ] Secrets nicht in Docker Images
- [ ] Secrets-Rotation eingerichtet
- [ ] Audit-Logging für Secret-Zugriffe
- [ ] Backup für Secrets (verschlüsselt)

## Secrets generieren

```bash
# Sicheres Passwort generieren
openssl rand -base64 32

# JWT Secret Key generieren
python -c "import secrets; print(secrets.token_urlsafe(64))"

# Vollständige .env für Produktion generieren
cat > .env.production << EOF
POSTGRES_PASSWORD=$(openssl rand -base64 32)
SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))")
HOME_ASSISTANT_TOKEN=<your-token-here>
EOF
```

## Referenzen

- [Docker Secrets Documentation](https://docs.docker.com/engine/swarm/secrets/)
- [HashiCorp Vault](https://www.vaultproject.io/)
- [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/)
