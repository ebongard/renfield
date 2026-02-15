# Plan: SIP-Integration Phase 1 — Eingehende Anrufe erkennen + benachrichtigen

## Context

Renfield hat aktuell keine Telefonie-Anbindung. Der Nutzer hat kein bestehendes VoIP-Setup und keine Fritzbox. Phase 1 liefert: Eingehende Anrufe werden erkannt, der Anrufer wird identifiziert (CallerID + Telefonbuch), und der Nutzer wird per Toast + TTS benachrichtigt.

**Kernidee:** Asterisk als SIP-PBX im Docker-Container, ein leichtgewichtiger Python-Sidecar als Call Monitor, und die **bestehende Proactive Notification Pipeline** (Webhook → WebSocket → Toast + TTS) — kein Renfield-Backend-Code nötig.

## Architektur

```
SIP Provider (sipgate/easybell)
  ↓ SIP INVITE (UDP 5060)
Asterisk (Docker, minimale Config)
  ↓ AMI Event: Newchannel (CallerID)
sip-monitor (Python Sidecar Container)
  ↓ Telefonbuch-Lookup → POST /api/notifications/webhook
Renfield Backend (bestehend, zero changes)
  ↓ WebSocket broadcast + Piper TTS
User: Toast "Anruf von Max Mustermann" + TTS-Ansage
```

## Implementierung

### Schritt 1: Asterisk Docker Container

**Image:** `andrius/asterisk:alpine-20` (~50MB, leichtestes Asterisk-Image)

**3 Config-Dateien** (gemountet als Volumes, mit envsubst-Template-Rendering):

**`config/asterisk/pjsip.conf`** — SIP-Trunk zum Provider:
```ini
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0

[sip-provider]
type=registration
outbound_auth=sip-provider-auth
server_uri=sip:${SIP_REGISTRAR}
client_uri=sip:${SIP_USERNAME}@${SIP_REGISTRAR}
retry_interval=60

[sip-provider-auth]
type=auth
auth_type=userpass
username=${SIP_USERNAME}
password=${SIP_PASSWORD}

[sip-provider-endpoint]
type=endpoint
context=incoming-calls
disallow=all
allow=alaw
allow=ulaw
from_user=${SIP_USERNAME}
trust_id_inbound=yes

[sip-provider-aor]
type=aor
contact=sip:${SIP_REGISTRAR}

[sip-provider-identify]
type=identify
endpoint=sip-provider-endpoint
match=${SIP_REGISTRAR}
```

**`config/asterisk/extensions.conf`** — Minimaler Dialplan (erkennen + klingeln lassen + auflegen):
```ini
[incoming-calls]
exten => _X.,1,NoOp(Incoming call from ${CALLERID(num)})
 same => n,Wait(30)
 same => n,Hangup(21)

exten => _+X.,1,Goto(incoming-calls,${EXTEN:1},1)
exten => s,1,Goto(incoming-calls,s,1)
```
> `Wait(30)` lässt den Anruf 30 Sek. klingeln (Anrufer hört Freizeichen), dann wird abgelehnt. Phase 2 ersetzt dies durch `Answer()` + tatsächliche Anrufbehandlung.

**`config/asterisk/manager.conf`** — AMI-Zugang (read-only):
```ini
[general]
enabled=yes
port=5038
bindaddr=0.0.0.0

[callmonitor]
secret=${AMI_SECRET}
read=call
write=
```

**`config/asterisk/entrypoint.sh`** — Template-Rendering + Asterisk-Start:
```bash
#!/bin/sh
for f in /etc/asterisk/templates/*.conf; do
    filename=$(basename "$f")
    envsubst < "$f" > "/etc/asterisk/$filename"
done
exec asterisk -f
```

### Schritt 2: Call Monitor (Python Sidecar)

Leichtgewichtiger Python-Service (~120 Zeilen), verbindet sich über TCP zum Asterisk AMI, erkennt eingehende Anrufe, und POSTet zum Renfield Notification Webhook.

**`src/sip-monitor/monitor.py`** — Kernlogik:
- Verbindung zu Asterisk AMI via [panoramisk](https://github.com/gawel/panoramisk) (asyncio)
- Lauscht auf `Newchannel` + `NewCallerid` Events
- Extrahiert CallerID-Nummer
- Telefonbuch-Lookup (YAML-Datei)
- Dedup-Window (30 Sek., verhindert Doppel-Benachrichtigungen)
- POST an `http://backend:8000/api/notifications/webhook`

**`src/sip-monitor/Dockerfile`:**
```dockerfile
FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py .
CMD ["python", "monitor.py"]
```

**`src/sip-monitor/requirements.txt`:** `panoramisk>=1.4`, `httpx>=0.27`, `pyyaml>=6.0`

**Webhook-Payload:**
```json
{
  "event_type": "sip.incoming_call",
  "title": "Anruf von Max Mustermann",
  "message": "Eingehender Anruf von Max Mustermann",
  "urgency": "info",
  "room": null,
  "tts": true,
  "data": {
    "caller_number": "+49 170 1234567",
    "caller_name": "Max Mustermann"
  }
}
```

### Schritt 3: Telefonbuch

**`config/sip/phonebook.yaml`:**
```yaml
contacts:
  "+49 170 1234567": "Max Mustermann"
  "+49 171 9876543": "Emma Mustermann"
  "0170 5555555": "Oma Helga"
```

Nummern werden vor dem Vergleich normalisiert (Leerzeichen, +, 00-Prefix entfernt). Unbekannte Nummern werden als Rufnummer angezeigt.

### Schritt 4: Docker Compose

Beide Services unter `sip` Profile (opt-in):

```yaml
  asterisk:
    image: andrius/asterisk:alpine-20
    container_name: renfield-asterisk
    environment:
      SIP_REGISTRAR: ${SIP_REGISTRAR:-sipconnect.sipgate.de}
      SIP_USERNAME: ${SIP_USERNAME:-}
      SIP_PASSWORD: ${SIP_PASSWORD:-}
      AMI_SECRET: ${AMI_SECRET:-changeme}
    volumes:
      - ./config/asterisk/pjsip.conf:/etc/asterisk/templates/pjsip.conf:ro
      - ./config/asterisk/extensions.conf:/etc/asterisk/templates/extensions.conf:ro
      - ./config/asterisk/manager.conf:/etc/asterisk/templates/manager.conf:ro
      - ./config/asterisk/entrypoint.sh:/entrypoint.sh:ro
    entrypoint: ["/bin/sh", "/entrypoint.sh"]
    ports:
      - "5060:5060/udp"
      - "5060:5060/tcp"
    networks:
      - renfield-network
    restart: unless-stopped
    profiles: [sip]

  sip-monitor:
    build: ./src/sip-monitor
    container_name: renfield-sip-monitor
    environment:
      AMI_HOST: asterisk
      AMI_PORT: 5038
      AMI_USERNAME: callmonitor
      AMI_SECRET: ${AMI_SECRET:-changeme}
      RENFIELD_WEBHOOK_URL: http://backend:8000/api/notifications/webhook
      RENFIELD_WEBHOOK_TOKEN: ${SIP_WEBHOOK_TOKEN:-}
      PHONEBOOK_PATH: /config/phonebook.yaml
    volumes:
      - ./config/sip/phonebook.yaml:/config/phonebook.yaml:ro
    depends_on:
      asterisk: { condition: service_healthy }
    networks:
      - renfield-network
    restart: unless-stopped
    profiles: [sip]
```

### Schritt 5: Environment Variables

**`.env.example`:**
```bash
# === SIP / Telephony (docker compose --profile sip up) ===
# SIP_REGISTRAR=sipconnect.sipgate.de
# SIP_USERNAME=your_sip_id
# SIP_PASSWORD=your_sip_password
# AMI_SECRET=changeme-ami-secret
# SIP_WEBHOOK_TOKEN=your_notification_webhook_token
```

### Schritt 6: Dokumentation

- `docs/SIP_INTEGRATION.md` — Setup-Guide (Provider-Registrierung, Port-Forwarding, Telefonbuch)
- `docs/ENVIRONMENT_VARIABLES.md` — SIP-Sektion
- `CLAUDE.md` — SIP in Features-Liste

## Dateien

### Neue Dateien
| Datei | Beschreibung |
|-------|-------------|
| `config/asterisk/pjsip.conf` | PJSIP Trunk + Provider-Registrierung |
| `config/asterisk/extensions.conf` | Dialplan (detect + wait + hangup) |
| `config/asterisk/manager.conf` | AMI-Zugang für Call Monitor |
| `config/asterisk/entrypoint.sh` | Template-Rendering + Start |
| `config/sip/phonebook.yaml` | Telefonnummer → Name Mapping |
| `src/sip-monitor/monitor.py` | Call Monitor (~120 Zeilen) |
| `src/sip-monitor/Dockerfile` | Python Alpine Image |
| `src/sip-monitor/requirements.txt` | panoramisk, httpx, pyyaml |
| `docs/SIP_INTEGRATION.md` | Setup-Guide |

### Geänderte Dateien
| Datei | Änderung |
|-------|---------|
| `docker-compose.yml` | asterisk + sip-monitor (profile: sip) |
| `.env.example` | SIP env vars |
| `docs/ENVIRONMENT_VARIABLES.md` | SIP-Sektion |
| `CLAUDE.md` | SIP erwähnen |

### Nicht geändert (Zero Changes)
| Datei | Grund |
|-------|-------|
| `src/backend/**` | Notification Webhook funktioniert as-is |
| `src/frontend/**` | Toast + TTS Rendering funktioniert as-is |
| `config/mcp_servers.yaml` | Telefonie ist kein MCP-Tool (Phase 1) |

## Voraussetzungen (manuell)

1. **SIP-Provider-Account** (z.B. sipgate basic — kostenlos, deutsche Rufnummer)
2. **Port-Forwarding** im Router: UDP 5060 → Docker Host
3. **Notification Webhook Token** generieren: `POST /api/notifications/token`

## Verifizierung

1. `docker compose --profile sip up -d` — Asterisk + sip-monitor starten
2. `docker exec renfield-asterisk asterisk -rx 'pjsip show registrations'` — SIP-Registrierung OK
3. `docker logs renfield-sip-monitor` — "Connected to Asterisk AMI"
4. Testanruf von Mobiltelefon → Toast + TTS "Eingehender Anruf von ..."
5. `GET /api/notifications` — `event_type: "sip.incoming_call"` in DB

## Phase 2+ Ausblick

| Phase | Erweiterung | Änderung |
|-------|------------|---------|
| Phase 2 | Anrufe beantworten + Ansage | `extensions.conf` (Answer + Playback) |
| Phase 2 | Voicemail | `voicemail.conf` + Mailbox |
| Phase 3 | "Ruf Max an" | Neuer MCP Server + mcp_servers.yaml |
| Phase 3 | Anrufhistorie | DB-Tabelle + REST API |
| Phase 4 | IVR-Menü | extensions.conf + Sound-Dateien |
