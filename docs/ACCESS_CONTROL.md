# Access Control System (RPBAC)

Renfield implementiert ein **Role-Permission Based Access Control (RPBAC)** System zum Schutz von Ressourcen.

## Inhaltsverzeichnis

- [Übersicht](#übersicht)
- [Aktivierung](#aktivierung)
- [Berechtigungen](#berechtigungen)
- [Rollen](#rollen)
- [Benutzer](#benutzer)
- [Resource Ownership](#resource-ownership)
- [Knowledge Base Sharing](#knowledge-base-sharing)
- [Voice Authentication](#voice-authentication)
- [API Referenz](#api-referenz)
- [Beispiel-Szenarien](#beispiel-szenarien)

---

## Übersicht

Das RPBAC-System bietet:

- **JWT-basierte Authentifizierung** mit Access- und Refresh-Tokens
- **Flexible Rollen** mit frei konfigurierbaren Berechtigungen
- **Granulare Permissions** für verschiedene Ressourcen-Typen
- **Resource Ownership** für Wissensdatenbanken und Konversationen
- **KB-Level Sharing** mit expliziten Berechtigungen pro Benutzer
- **Voice Authentication** per Sprechererkennung (optional)
- **Optional by default** - Standardmäßig deaktiviert für einfache Entwicklung

### Design-Philosophie

Das System ist für **vertrauenswürdige, offline Umgebungen** konzipiert:
- Authentifizierung ist optional und standardmäßig deaktiviert
- Wenn aktiviert, schützt es alle sensiblen Ressourcen
- Rollen sind flexibel und können an Haushaltsbedürfnisse angepasst werden

---

## Aktivierung

### Minimale Konfiguration

```bash
# .env
AUTH_ENABLED=true
SECRET_KEY=dein-starker-64-zeichen-key
```

### Vollständige Konfiguration

```bash
# .env

# === Authentifizierung ===
AUTH_ENABLED=true
SECRET_KEY=generiere-mit-python3-c-import-secrets-print-secrets.token_urlsafe-64

# JWT Token Gültigkeitsdauer
ACCESS_TOKEN_EXPIRE_MINUTES=1440    # 24 Stunden
REFRESH_TOKEN_EXPIRE_DAYS=30

# Passwort-Policy
PASSWORD_MIN_LENGTH=8

# Registrierung (false = nur Admin kann Benutzer erstellen)
ALLOW_REGISTRATION=false

# Standard-Admin (nur beim ersten Start)
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=sofort-aendern!

# === Voice Authentication ===
VOICE_AUTH_ENABLED=true
VOICE_AUTH_MIN_CONFIDENCE=0.7
```

### Erster Start

Beim ersten Start mit `AUTH_ENABLED=true`:

1. Standard-Rollen werden automatisch erstellt (Admin, Familie, Gast)
2. Ein Admin-Benutzer wird erstellt mit den konfigurierten Zugangsdaten
3. **Wichtig:** Passwort sofort nach erstem Login ändern!

```
⚠️  Standard-Admin erstellt: 'admin' - BITTE PASSWORT SOFORT ÄNDERN!
```

---

## Berechtigungen

### Permission-Typen

| Bereich | Berechtigungen | Beschreibung |
|---------|----------------|--------------|
| **Knowledge Bases** | `kb.none`, `kb.own`, `kb.shared`, `kb.all` | Wissensdatenbank-Zugriff |
| **Home Assistant** | `ha.none`, `ha.read`, `ha.control`, `ha.full` | Smart Home Steuerung |
| **Kameras** | `cam.none`, `cam.view`, `cam.full` | Frigate Kamera-Zugriff |
| **Konversationen** | `chat.own`, `chat.all` | Chat-Historie |
| **Räume** | `rooms.read`, `rooms.manage` | Raum-Konfiguration |
| **Sprecher** | `speakers.own`, `speakers.all` | Sprecherprofile |
| **Tasks** | `tasks.view`, `tasks.manage` | Task-Queue |
| **RAG** | `rag.use`, `rag.manage` | RAG-Nutzung |
| **Benutzer** | `users.view`, `users.manage` | Benutzerverwaltung |
| **Rollen** | `roles.view`, `roles.manage` | Rollenverwaltung |
| **Einstellungen** | `settings.view`, `settings.manage` | System-Einstellungen |
| **Benachrichtigungen** | `notifications.view`, `notifications.manage` | Proaktive Benachrichtigungen |
| **Plugins** | `plugins.none`, `plugins.use`, `plugins.manage` | Plugin-Zugriff |
| **Admin** | `admin` | Admin-Endpoints |

### Permission-Hierarchie

Höhere Berechtigungen implizieren niedrigere:

```
Knowledge Bases:
  kb.all → kb.shared → kb.own → kb.none

Home Assistant:
  ha.full → ha.control → ha.read → ha.none

Kameras:
  cam.full → cam.view → cam.none

Konversationen:
  chat.all → chat.own

Räume:
  rooms.manage → rooms.read

Sprecher:
  speakers.all → speakers.own

Tasks:
  tasks.manage → tasks.view

RAG:
  rag.manage → rag.use

Benutzer:
  users.manage → users.view

Rollen:
  roles.manage → roles.view

Einstellungen:
  settings.manage → settings.view
```

**Beispiel:** Ein Benutzer mit `ha.full` hat automatisch auch `ha.control` und `ha.read`.

### Geschützte Endpoints

| Endpoint | Benötigte Berechtigung |
|----------|------------------------|
| `/admin/*` | `admin` |
| `/debug/*` | `admin` |
| `/api/homeassistant/states` | `ha.read` |
| `/api/homeassistant/turn_*` | `ha.control` |
| `/api/homeassistant/service` | `ha.full` |
| `/api/camera/events` | `cam.view` |
| `/api/camera/snapshot` | `cam.full` |
| `/api/knowledge/bases` | `kb.own` + Ownership |
| `/api/knowledge/upload` | `rag.manage` oder KB-Schreibzugriff |
| `/api/roles/*` (GET) | `roles.view` |
| `/api/roles/*` (POST/PATCH/DELETE) | `roles.manage` |
| `/api/users/*` (GET) | `users.view` |
| `/api/users/*` (POST/PATCH/DELETE) | `users.manage` |

---

## Rollen

### Standard-Rollen

| Rolle | Beschreibung | Berechtigungen |
|-------|--------------|----------------|
| **Admin** | Vollzugriff | Alle Berechtigungen |
| **Familie** | Familienmitglieder | `kb.shared`, `ha.full`, `cam.view`, `chat.own`, `rooms.read`, `speakers.own`, `tasks.view`, `rag.use`, `plugins.use`, `notifications.view` |
| **Gast** | Eingeschränkter Zugriff | `kb.none`, `ha.read`, `cam.none`, `chat.own`, `rooms.read`, `plugins.none` |

### System-Rollen

Standard-Rollen sind als **System-Rollen** markiert:
- Können nicht gelöscht werden
- Name kann nicht geändert werden
- Berechtigungen können angepasst werden

### Eigene Rollen erstellen

```bash
# Via API
curl -X POST http://localhost:8000/api/roles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Techniker",
    "description": "Voller Smart Home Zugriff, keine Dokumente",
    "permissions": ["ha.full", "rooms.read", "chat.own"]
  }'
```

### Rollen-API

| Endpoint | Methode | Beschreibung |
|----------|---------|--------------|
| `/api/roles` | GET | Alle Rollen auflisten |
| `/api/roles` | POST | Neue Rolle erstellen |
| `/api/roles/{id}` | GET | Rolle abrufen |
| `/api/roles/{id}` | PATCH | Rolle bearbeiten |
| `/api/roles/{id}` | DELETE | Rolle löschen (nicht System-Rollen) |
| `/api/roles/permissions/all` | GET | Alle verfügbaren Permissions |

---

## Benutzer

### Benutzer erstellen

```bash
# Via API (benötigt Admin-Rechte)
curl -X POST http://localhost:8000/api/users \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "max",
    "password": "sicheres-passwort",
    "email": "max@example.com",
    "role_id": 2
  }'
```

### Selbst-Registrierung

Wenn `ALLOW_REGISTRATION=true`:

```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "neuer_benutzer",
    "password": "sicheres-passwort",
    "email": "user@example.com"
  }'
```

Neue Benutzer erhalten automatisch die "Gast"-Rolle.

### Benutzer-API

| Endpoint | Methode | Beschreibung |
|----------|---------|--------------|
| `/api/users` | GET | Alle Benutzer auflisten |
| `/api/users` | POST | Benutzer erstellen |
| `/api/users/{id}` | GET | Benutzer abrufen |
| `/api/users/{id}` | PATCH | Benutzer bearbeiten |
| `/api/users/{id}` | DELETE | Benutzer löschen |
| `/api/users/{id}/reset-password` | POST | Passwort zurücksetzen |
| `/api/users/{id}/link-speaker` | POST | Sprecher verknüpfen |
| `/api/users/{id}/link-speaker` | DELETE | Sprecher-Verknüpfung lösen |

---

## Resource Ownership

### Knowledge Bases

Jede Wissensdatenbank hat einen Besitzer:

```python
class KnowledgeBase:
    owner_id: int         # Benutzer-ID des Besitzers
    is_public: bool       # Öffentlich für alle mit kb.shared?
```

**Zugriffs-Regeln:**

1. `kb.all` → Voller Zugriff auf alle KBs
2. `owner_id == user.id` → Voller Zugriff auf eigene KBs
3. `is_public == True` + `kb.shared` → Lesezugriff auf öffentliche KBs
4. Explizite `KBPermission` → Per-User Zugriff

### Konversationen

Jede Konversation kann einem Benutzer zugeordnet sein:

```python
class Conversation:
    user_id: int          # Optional, kann NULL sein für anonyme
```

**Zugriffs-Regeln:**

1. `chat.all` → Voller Zugriff auf alle Konversationen
2. `user_id == user.id` + `chat.own` → Zugriff auf eigene Konversationen
3. `user_id == NULL` → Anonyme Konversationen (Legacy)

---

## Knowledge Base Sharing

### KB teilen

```bash
curl -X POST http://localhost:8000/api/knowledge/bases/1/share \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 5,
    "permission": "read"
  }'
```

### Permission-Level

| Level | Beschreibung |
|-------|--------------|
| `read` | KB in Suche verwenden, Dokumente lesen |
| `write` | Dokumente hinzufügen/bearbeiten |
| `admin` | KB löschen, mit anderen teilen |

### Sharing-API

| Endpoint | Methode | Beschreibung |
|----------|---------|--------------|
| `/api/knowledge/bases/{id}/share` | POST | KB mit Benutzer teilen |
| `/api/knowledge/bases/{id}/permissions` | GET | Alle Berechtigungen auflisten |
| `/api/knowledge/bases/{id}/permissions/{perm_id}` | DELETE | Berechtigung entziehen |
| `/api/knowledge/bases/{id}/public` | PATCH | Öffentlich/Privat setzen |

### Öffentliche Knowledge Bases

```bash
# KB öffentlich machen
curl -X PATCH http://localhost:8000/api/knowledge/bases/1/public \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"is_public": true}'
```

Öffentliche KBs sind für alle Benutzer mit mindestens `kb.shared` sichtbar.

---

## Voice Authentication

### Übersicht

Voice Authentication ermöglicht Login per Stimmerkennung:

1. Audio-Datei an `/api/auth/voice` senden
2. Sprechererkennung identifiziert den Sprecher
3. Wenn Sprecher mit User verknüpft → JWT Tokens zurückgeben

### Aktivierung

```bash
VOICE_AUTH_ENABLED=true
VOICE_AUTH_MIN_CONFIDENCE=0.7    # Minimum Confidence (0-1)
```

### Sprecher mit User verknüpfen

```bash
# Admin verknüpft Sprecher-ID 3 mit User-ID 5
curl -X POST http://localhost:8000/api/users/5/link-speaker \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"speaker_id": 3}'
```

### Voice Login

```bash
curl -X POST http://localhost:8000/api/auth/voice \
  -F "audio_file=@recording.wav"
```

**Erfolgreiche Antwort:**
```json
{
  "success": true,
  "speaker_id": 3,
  "speaker_name": "Max Mustermann",
  "confidence": 0.85,
  "user_id": 5,
  "username": "max",
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "message": "Voice authentication successful"
}
```

**Fehlgeschlagene Antwort:**
```json
{
  "success": false,
  "speaker_id": 3,
  "speaker_name": "Max Mustermann",
  "confidence": 0.65,
  "message": "Confidence too low (0.65 < 0.70)"
}
```

---

## API Referenz

### Authentifizierung

| Endpoint | Methode | Auth | Beschreibung |
|----------|---------|------|--------------|
| `/api/auth/login` | POST | - | Login (OAuth2 Password Flow) |
| `/api/auth/register` | POST | - | Selbst-Registrierung |
| `/api/auth/refresh` | POST | - | Token erneuern |
| `/api/auth/me` | GET | Bearer | Aktueller Benutzer |
| `/api/auth/status` | GET | Optional | Auth-Status |
| `/api/auth/change-password` | POST | Bearer | Passwort ändern |
| `/api/auth/voice` | POST | - | Voice Login |
| `/api/auth/permissions` | GET | - | Alle Permissions |

### Login-Flow

```bash
# 1. Login
TOKEN_RESPONSE=$(curl -X POST http://localhost:8000/api/auth/login \
  -d "username=admin&password=changeme")

ACCESS_TOKEN=$(echo $TOKEN_RESPONSE | jq -r '.access_token')
REFRESH_TOKEN=$(echo $TOKEN_RESPONSE | jq -r '.refresh_token')

# 2. API-Aufruf mit Token
curl http://localhost:8000/api/auth/me \
  -H "Authorization: Bearer $ACCESS_TOKEN"

# 3. Token erneuern
NEW_TOKENS=$(curl -X POST http://localhost:8000/api/auth/refresh \
  -H "Content-Type: application/json" \
  -d "{\"refresh_token\": \"$REFRESH_TOKEN\"}")
```

---

## Beispiel-Szenarien

### Szenario 1: Familien-Haushalt

```
Admin-Benutzer "Erik"
├── Rolle: Admin
├── Vollzugriff auf alles
└── Verwaltet Benutzer und Rollen

Benutzer "Partner"
├── Rolle: Familie
├── Volle Smart Home Kontrolle
├── Eigene + geteilte Wissensdatenbanken
└── Kamera-Events ansehen (keine Snapshots)

Benutzer "Kind"
├── Rolle: Familie (angepasst)
├── Smart Home Kontrolle
├── Nur eigene Wissensdatenbanken
└── Keine Kameras

Benutzer "Gast-WLAN"
├── Rolle: Gast
├── Nur Smart Home Status lesen
└── Keine Wissensdatenbanken oder Kameras
```

### Szenario 2: Custom-Rolle "Techniker"

```bash
# Rolle erstellen
curl -X POST http://localhost:8000/api/roles \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Techniker",
    "description": "Voller Smart Home Zugriff für Wartung",
    "permissions": ["ha.full", "rooms.read", "chat.own"]
  }'

# Benutzer mit Rolle erstellen
curl -X POST http://localhost:8000/api/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "handwerker_max",
    "password": "temp-passwort-123",
    "role_id": 4
  }'
```

### Szenario 3: Geteilte Wissensdatenbank

```bash
# 1. KB erstellen (als Owner)
KB_RESPONSE=$(curl -X POST http://localhost:8000/api/knowledge/bases \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Haushalts-Handbücher", "is_public": false}')

KB_ID=$(echo $KB_RESPONSE | jq -r '.id')

# 2. Mit Partner teilen (Schreibzugriff)
curl -X POST "http://localhost:8000/api/knowledge/bases/$KB_ID/share" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_id": 3, "permission": "write"}'

# 3. Öffentlich für Familie machen
curl -X PATCH "http://localhost:8000/api/knowledge/bases/$KB_ID/public" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"is_public": true}'
```

---

## Migration bestehender Daten

Bei Aktivierung von `AUTH_ENABLED` auf einem bestehenden System:

1. **Bestehende Wissensdatenbanken:** `owner_id = NULL`, können vom Admin zugewiesen werden
2. **Bestehende Konversationen:** `user_id = NULL`, bleiben anonym
3. **Sprecher-Profile:** Können nachträglich mit Benutzern verknüpft werden

```sql
-- Beispiel: Alle KBs dem Admin zuweisen
UPDATE knowledge_bases SET owner_id = 1 WHERE owner_id IS NULL;

-- Beispiel: Alle KBs öffentlich machen
UPDATE knowledge_bases SET is_public = true;
```

---

## Troubleshooting

### Token abgelaufen

```
401 Unauthorized: Invalid authentication token
```

**Lösung:** Refresh-Token verwenden um neuen Access-Token zu erhalten.

### Permission denied

```
403 Forbidden: Permission required: ha.control
```

**Lösung:** Benutzer-Rolle anpassen oder entsprechende Berechtigung hinzufügen.

### Selbst nicht deaktivieren

```
400 Bad Request: Cannot deactivate your own account
```

**Lösung:** Ein anderer Admin muss den Account deaktivieren.

### Voice Auth Confidence zu niedrig

```
{"success": false, "message": "Confidence too low (0.65 < 0.70)"}
```

**Lösung:**
1. Mehr Voice-Samples zum Sprecher hinzufügen
2. `VOICE_AUTH_MIN_CONFIDENCE` senken (weniger sicher)
3. Ruhigere Umgebung für Aufnahme

---

## Sicherheits-Hinweise

1. **SECRET_KEY:** Immer einen starken, zufälligen Key verwenden
2. **Admin-Passwort:** Nach erstem Login sofort ändern
3. **ALLOW_REGISTRATION:** In Produktion auf `false` setzen
4. **HTTPS:** Für Produktion unbedingt HTTPS aktivieren (via Nginx)
5. **Token-Speicherung:** Access-Tokens nie in localStorage speichern (nur Memory)
6. **Voice Auth:** Confidence-Threshold nicht zu niedrig setzen
