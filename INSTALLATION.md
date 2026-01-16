# Installation & Setup Guide

Dieser Guide führt dich Schritt für Schritt durch die Installation von Renfield.

## 📋 Systemanforderungen

### Minimum
- **CPU**: 4 Cores
- **RAM**: 16 GB
- **Speicher**: 50 GB frei
- **OS**: Linux (Ubuntu 22.04+, Debian 11+) oder macOS
- **Docker**: Version 24+
- **Docker Compose**: Version 2.0+

### Empfohlen
- **CPU**: 8+ Cores
- **RAM**: 32 GB
- **Speicher**: 100 GB+ SSD
- **GPU**: NVIDIA GPU mit CUDA Support (optional, für bessere Performance)

## 🛠️ Installation

### 1. System vorbereiten

#### Ubuntu/Debian
```bash
# System aktualisieren
sudo apt update && sudo apt upgrade -y

# Docker installieren
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Docker Compose installieren
sudo apt install docker-compose-plugin

# User zur Docker-Gruppe hinzufügen
sudo usermod -aG docker $USER
newgrp docker

# Git installieren
sudo apt install git
```

#### macOS
```bash
# Docker Desktop installieren
# Lade von https://www.docker.com/products/docker-desktop

# Homebrew installieren (falls nicht vorhanden)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Git installieren
brew install git
```

### 2. Repository klonen

```bash
git clone <your-repository-url> renfield
cd renfield
```

### 3. Umgebung konfigurieren

```bash
# .env Datei erstellen
cp .env.example .env

# Datei bearbeiten
nano .env  # oder vim, code, etc.
```

**Wichtige Einstellungen:**

```env
# PostgreSQL Passwort ändern!
POSTGRES_PASSWORD=dein_sicheres_passwort_hier

# Home Assistant konfigurieren
HOME_ASSISTANT_URL=http://192.168.1.100:8123
HOME_ASSISTANT_TOKEN=dein_long_lived_token

# n8n (falls vorhanden)
N8N_WEBHOOK_URL=http://192.168.1.100:5678/webhook

# Frigate (falls vorhanden)
FRIGATE_URL=http://192.168.1.100:5000

# Sprache und Modelle
DEFAULT_LANGUAGE=de
OLLAMA_MODEL=llama3.2:3b
WHISPER_MODEL=base
PIPER_VOICE=de_DE-thorsten-high
```

### 4. Home Assistant Token erstellen

1. Öffne Home Assistant
2. Gehe zu deinem Profil (unten links)
3. Scrolle zu "Lange Zugangstoken"
4. Klicke "Token erstellen"
5. Kopiere den Token in deine `.env` Datei

### 5. Services starten

```bash
# Alle Container im Hintergrund starten
docker-compose up -d

# Logs verfolgen
docker-compose logs -f
```

### 6. Ollama Modell laden

```bash
# Warte bis Ollama Container läuft (ca. 30 Sekunden)
docker exec -it renfield-ollama ollama pull llama3.2:3b

# Alternativ für bessere Qualität (größer):
# docker exec -it renfield-ollama ollama pull llama3.1:8b
```

### 7. Installation testen

```bash
# Health Check
curl http://localhost:8000/health

# Sollte zurückgeben:
# {"status":"healthy","services":{"ollama":"ok","database":"ok","redis":"ok"}}
```

### 8. Web-Interface öffnen

Öffne in deinem Browser:
```
http://localhost:3000
```

## 🎯 Erste Schritte

### Test 1: Chat ohne Home Assistant

1. Gehe zu **Chat**
2. Schreibe: "Hallo, wer bist du?"
3. Der Assistent sollte antworten

### Test 2: Spracheingabe

1. Im Chat auf Mikrofon klicken
2. Etwas sagen (z.B. "Was kannst du alles?")
3. Warte auf Transkription und Antwort

### Test 3: Home Assistant

1. Gehe zu **Smart Home**
2. Du solltest deine Geräte sehen
3. Klicke auf ein Gerät zum Ein-/Ausschalten

## 🔧 Erweiterte Konfiguration

### GPU Support aktivieren (NVIDIA)

1. NVIDIA Docker installieren:
```bash
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list

sudo apt-get update
sudo apt-get install -y nvidia-docker2
sudo systemctl restart docker
```

2. Docker Compose anpassen (bereits konfiguriert in `docker-compose.yml`)

### Nginx Reverse Proxy (HTTPS)

1. SSL-Zertifikat erstellen (Let's Encrypt):
```bash
sudo apt install certbot
sudo certbot certonly --standalone -d deine-domain.de
```

2. Nginx Konfiguration anpassen:
```bash
nano config/nginx.conf
```

3. Nginx Profil aktivieren:
```bash
docker-compose --profile production up -d nginx
```

### Firewall konfigurieren

```bash
# UFW installieren und konfigurieren
sudo apt install ufw

# Ports öffnen
sudo ufw allow 22/tcp   # SSH
sudo ufw allow 80/tcp   # HTTP
sudo ufw allow 443/tcp  # HTTPS
sudo ufw allow 3000/tcp # Frontend (nur im lokalen Netz)
sudo ufw allow 8000/tcp # Backend (nur im lokalen Netz)

# Firewall aktivieren
sudo ufw enable
```

## 🔍 Troubleshooting

### Container startet nicht

```bash
# Logs prüfen
docker-compose logs renfield-backend
docker-compose logs renfield-ollama

# Container neu starten
docker-compose restart
```

### Ollama Modell lädt nicht

```bash
# Manuell laden
docker exec -it renfield-ollama ollama pull llama3.2:3b

# Status prüfen
docker exec -it renfield-ollama ollama list
```

### Whisper Fehler

```bash
# Cache leeren
docker exec -it renfield-backend rm -rf /root/.cache/whisper

# Container neu bauen
docker-compose build backend
docker-compose up -d backend
```

### Datenbank Probleme

```bash
# Datenbank neu initialisieren
docker-compose down
docker volume rm renfield_postgres_data
docker-compose up -d
```

### Speicherplatz freigeben

```bash
# Ungenutzte Docker Images entfernen
docker system prune -a

# Logs limitieren in docker-compose.yml:
# logging:
#   driver: "json-file"
#   options:
#     max-size: "10m"
#     max-file: "3"
```

## 📊 Performance-Optimierung

### Ollama Modell-Wahl

**Kleine Geräte (16GB RAM):**
- `llama3.2:3b` - Schnell, ausreichend für einfache Aufgaben

**Mittlere Server (32GB RAM):**
- `llama3.1:8b` - Gute Balance

**Starke Server (64GB+ RAM):**
- `llama3.1:70b` - Beste Qualität (benötigt GPU)

### Whisper Modell-Wahl

- `tiny` - Sehr schnell, geringere Genauigkeit
- `base` - Standard, gute Balance (empfohlen)
- `small` - Besser, langsamer
- `medium` - Sehr gut, braucht mehr RAM
- `large` - Beste Qualität, sehr langsam

## 🔄 Updates

```bash
# Repository aktualisieren
git pull

# Container neu bauen und starten
docker-compose down
docker-compose build
docker-compose up -d
```

## 🗑️ Deinstallation

```bash
# Container und Volumes löschen
docker-compose down -v

# Repository entfernen
cd ..
rm -rf renfield
```

## 💾 Backup

### Wichtige Daten sichern

```bash
# Datenbank
docker exec renfield-postgres pg_dump -U renfield renfield > backup_db.sql

# Uploads & Modelle
docker run --rm -v renfield_whisper_models:/data -v $(pwd):/backup alpine tar czf /backup/whisper_models_backup.tar.gz /data
docker run --rm -v renfield_ollama_data:/data -v $(pwd):/backup alpine tar czf /backup/ollama_backup.tar.gz /data
```

### Wiederherstellen

```bash
# Datenbank
cat backup_db.sql | docker exec -i renfield-postgres psql -U renfield renfield

# Modelle
docker run --rm -v renfield_whisper_models:/data -v $(pwd):/backup alpine sh -c "cd /data && tar xzf /backup/whisper_models_backup.tar.gz --strip 1"
```

## 📞 Support

Bei Problemen:
1. Prüfe die Logs: `docker-compose logs`
2. Suche in den Issues auf GitHub
3. Erstelle ein neues Issue mit:
   - Systeminformationen
   - Fehlermeldungen
   - Relevante Logs

---

**Viel Erfolg mit Renfield!** 🎉
