# Externe Ollama-Instanz nutzen

Diese Anleitung zeigt, wie du eine externe Ollama-Instanz (z.B. auf einem separaten Server mit GPU) anstelle des Docker-Containers verwendest.

## Warum externe Ollama-Instanz?

- **Bessere GPU-Performance**: Nutze einen dedizierten Server mit leistungsstarker GPU
- **Shared Resource**: Mehrere Renfield-Instanzen können dieselbe Ollama-Instanz nutzen
- **Zentrale Modell-Verwaltung**: Modelle müssen nur an einem Ort verwaltet werden
- **Geringerer Ressourcen-Verbrauch**: Renfield Docker-Stack benötigt weniger RAM/CPU

## Voraussetzungen

1. **Ollama auf externem Host installiert** (z.B. `cuda.local`)
2. **Ollama läuft und ist erreichbar** über Port 11434
3. **Gewünschtes Modell ist installiert** (z.B. `qwen3:8b`)

## Schnellstart

### 1. Ollama-URL in .env konfigurieren

Bearbeite die `.env` Datei:

```bash
# Ollama LLM
OLLAMA_URL=http://cuda.local:11434
OLLAMA_MODEL=qwen3:8b
```

**Wichtig:** Ersetze `cuda.local` mit dem Hostnamen oder IP-Adresse deines Ollama-Servers.

### 2. Renfield ohne lokalen Ollama-Container starten

```bash
# Entwicklung auf Mac
docker compose -f docker-compose.dev.yml up -d

# Produktion mit GPU (Whisper)
docker compose -f docker-compose.prod.yml up -d

# Standard
docker compose up -d
```

Der Ollama-Container wird nun **nicht** gestartet, da er im `ollama`-Profil ist.

### 3. Verbindung testen

```bash
# Test von Host-System
curl http://cuda.local:11434/api/tags

# Test vom Backend-Container aus
docker exec renfield-backend curl http://cuda.local:11434/api/tags
```

Erwartete Antwort:
```json
{
  "models": [
    {
      "name": "qwen3:8b",
      "modified_at": "2024-01-15T10:30:00Z",
      ...
    }
  ]
}
```

### 4. Renfield verwenden

Das wars! Renfield nutzt jetzt automatisch deine externe Ollama-Instanz.

---

## Detaillierte Setup-Anleitung

### Externe Ollama-Instanz einrichten

Falls du noch keine Ollama-Instanz hast:

#### Option 1: Ollama auf Linux-Server (empfohlen für GPU)

```bash
# Ollama installieren
curl -fsSL https://ollama.com/install.sh | sh

# Modell herunterladen
ollama pull qwen3:8b

# Als Service starten
systemctl enable ollama
systemctl start ollama

# Status prüfen
systemctl status ollama

# Test
curl http://localhost:11434/api/tags
```

#### Option 2: Ollama mit Docker auf separatem Host

```bash
# Mit NVIDIA GPU
docker run -d \
  --name ollama \
  --gpus all \
  -p 11434:11434 \
  -v ollama_data:/root/.ollama \
  --restart unless-stopped \
  ollama/ollama:latest

# Ohne GPU (CPU-only)
docker run -d \
  --name ollama \
  -p 11434:11434 \
  -v ollama_data:/root/.ollama \
  --restart unless-stopped \
  ollama/ollama:latest

# Modell laden
docker exec -it ollama ollama pull qwen3:8b
```

### Firewall-Konfiguration

Stelle sicher, dass Port 11434 erreichbar ist:

```bash
# UFW (Ubuntu)
sudo ufw allow 11434/tcp

# firewalld (CentOS/RHEL)
sudo firewall-cmd --permanent --add-port=11434/tcp
sudo firewall-cmd --reload

# iptables
sudo iptables -A INPUT -p tcp --dport 11434 -j ACCEPT
```

### Netzwerk-Troubleshooting

Falls Verbindungsprobleme auftreten:

#### 1. Hostname-Auflösung testen

```bash
# Vom Host-System
ping cuda.local

# Vom Backend-Container
docker exec renfield-backend ping -c 4 cuda.local
```

Falls `cuda.local` nicht auflösbar ist:
- Option A: IP-Adresse statt Hostname verwenden: `OLLAMA_URL=http://192.168.1.100:11434`
- Option B: Eintrag in `/etc/hosts` hinzufügen (auf Host-System und ggf. im Container)

#### 2. Port-Erreichbarkeit testen

```bash
# Von Host-System
telnet cuda.local 11434
# oder
nc -zv cuda.local 11434

# Von Backend-Container
docker exec renfield-backend telnet cuda.local 11434
```

#### 3. Docker-Netzwerk prüfen

Falls der Container `cuda.local` nicht erreichen kann, aber das Host-System schon:

**Lösung A: Host-Netzwerk-Modus** (nicht empfohlen, aber einfach)
```yaml
# In docker-compose.yml beim backend-Service:
backend:
  network_mode: "host"
  # depends_on und networks entfernen
```

**Lösung B: Extra-Hosts** (empfohlen)
```yaml
# In docker-compose.yml beim backend-Service:
backend:
  extra_hosts:
    - "cuda.local:192.168.1.100"  # IP deines Ollama-Servers
```

**Lösung C: Docker-Bridge mit Host-Gateway**
```yaml
backend:
  extra_hosts:
    - "cuda.local:host-gateway"
```

---

## Erweiterte Konfiguration

### Mehrere Ollama-Instanzen (Load Balancing)

Falls du mehrere Ollama-Server hast, kannst du einen Load Balancer (z.B. nginx) davor schalten:

```nginx
# nginx.conf
upstream ollama_backend {
    server cuda1.local:11434;
    server cuda2.local:11434;
    least_conn;
}

server {
    listen 11434;
    location / {
        proxy_pass http://ollama_backend;
    }
}
```

Dann in `.env`:
```
OLLAMA_URL=http://loadbalancer.local:11434
```

### Ollama mit Authentifizierung

Falls deine Ollama-Instanz hinter einem Auth-Proxy liegt:

```python
# backend/services/ollama_service.py anpassen:
def __init__(self):
    self.client = ollama.AsyncClient(
        host=settings.ollama_url,
        headers={
            "Authorization": "Bearer YOUR_TOKEN"
        }
    )
```

### Performance-Optimierung

#### Ollama-Server Tuning

```bash
# Ollama Umgebungsvariablen setzen
export OLLAMA_NUM_PARALLEL=4        # Parallele Anfragen
export OLLAMA_MAX_LOADED_MODELS=2   # Gleichzeitig geladene Modelle
export OLLAMA_KEEP_ALIVE=24h        # Modell im RAM halten

# Service neu starten
systemctl restart ollama
```

#### Renfield Backend Tuning

```env
# .env
OLLAMA_MODEL=qwen3:8b  # Kleineres Modell für schnellere Antworten
# oder
OLLAMA_MODEL=llama3.2:1b  # Noch kleiner, aber weniger akkurat
```

---

## Monitoring & Debugging

### Ollama-Logs ansehen

```bash
# Native Installation
journalctl -u ollama -f

# Docker-Container
docker logs -f ollama
```

### Renfield Backend Logs

```bash
docker logs -f renfield-backend | grep ollama
```

### Ollama API direkt testen

```bash
# Modelle auflisten
curl http://cuda.local:11434/api/tags

# Chat-Anfrage senden
curl http://cuda.local:11434/api/chat -d '{
  "model": "qwen3:8b",
  "messages": [
    {"role": "user", "content": "Hallo"}
  ]
}'

# Modell-Info abrufen
curl http://cuda.local:11434/api/show -d '{
  "name": "qwen3:8b"
}'
```

### Häufige Fehler

#### Fehler: "Connection refused"
```bash
# Prüfen ob Ollama läuft
systemctl status ollama
# oder
docker ps | grep ollama

# Prüfen ob Port offen ist
sudo netstat -tlnp | grep 11434
```

#### Fehler: "Model not found"
```bash
# Modell herunterladen
ollama pull qwen3:8b

# Verfügbare Modelle prüfen
ollama list
```

#### Fehler: "Timeout"
```bash
# Timeout in Backend erhöhen (backend/services/ollama_service.py)
self.client = ollama.AsyncClient(
    host=settings.ollama_url,
    timeout=300  # 5 Minuten
)
```

---

## Zurück zu lokalem Ollama-Container

Falls du wieder den lokalen Container nutzen möchtest:

### 1. .env anpassen

```env
OLLAMA_URL=http://ollama:11434
```

### 2. Mit Ollama-Profil starten

```bash
docker compose --profile ollama up -d
```

### 3. Modell herunterladen

```bash
docker exec -it renfield-ollama ollama pull qwen3:8b
```

---

## Best Practices

### Security

1. **Firewall**: Erlaube nur vertrauenswürdige IPs auf Port 11434
2. **VPN**: Nutze VPN für Zugriff über Internet
3. **Reverse Proxy**: Setze nginx mit TLS davor
4. **No Public Exposure**: Ollama sollte NICHT öffentlich erreichbar sein

### Performance

1. **GPU verwenden**: Ollama ist mit GPU deutlich schneller
2. **Ausreichend RAM**: Mindestens 8GB für qwen3:8b
3. **SSD**: Schnellere Modell-Ladezeiten
4. **Proximity**: Ollama-Server sollte im gleichen Netzwerk sein (geringe Latenz)

### Maintenance

1. **Regelmäßige Updates**: `ollama pull` für Modell-Updates
2. **Monitoring**: Überwache CPU/GPU/RAM-Nutzung
3. **Backups**: Sichere Ollama-Daten: `/root/.ollama` oder `ollama_data` Volume
4. **Logs rotieren**: Verhindere zu große Log-Dateien

---

## Alternative: Ollama-Container im gleichen Docker-Netzwerk

Falls du Ollama in einem separaten Docker-Stack laufen lassen möchtest, aber im gleichen Netzwerk:

```yaml
# separate docker-compose.ollama.yml
services:
  ollama:
    image: ollama/ollama:latest
    container_name: shared-ollama
    volumes:
      - ollama_data:/root/.ollama
    networks:
      - shared-network
    ports:
      - "11434:11434"
    restart: unless-stopped

networks:
  shared-network:
    name: shared-network
    driver: bridge

volumes:
  ollama_data:
```

```bash
# Ollama-Stack starten
docker compose -f docker-compose.ollama.yml up -d

# Renfield an shared-network anhängen
# In renfield docker-compose.yml:
networks:
  renfield-network:
    external: true
    name: shared-network
```

---

## Support

Bei Problemen:

1. Prüfe die Logs (siehe Monitoring-Sektion)
2. Teste die Verbindung (siehe Troubleshooting)
3. Erstelle ein Issue auf GitHub: https://github.com/yourusername/renfield/issues
4. Discord/Community-Forum nutzen

---

## Weitere Ressourcen

- [Ollama Dokumentation](https://github.com/ollama/ollama/blob/main/docs/README.md)
- [Ollama API Reference](https://github.com/ollama/ollama/blob/main/docs/api.md)
- [Verfügbare Modelle](https://ollama.com/library)
- [GPU-Setup für Ollama](https://github.com/ollama/ollama/blob/main/docs/gpu.md)
