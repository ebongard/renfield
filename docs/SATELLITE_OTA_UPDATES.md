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

Die "neueste verfügbare Version" wird **aus der gebündelten Satellite-Quelle gelesen**
(`__version__` in `renfield_satellite/__init__.py`) — nicht aus einer separaten
Variable. Das Backend-Image bündelt die Satellite-Quelle nach `/app/satellite`
(Dockerfile `COPY satellite /app/satellite`; die Quelle wird beim Build aus
`src/satellite/` in den Build-Context gesynct — siehe `deploy-production` Skill),
und `SatelliteUpdateService.get_latest_version()` liest die Version daraus. Dadurch
kann die angebotene Version nie von dem tatsächlich ausgelieferten Paket abweichen.
Ein neues Satellite-Release = `__version__` im Satellite-Repo erhöhen, Backend neu
bauen — kein Env-Bump nötig.

```bash
# Optionaler Fallback NUR falls die Quelle nicht gebündelt ist (lokale Dev /
# abgespecktes Image). Wird ignoriert, sobald /app/satellite vorhanden ist.
SATELLITE_LATEST_VERSION=1.4.0
```

> **Wichtig:** Ohne die gebündelte Quelle liefert `GET /api/satellites/update-package`
> einen 503 (kein Paket baubar). Der Build bricht laut ab, wenn `src/satellite/`
> nicht in den Build-Context gesynct wurde (Dockerfile-Assertion).

### Satellite

Der Satellite benötigt passwortlosen sudo-Zugriff für den Service-Neustart:

```bash
# /etc/sudoers.d/renfield-satellite
# Replace <satellite_user> with the username that runs the satellite service
# (default: `pi` on Raspberry Pi OS).
<satellite_user> ALL=(ALL) NOPASSWD: /bin/systemctl restart renfield-satellite.service
<satellite_user> ALL=(ALL) NOPASSWD: /bin/systemctl stop renfield-satellite.service
<satellite_user> ALL=(ALL) NOPASSWD: /bin/systemctl start renfield-satellite.service
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
| `completed` | 100% | Update erfolgreich — **beendet den Lauf** |
| `failed` | - | Fehler aufgetreten — **beendet den Lauf** |
| `rolling_back` | - | Sicherung wird zurückgespielt — **beendet den Lauf** |

### Wann ein Lauf endet

`failed`, `rolling_back` und `completed` kommen als gewöhnliche
`update_progress`-Meldungen herein, **beenden aber einen Lauf**. `completed`
wird ausdrücklich VOR dem Neustart gemeldet, der das eigentliche
`update_complete` meist verschluckt — bliebe der Lauf deshalb auf
`in_progress`, würde die Zeitgrenze ein **erfolgreiches** Update eine
Viertelstunde später als gescheitert ausweisen. Das Backend setzt darauf `failed` und
übernimmt den Meldungstext als Fehlergrund. Bis #1209 galt jede
Fortschrittsmeldung als „läuft noch": ein zurückgerollter Lauf blieb dauerhaft
auf `in_progress`/`rolling_back` stehen, und weil der Fortschrittszweig keinen
Fehler mitgab, wurde ein bereits gesetzter Grund dabei auf `null` überschrieben
— ein hängendes Update ohne erkennbare Ursache.

Zwei Regeln sichern das ab:

1. **Ein beendeter Lauf wird nicht zurückgeholt.** Der Satellit plant seine
   Fortschrittsmeldungen abgesetzt ein, während er die Endmeldung abwartet —
   eine nachlaufende Meldung ist also der Normalfall, keine Ausnahme. Sie darf
   einen `completed`- oder `failed`-Lauf nicht wieder auf `in_progress` ziehen.
   Ein **neuer** Lauf kann jederzeit starten; die Sperre gilt nur für
   Fortschrittsmeldungen.
2. **Zeitgrenze als Auffangnetz.** Meldet ein Lauf innerhalb von
   `SATELLITE_UPDATE_TIMEOUT` (900 s) keinen Endzustand — etwa weil die
   Verbindung mitten im Install abriss —, beendet ihn der Kehraus als
   `failed`. Eine vom Satelliten bereits gelieferte Begründung bleibt dabei
   erhalten; nur wenn keine vorliegt, wird kenntlich gemacht, dass das Urteil
   vom Backend stammt.

### Was der Kehraus sonst noch tut

`cleanup_stale` trägt drei Zeitgrenzen, und bis #1209 lief keine davon, weil die
Funktion **keinen Aufrufer im Produktivcode** hatte. Sie zu takten schaltet alle
drei scharf, deshalb sind die beiden älteren dabei kalibriert worden:

* **Sicherungsnetz für ein hängendes Gerät** (`DEVICE_SESSION_TIMEOUT`, 120 s)
  gilt nur noch im Zustand `listening`. Es ist KEINE Aufnahmegrenze: die sitzt
  auf dem Satelliten (`vad_max_recording_seconds`, Flotte 60 s) und beendet die
  Aufnahme über `audio_end`, womit die Sitzung `listening` verlässt. Dieser Wert
  greift nur, wenn gar kein `audio_end` kommt, und muss deutlich über jeder
  Gerätegrenze liegen — sonst gewinnt er und verwirft die Aufnahme samt Puffer. Die Marke wird beim Weckwort gesetzt, und der ganze Zug —
  Spracherkennung, Agent, Modell, Sprachausgabe — läuft inline in derselben
  Empfangsschleife. Auf den ganzen Zug angewandt zerstörte die Frist die Sitzung
  mitten in der Antwort, und die fertige Antwort würde stumm verworfen.
* **Heartbeat-Räumung** (`DEVICE_HEARTBEAT_TIMEOUT`, 60 s) nimmt zwei Fälle aus:
  einen Satelliten mit laufender Sitzung (seine Lebenszeichen liegen ungelesen
  im Puffer) und einen mit laufendem OTA (der Installer blockiert die
  Ereignisschleife des Geräts bis zu 150 s). Geräumt wird zudem **mit
  Verbindungsschluss** — ohne ihn liefe die Empfangsschleife weiter und
  bestätigte weiter Heartbeats, das Gerät sähe eine gesunde Leitung, meldete
  sich nie neu an und bliebe dauerhaft stumm.

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
./bin/deploy-satellite.sh satellite-livingroom.local pi
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

## Background moved from CLAUDE.md (2026-09-20)

The hard rules for this code live in `.claude/rules/satellite-trust-ota.md`. This section keeps the
"why" and the rollout history of the two satellite-trust fixes from the security review (H1 + H6).
Design + the 4 resolved decisions: `docs/private/security/satellite-trust-design.md`.

### Signed OTA packages (security review H6 — full fix)

- The **surgical** H6 fix made the OTA download verify TLS. The **full** fix makes code authenticity
  independent of transport/backend trust, so a compromised or spoofed backend cannot push code it did
  not get signed offline.
- Model: a **signed source manifest**. It was chosen because the backend builds the OTA tarball
  *dynamically* — bytes built on demand cannot be pre-signed.
- The manifest is signed offline with an **Ed25519** release key (consistent with the federation
  Ed25519). `bin/sign_satellite_release.py` (`--gen-key` / `--sign` / `--verify`) runs on the operator
  workstation and writes `src/satellite/RELEASE_MANIFEST.json` + `.sig`; both are committed and baked
  into the backend image by the existing Dockerfile COPY.
- As part of the same change the backend tarball started excluding `__pycache__`.
- **Rollout status as recorded on 2026-08-22:** `RELEASE_MANIFEST.json` + `.sig` for v1.4.6 were
  committed (key #1 generated; private key at `~/.renfield/ota_release_key` on the operator workstation
  only) and the public key was added to group_vars `satellite_release_pubkeys`. Remaining at that time:
  the fleet re-provision (pins the pubkey on each satellite), then flipping `require_signature`
  (satellite side + backend `satellite_ota_require_signature`) to fail-closed.
- Until a satellite is re-provisioned it has no pinned key and still installs on checksum-only
  (legacy); a satellite that HAS the key verifies every signed release (verify-if-present).
- **Later state (from the repo, not from CLAUDE.md):**
  `src/satellite/provisioning/group_vars/satellites.yml` sets `satellite_ota_require_signature: true`
  with the comment "FAIL-CLOSED since 2026-08-23", and `k8s/configmap.yaml` sets
  `SATELLITE_OTA_REQUIRE_SIGNATURE: "true"`. Satellites that were offline at that rollout inherit
  fail-closed on their next re-provision.
- Why the version bump matters: the comment on `SAFE_PACKAGES` in `update_manager.py` records that the
  satellite version was not bumped between 2026-08-22 and 2026-09-04, so no update was ever attempted —
  which hid both a missing allowlist entry (`opuslib`; every OTA would have failed with "Unknown
  packages in requirements") and the stale `RELEASE_MANIFEST` signature. The allowlist living in the
  running code instead of in the package is tracked as #1210 (`docs/BACKLOG_INVENTORY.md`).

### Satellite enrollment credential (security review H1 — full fix)

- A satellite's trust *was* assertion-based: any LAN device could connect to `/ws/satellite`,
  **claim** any `satellite_id` in its register frame, evict the incumbent, and harvest the per-person
  IRK push (location-tracking keys).
- The full fix gives each satellite a **per-device enrollment PSK** (256-bit), stored server-side only
  as a bcrypt hash in the `satellites` table (migration `pc20260624`).
- **Dark by default in code** (`SATELLITE_ENROLLMENT_ENABLED=false`): the register path is
  byte-identical to legacy — no PSK check, eviction unchanged, IRK push on the legacy
  `SATELLITE_IRK_ALLOWLIST`. PERMISSIVE is the soak phase; in it IRKs already go only to
  verified-enrolled satellites.
- The auto-flip latch exists because of one concrete scenario: a satellite enrolled later through the
  UI but still offline must not silently re-open the fleet, so the latch never auto-clears.
- `/api/ws/token` previously minted a token to anyone; it now answers 401 to an unauthenticated
  caller when WS auth is on.
- The staged rollout and the break-glass procedure are in `docs/ENVIRONMENT_VARIABLES.md`. The
  committed `k8s/configmap.yaml` carries `SATELLITE_ENROLLMENT_ENABLED: "true"` and
  `SATELLITE_ENROLLMENT_AUTOFLIP_ENABLED: "true"`.
