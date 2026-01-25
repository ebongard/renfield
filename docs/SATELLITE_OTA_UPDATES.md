# Satellite OTA Update System

Over-the-Air (OTA) Updates für Renfield Satellites ermöglichen die Aktualisierung der Satellite-Software direkt aus dem Web-UI.

## Features

- **Versions-Tracking**: Jeder Satellite meldet seine Version bei der Registrierung
- **Update-Erkennung**: Automatische Erkennung, wenn neuere Versionen verfügbar sind
- **Web-UI Integration**: Update-Auslösung per Klick auf der Satelliten-Seite
- **Fortschrittsanzeige**: Echtzeit-Fortschritt während des Updates
- **Automatisches Rollback**: Bei Fehlern wird das Backup wiederhergestellt

## Architektur

```
┌─────────────────────────────────────────────────────────────┐
│                     Admin-Frontend                          │
│                   (SatellitesPage.jsx)                      │
│  - Version anzeigen pro Satellite                           │
│  - "Update verfügbar" Badge                                 │
│  - Update-Button + Fortschrittsanzeige                      │
└─────────────────────┬───────────────────────────────────────┘
                      │ POST /api/satellites/{id}/update
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                     Backend API                             │
│  - GET /api/satellites/versions                             │
│  - POST /api/satellites/{id}/update                         │
│  - GET /api/satellites/{id}/update-status                   │
│  - GET /api/satellites/update-package                       │
└─────────────────────┬───────────────────────────────────────┘
                      │ WebSocket: update_request
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   Satellite (Pi)                            │
│  - UpdateManager: Download, Backup, Install, Rollback       │
│  - Sendet update_progress während Update                    │
│  - Neustart via systemctl                                   │
└─────────────────────────────────────────────────────────────┘
```

## Konfiguration

### Backend

In `.env` die neueste verfügbare Version setzen:

```bash
# Satellite OTA Updates
SATELLITE_LATEST_VERSION=1.1.0
```

### Satellite

Der Satellite benötigt passwortlosen sudo-Zugriff für den Service-Neustart:

```bash
# /etc/sudoers.d/renfield-satellite
evdb ALL=(ALL) NOPASSWD: /bin/systemctl restart renfield-satellite.service
evdb ALL=(ALL) NOPASSWD: /bin/systemctl stop renfield-satellite.service
evdb ALL=(ALL) NOPASSWD: /bin/systemctl start renfield-satellite.service
```

## API Endpoints

| Endpoint | Methode | Beschreibung |
|----------|---------|--------------|
| `/api/satellites/versions` | GET | Alle Versionen abrufen |
| `/api/satellites/{id}/update` | POST | Update auslösen |
| `/api/satellites/{id}/update-status` | GET | Update-Status abfragen |
| `/api/satellites/update-package` | GET | Update-Paket herunterladen |

### Beispiel: Versionen abrufen

```bash
curl http://localhost:8000/api/satellites/versions
```

```json
{
  "latest_version": "1.1.0",
  "satellites": [
    {
      "satellite_id": "sat-wohnzimmer",
      "version": "1.0.0",
      "update_available": true,
      "update_status": "none"
    }
  ]
}
```

### Beispiel: Update auslösen

```bash
curl -X POST http://localhost:8000/api/satellites/sat-wohnzimmer/update
```

```json
{
  "success": true,
  "message": "Update to v1.1.0 initiated",
  "target_version": "1.1.0"
}
```

## Update-Ablauf

```
┌─────────────────┐
│ update_request  │
└────────┬────────┘
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Downloading   │────►│   Verifying     │
│    (0-40%)      │     │   (40-45%)      │
└─────────────────┘     └────────┬────────┘
                                 ▼
┌─────────────────┐     ┌─────────────────┐
│   Backing up    │◄────│   Extracting    │
│   (45-55%)      │     │   (55-70%)      │
└────────┬────────┘     └─────────────────┘
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Installing    │────►│   Restarting    │
│   (70-90%)      │     │   (90-100%)     │
└────────┬────────┘     └────────┬────────┘
         │                       │
         ▼ Bei Fehler            ▼ Erfolg
┌─────────────────┐     ┌─────────────────┐
│    Rollback     │     │ update_complete │
│ backup → install│     │  new_version    │
└─────────────────┘     └─────────────────┘
```

### Update-Stages

| Stage | Fortschritt | Beschreibung |
|-------|-------------|--------------|
| `downloading` | 0-40% | Paket vom Server herunterladen |
| `verifying` | 40-45% | SHA256 Checksum prüfen |
| `backing_up` | 45-55% | Aktuelle Installation sichern |
| `extracting` | 55-70% | Paket entpacken |
| `installing` | 70-90% | Neue Version installieren |
| `restarting` | 90-100% | Service neu starten |
| `completed` | 100% | Update erfolgreich |
| `failed` | - | Fehler aufgetreten, Rollback |

## WebSocket-Protokoll

### Server → Satellite: Update-Anfrage

```json
{
  "type": "update_request",
  "target_version": "1.1.0",
  "package_url": "/api/satellites/update-package",
  "checksum": "sha256:abc123...",
  "size_bytes": 108544
}
```

### Satellite → Server: Fortschritt

```json
{
  "type": "update_progress",
  "stage": "downloading",
  "progress": 45,
  "message": "Downloading... (48KB / 106KB)"
}
```

### Satellite → Server: Abgeschlossen

```json
{
  "type": "update_complete",
  "success": true,
  "old_version": "1.0.0",
  "new_version": "1.1.0"
}
```

### Satellite → Server: Fehlgeschlagen

```json
{
  "type": "update_failed",
  "stage": "installing",
  "error": "Permission denied",
  "rolled_back": true
}
```

## Manuelles Deployment

Für Entwicklung oder schnelle Updates ohne OTA:

```bash
# Satellite-Code deployen
./bin/deploy-satellite.sh [hostname] [user]

# Beispiel
./bin/deploy-satellite.sh satellite-wohnzimmer.local evdb
```

## Fehlerbehebung

### Update startet nicht

1. Prüfen, ob Satellite verbunden ist:
   ```bash
   curl http://localhost:8000/api/satellites
   ```

2. Backend-Logs prüfen:
   ```bash
   docker compose logs backend | grep -i update
   ```

### Update schlägt fehl

1. Satellite-Logs prüfen:
   ```bash
   ssh user@satellite.local "sudo journalctl -u renfield-satellite -n 50"
   ```

2. Häufige Probleme:
   - **Permission denied**: Sudoers-Konfiguration prüfen
   - **Checksum mismatch**: Netzwerkproblem, erneut versuchen
   - **Backup failed**: Speicherplatz prüfen

### Manueller Rollback

Falls ein Rollback nicht automatisch erfolgt:

```bash
ssh user@satellite.local
cd /opt/renfield-satellite
sudo systemctl stop renfield-satellite
rm -rf renfield_satellite
cp -r .backup/renfield_satellite .
sudo systemctl start renfield-satellite
```

## Sicherheit

- **Checksum-Verifikation**: SHA256 vor Installation
- **Automatisches Backup**: Vor jeder Installation
- **Automatischer Rollback**: Bei jedem Fehler nach Backup
- **Keine Root-Installation**: Update läuft als normaler User

## Dateien

| Datei | Beschreibung |
|-------|--------------|
| `src/backend/services/satellite_update_service.py` | Backend Update-Service |
| `src/satellite/renfield_satellite/update/update_manager.py` | Satellite Update-Manager |
| `src/frontend/src/pages/SatellitesPage.jsx` | Frontend Update-UI |
| `bin/deploy-satellite.sh` | Manuelles Deployment-Script |
