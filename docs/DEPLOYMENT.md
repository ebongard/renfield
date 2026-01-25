# Produktives Deployment von Renfield

Diese Anleitung beschreibt das Deployment von Renfield auf einem Server mit NVIDIA GPU.

**Zielserver:** `renfield.local`

---

## Voraussetzungen

- Ubuntu 22.04 LTS (oder vergleichbar)
- Docker und Docker Compose
- NVIDIA GPU mit installierten Treibern
- Mindestens 8GB RAM
- Mindestens 50GB Festplattenspeicher

---

## Schritt 1: Server vorbereiten

### 1.1 SSH-Verbindung herstellen

```bash
ssh user@renfield.local
```

### 1.2 System aktualisieren

```bash
sudo apt update && sudo apt upgrade -y
```

### 1.3 NVIDIA-Treiber prüfen

```bash
nvidia-smi
```

Sollte GPU-Informationen anzeigen. Falls nicht installiert:

```bash
sudo apt install nvidia-driver-535 -y
sudo reboot
```

---

## Schritt 2: NVIDIA Container Toolkit installieren

```bash
# GPG Key hinzufügen
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

# Repository hinzufügen
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Toolkit installieren
sudo apt update
sudo apt install -y nvidia-container-toolkit

# Docker für NVIDIA konfigurieren
sudo nvidia-ctk runtime configure --runtime=docker

# Docker neu starten
sudo systemctl restart docker
```

### 2.1 GPU-Zugriff in Docker testen

```bash
   docker run  --rm --gpus all nvidia/cuda:12.9.0-runtime-ubuntu22.04  nvidia-smi
```

Sollte die GPU-Informationen anzeigen.

---

## Schritt 3: Renfield Repository klonen

```bash
# Projektverzeichnis erstellen
sudo mkdir -p /opt/renfield
sudo chown $USER:$USER /opt/renfield
cd /opt/renfield

# Repository klonen
git clone https://github.com/ebongard/renfield.git .
```

---

## Schritt 4: Umgebungsvariablen konfigurieren

### 4.1 .env-Datei erstellen

```bash
cp .env.example .env
nano .env
```

### 4.2 Wichtige Einstellungen anpassen

```bash
# =============================================================================
# PRODUKTION - renfield.local
# =============================================================================

# -----------------------------------------------------------------------------
# Datenbank (WICHTIG: Sicheres Passwort setzen!)
# -----------------------------------------------------------------------------
POSTGRES_PASSWORD=HIER_SICHERES_PASSWORT_EINSETZEN

# -----------------------------------------------------------------------------
# Security (WICHTIG: Starken Key generieren!)
# -----------------------------------------------------------------------------
SECRET_KEY=HIER_STARKEN_KEY_EINSETZEN
CORS_ORIGINS=https://renfield.local,http://renfield.local

# -----------------------------------------------------------------------------
# Authentifizierung
# -----------------------------------------------------------------------------
AUTH_ENABLED=true
ALLOW_REGISTRATION=false
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=HIER_ADMIN_PASSWORT_EINSETZEN

# WebSocket Security
WS_AUTH_ENABLED=true
WS_RATE_LIMIT_ENABLED=true

# -----------------------------------------------------------------------------
# Ollama LLM (lokal mit GPU)
# -----------------------------------------------------------------------------
OLLAMA_URL=http://ollama:11434
OLLAMA_CHAT_MODEL=llama3.2:3b
OLLAMA_RAG_MODEL=llama3.1:8b
OLLAMA_EMBED_MODEL=nomic-embed-text

# -----------------------------------------------------------------------------
# Sprache & Voice
# -----------------------------------------------------------------------------
DEFAULT_LANGUAGE=de
SUPPORTED_LANGUAGES=de,en
WHISPER_MODEL=base
PIPER_VOICE=de_DE-thorsten-high

# -----------------------------------------------------------------------------
# Home Assistant Integration
# -----------------------------------------------------------------------------
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=DEIN_HA_TOKEN_HIER

# -----------------------------------------------------------------------------
# Zeroconf/mDNS für Satellites
# -----------------------------------------------------------------------------
ADVERTISE_HOST=renfield.local
ADVERTISE_PORT=8000

# -----------------------------------------------------------------------------
# Frigate (optional)
# -----------------------------------------------------------------------------
# FRIGATE_URL=http://frigate.local:5000

# -----------------------------------------------------------------------------
# n8n (optional)
# -----------------------------------------------------------------------------
# N8N_WEBHOOK_URL=http://n8n.local:5678/webhook

# -----------------------------------------------------------------------------
# RAG (Knowledge Base)
# -----------------------------------------------------------------------------
RAG_ENABLED=true
UPLOAD_DIR=/app/data/uploads
MAX_FILE_SIZE_MB=50

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
LOG_LEVEL=INFO
```

### 4.3 Sichere Werte generieren

```bash
# Secret Key generieren
python3 -c "import secrets; print(secrets.token_urlsafe(64))"

# Passwort generieren
openssl rand -base64 32
```

---

## Schritt 5: SSL-Zertifikate erstellen

### Option A: Selbstsigniertes Zertifikat (schnell)

```bash
mkdir -p config/ssl

# Zertifikat erstellen
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout config/ssl/key.pem \
  -out config/ssl/cert.pem \
  -subj "/CN=renfield.local"
```

### Option B: Let's Encrypt (empfohlen für öffentliche Server)

```bash
# Certbot installieren
sudo apt install certbot -y

# Zertifikat anfordern (Port 80 muss erreichbar sein)
sudo certbot certonly --standalone -d renfield.local

# Zertifikate kopieren
sudo cp /etc/letsencrypt/live/renfield.local/fullchain.pem config/ssl/cert.pem
sudo cp /etc/letsencrypt/live/renfield.local/privkey.pem config/ssl/key.pem
sudo chown $USER:$USER config/ssl/*.pem
```

---

## Schritt 6: Nginx-Konfiguration anpassen (optional)

Falls nur HTTP (kein SSL) gewünscht:

```bash
nano config/nginx.conf
```

Ersetze den Inhalt mit:

```nginx
events {
    worker_connections 1024;
}

http {
    upstream backend {
        server backend:8000;
    }

    upstream frontend {
        server frontend:3000;
    }

    server {
        listen 80;
        server_name renfield.local;

        # Frontend
        location / {
            proxy_pass http://frontend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        # Backend API
        location /api {
            proxy_pass http://backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            client_max_body_size 100M;
        }

        # WebSocket
        location /ws {
            proxy_pass http://backend;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_set_header Host $host;
            proxy_read_timeout 86400;
        }

        # Health Check
        location /health {
            proxy_pass http://backend;
            access_log off;
        }
    }
}
```

---

## Schritt 7: Renfield starten

### 7.1 Mit lokalem Ollama (GPU)

```bash
docker compose -f docker-compose.prod.yml --profile ollama-gpu up -d
```

### 7.2 Ohne Ollama (externe Instanz)

Falls Ollama auf einem anderen Server läuft:

```bash
# In .env setzen:
# OLLAMA_URL=http://cuda.local:11434

docker compose -f docker-compose.prod.yml up -d
```

### 7.3 Build-Prozess verfolgen

```bash
docker compose -f docker-compose.prod.yml logs -f
```

---

## Schritt 8: Ollama-Modelle herunterladen

```bash
# In den Ollama-Container
docker exec -it renfield-ollama ollama pull llama3.2:3b
docker exec -it renfield-ollama ollama pull llama3.1:8b
docker exec -it renfield-ollama ollama pull nomic-embed-text

# Modelle prüfen
docker exec -it renfield-ollama ollama list
```

---

## Schritt 9: Installation prüfen

### 9.1 Container-Status

```bash
docker compose -f docker-compose.prod.yml ps
```

Alle Container sollten "Up" sein.

### 9.2 Health-Check

```bash
curl http://renfield.local/health
```

Sollte `{"status":"healthy"}` zurückgeben.

### 9.3 GPU-Nutzung prüfen

```bash
# Auf dem Host
nvidia-smi

# Im Backend-Container
docker exec renfield-backend python -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}')"
```

---

## Schritt 10: Erster Login

1. Browser öffnen: `http://renfield.local` (oder `https://`)
2. Mit Admin-Credentials anmelden:
   - Username: `admin` (oder wie in .env konfiguriert)
   - Passwort: wie in .env konfiguriert
3. **WICHTIG:** Admin-Passwort sofort ändern!

---

## Schritt 11: Systemd-Service (optional)

Für automatischen Start bei Systemboot:

```bash
sudo nano /etc/systemd/system/renfield.service
```

```ini
[Unit]
Description=Renfield AI Assistant
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/renfield
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.prod.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable renfield
```

---

## Wartung

### Logs anzeigen

```bash
cd /opt/renfield
docker compose -f docker-compose.prod.yml logs -f backend
docker compose -f docker-compose.prod.yml logs -f --tail=100
```

### Neustart

```bash
docker compose -f docker-compose.prod.yml restart
```

### Update

```bash
cd /opt/renfield
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

### Backup

```bash
# Datenbank-Backup
docker exec renfield-postgres pg_dump -U renfield renfield > backup_$(date +%Y%m%d).sql

# Volumes sichern
docker run --rm -v renfield_postgres_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/postgres_data_$(date +%Y%m%d).tar.gz /data
```

### Datenbank wiederherstellen

```bash
docker exec -i renfield-postgres psql -U renfield renfield < backup_YYYYMMDD.sql
```

---

## Troubleshooting

### Container startet nicht

```bash
docker compose -f docker-compose.prod.yml logs backend
```

### GPU nicht erkannt

```bash
# NVIDIA Container Runtime prüfen
docker info | grep -i nvidia

# GPU im Container testen
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

### Whisper/Piper laden langsam

Beim ersten Start werden die Modelle heruntergeladen. Das kann einige Minuten dauern.

```bash
# Whisper-Modelle prüfen
docker exec renfield-backend ls -la /root/.cache/whisper/

# Piper-Stimmen prüfen
docker exec renfield-backend ls -la /root/.local/share/piper/
```

### WebSocket-Verbindungsfehler

```bash
# Nginx-Logs prüfen
docker logs renfield-nginx

# WebSocket-Konfiguration in nginx.conf prüfen
```

### Ollama nicht erreichbar

```bash
# Ollama-Container prüfen
docker logs renfield-ollama

# Verbindung testen
docker exec renfield-backend curl http://ollama:11434/api/tags
```

---

## Sicherheitshinweise

1. **Passwörter:** Alle Default-Passwörter in `.env` ändern
2. **Firewall:** Nur Port 80/443 von außen erreichbar machen
3. **SSL:** In Produktion immer HTTPS verwenden
4. **Updates:** Regelmäßig `git pull` und Container rebuilden
5. **Backups:** Datenbank regelmäßig sichern
6. **Monitoring:** Container-Logs überwachen

---

## Ressourcenverbrauch

| Komponente | RAM | GPU-VRAM | CPU |
|------------|-----|----------|-----|
| PostgreSQL | ~200MB | - | niedrig |
| Redis | ~50MB | - | niedrig |
| Backend | ~2GB | ~2-4GB | mittel |
| Frontend | ~100MB | - | niedrig |
| Ollama | ~4-8GB | ~4-8GB | hoch bei Inferenz |
| Nginx | ~20MB | - | niedrig |
| **Gesamt** | **~7-11GB** | **~6-12GB** | - |

---

## Nächste Schritte

1. [ ] Home Assistant Token erstellen und in .env eintragen
2. [ ] Satellites einrichten (siehe `src/satellite/README.md`)
3. [ ] Wissensbasis mit Dokumenten füllen
4. [ ] Weitere Benutzer anlegen
5. [ ] Räume konfigurieren

---

Bei Fragen: https://github.com/ebongard/renfield/issues
