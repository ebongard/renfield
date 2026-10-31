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
ALLOW_REGISTRATION=false   # Pflicht auf einer Produktions-/Staging-Instanz: ohne ausdrücklichen Wert startet sie nicht
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
# 🛑 AUS LASSEN. Eine Tonaufnahme der Stimme reicht für Zugriffs- UND
# Erneuerungstoken; es gibt keine Lebendigkeitsprüfung und keinen zweiten
# Faktor. Bis 2026-09-27 war die Route ohnehin unbenutzbar (falsche
# Aufrufsignatur → `TypeError` bei jedem Versuch); sie funktioniert jetzt,
# ist aber nicht empfehlenswert. Details: `docs/ENVIRONMENT_VARIABLES.md`.
VOICE_AUTH_ENABLED=false
VOICE_AUTH_MIN_CONFIDENCE=0.7
```

### Erster Start

Beim ersten Start mit `AUTH_ENABLED=true`:

1. Standard-Rollen werden automatisch erstellt (Admin, Familie, Gast, Kiosk)
2. Ein Admin-Benutzer wird erstellt mit den konfigurierten Zugangsdaten
3. **Erzwungen:** Der Bootstrap-Admin startet immer mit `must_change_password=true` (auch bei operator-gesetztem `DEFAULT_ADMIN_PASSWORD`). Nach dem ersten Login leitet die App serverseitig-erzwungen nach `/change-password` — jede andere Route gibt bis zur Rotation `403 password_change_required`. Das neue Passwort darf nicht dem aktuellen oder dem `DEFAULT_ADMIN_PASSWORD` entsprechen.

```
⚠️  Default admin created: 'admin' - CHANGE THE PASSWORD IMMEDIATELY!
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
| **MCP Tools** | `mcp.*`, `mcp.<server>.*`, `mcp.<server>.<tool>` | MCP-Server Tool-Zugriff |
| **Kiosk** | `kiosk.view` | Wanddisplay: `/kiosk` und `/ws/kiosk` |
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

MCP Tools:
  mcp.* → mcp.<server>.* → mcp.<server>.<tool>
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
| **Admin** | Vollzugriff | Alle Berechtigungen + `mcp.*` |
| **Familie** | Familienmitglieder | `kb.shared`, `ha.full`, `cam.view`, `chat.own`, `rooms.read`, `speakers.own`, `tasks.view`, `rag.use`, `plugins.use`, `notifications.view`, `mcp.*` |
| **Gast** | Eingeschränkter Zugriff | `kb.none`, `ha.read`, `cam.none`, `chat.own`, `rooms.read`, `plugins.none` (kein MCP-Zugriff) |
| **Kiosk** | Wanddisplay (Gerätekonto) | `kiosk.view`, `rooms.read` — sonst nichts: kein Chat, keine KB, keine HA-Steuerung, kein MCP |

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

> **Grant-only-what-you-hold (Security-Audit 2026-07-21).** Beim Erstellen/Bearbeiten einer Rolle und beim Zuweisen einer Rolle an einen Benutzer kann der Aufrufer nur Permissions vergeben, die er **selbst** hält (`admin` ist ein Superset; MCP-Wildcards + Hierarchie zählen). Ein `roles.manage`/`users.manage`-Inhaber ohne `admin` kann sich also **nicht** selbst zum Admin machen und nicht seine eigene Rolle ändern. Das Ändern der Permissions einer **System-Rolle** erfordert zusätzlich `admin`. Außerdem verhindern **Last-Admin-Guards**, dass der letzte aktive Admin herabgestuft/deaktiviert/gelöscht wird (400). Details: [SECURITY.md → Login & User-Management Audit Hardening](SECURITY.md).

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
| `/api/users/{id}` | DELETE | Benutzer löschen — **409, solange das Konto noch Daten hält**. 36 Fremdschlüssel zeigen ohne `ON DELETE` auf `users.id` (Atome, Kreis-Mitgliedschaften, Benachrichtigungen, Erinnerungen, Vorschläge); die Datenbank verweigert, die Route macht daraus eine Antwort mit Code `user_still_referenced` und der blockierenden Tabelle. Kein Kaskadieren: die Atome eines Mitglieds sind Wissen auf Haushalts-Stufe, das andere weiter lesen. Der unterstützte Weg ist **Deaktivieren** (`is_active=false`): der Zugang geht, das Wissen bleibt. |
| `/api/users/{id}/reset-password` | POST | Passwort zurücksetzen |
| `/api/users/{id}/unlock` | POST | Anmeldesperre aufheben (`users.manage`; `GET /api/users` liefert `locked_out`) |
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

Seit dem 2026-09-27 ist die Stimme ein **Zusatzfaktor**, nicht mehr ein eigener
Anmeldeweg. Eine Tonaufnahme allein genügt nicht mehr — sie bestätigt eine bereits
mit Passwort bestandene Anmeldung.

**Ablauf:**

1. `POST /api/auth/login` mit Benutzername und Passwort. Für ein Konto mit
   gesetztem `voice_second_factor_enabled` kommen **keine Token** zurück, sondern
   `second_factor: "voice"` und ein `second_factor_ticket`.
2. `POST /api/auth/voice` mit diesem Ticket und einer Aufnahme. Geprüft wird
   **1:1** gegen das verknüpfte Sprecherprofil.
3. Erst hier entstehen Zugriffs- und Erneuerungstoken samt HttpOnly-Cookies.

**Einschalten** braucht deshalb zwei Dinge — den Sprachweg und die Einwilligung
der betroffenen Person:

```bash
VOICE_AUTH_ENABLED=true            # Standard false; ohne das ruht die Hürde
VOICE_AUTH_MIN_CONFIDENCE=0.7      # Minimum Confidence (0-1)
VOICE_SECOND_FACTOR_TTL_SECONDS=180  # Lebensdauer des Zwischentickets
```

Die Einwilligung ist eine Spalte je Person (`users.voice_second_factor_enabled`,
Standard `false`), **kein** Flag: ein ECAPA-Stimmabdruck ist biometrisches Datum
(Art. 9 DSGVO), und dass eine Anmeldung ihn verlangt, kann niemand für jemand
anderen entscheiden. `speaker_id` taugt dafür nicht — die Verknüpfung entstand für
die Sprecherkennung, nicht als Zustimmung zur Anmeldung.

🛑 **Es gibt keinen Rückfall auf Passwort allein.** Ein Rückfall, den ein Angreifer
selbst auslösen kann (indem er die Stimmprüfung wiederholt scheitern lässt), hebt
den zweiten Faktor auf. Wiederherstellung bei defektem Mikrofon oder Erkältung:
ein Administrator schaltet `voice_second_factor_enabled` für diese Person ab.

### 🛑 Die Einwilligung gehört der Person — Selbstbedienung unter **Mein Konto**

Bis zum 2026-09-28 stand `users.manage` vor **beiden** Richtungen, und die einzige
Oberfläche war die Benutzerverwaltung. Damit war die Einwilligung ausgerechnet für
die Person unerreichbar, um deren Stimme es geht: ein Haushaltsmitglied konnte weder
einwilligen noch überhaupt sehen, ob der Faktor für sein Konto gilt. **Eine
Einwilligung nach Art. 9 DSGVO, die nur ein Dritter erteilen kann, ist keine.**

Seither:

* **`/settings/account` („Mein Konto")** — keine Admin-Route, jede angemeldete Person
  erreicht sie. Dort steht der Schalter für das eigene Konto.
* **`GET /auth/me` führt `voice_second_factor_enabled`** — derselbe Grund, aus dem
  `must_change_password` daneben steht: ein Zustand, auf den die Oberfläche reagieren
  soll, muss ablesbar sein.
* Die Route hängt an `get_user_or_default` statt an `require_permission(USERS_MANAGE)`.
  Den eigenen Faktor scharf zu stellen fügt eine *zusätzliche* Hürde am eigenen Konto
  hinzu — keine Rechteausweitung. Ein fremdes Konto abzuschalten verlangt unverändert
  `admin`.
* 🛑 **Das Selbst-Abschalten verlangt das Passwort erneut.** Das Einschalten hebt
  `token_epoch` bewusst **nicht** an — ein Epoch-Sprung würde die Person im Moment des
  Einwilligens abmelden, auf einer Ein-Admin-Instanz mit klemmendem Sprachweg eine
  sofortige Aussperrung. Folge: ein Token von *vor* der Einwilligung überlebt sie und
  erneuert sich über `/auth/refresh`, das den Faktor nicht prüft. Ohne Riegel dürfte
  genau dieses Token die Einwilligung dauerhaft zurücknehmen — vorher verlangte das
  `users.manage`. Das Passwort ersetzt diesen Schutz: wer nur ein Token erbeutet hat,
  kommt nicht durch; wer auch das Passwort hat, war ohnehin nur noch durch die Stimme
  getrennt.

  Eine **Stimmprobe** zu verlangen wäre zirkulär: genau wer nicht sprechen kann,
  braucht diesen Weg. Für ein *fremdes* Konto entfällt die Frage — dort steht `admin`,
  und eine Administratorin kennt das fremde Passwort nicht.
* 🛑 **Bei `AUTH_ENABLED=false` verweigert die Route BEIDE Richtungen mit 401.**
  `get_user_or_default` löst dort jeden Aufrufer auf das Administratorkonto auf —
  „selbst" wäre dann jeder, der den Port erreicht, und die Einschaltrichtung prüft
  `VOICE_AUTH_ENABLED` bewusst nicht. Der Schreibvorgang ginge also auch bei ruhendem
  Sprachweg durch und würde scharf, sobald jemand die Auth einschaltet. Eine
  Einwilligung nach Art. 9 DSGVO verlangt eine Person; „irgendwer am Port" ist keine.
* 🛑 **Die Berechtigungsentscheidung steht VOR der Datenbankabfrage.** Sonst
  unterschiede die Antwort für einen Unberechtigten 404 („gibt es nicht") von 403
  („gibt es, nicht deins") — ein Aufzählungsorakel über den ganzen Id-Raum, das jedem
  angemeldeten Mitglied offenstünde. Vorher verwehrte `users.manage` den Zutritt vor
  der Abfrage; mit der Lockerung muss die Reihenfolge diesen Schutz ersetzen.
* **Das Entfernen wird als WARNING protokolliert, das Erteilen als INFO.** Ein Mensch,
  der die Hürde wegnimmt, darf nicht leiser vermerkt sein als eine Hürde, die von
  selbst einschläft (`voice_factor_preconditions` schreibt dort WARNING).
* **Ein Gerätekonto wird mit 409 abgewiesen.** Es spricht nicht und meldet sich nicht
  über `/auth/login` an; eine Einwilligung, die es nie einlösen kann, ist ein Zustand,
  den niemand gebrauchen kann.

### Die Hürde steht genau dann, wenn sie auch fällt

🛑 **Der Fehler, gegen den `services/voice_factor_preconditions` geschrieben ist.**
`/auth/login` stellte die Hürde, sobald `VOICE_AUTH_ENABLED` an war. Zum Einlösen
verlangte `/auth/voice` aber vier weitere Dinge. **Jede Lücke zwischen den beiden
Mengen ist eine Aussperrung**, denn einen Rückfall gibt es bewusst nicht:

| Vorbedingung | prüfte Login vorher | prüft `/auth/voice` |
|---|---|---|
| `VOICE_AUTH_ENABLED` | ✅ | ✅ |
| `SPEAKER_RECOGNITION_ENABLED` | ❌ | ✅ |
| verknüpftes Sprecherprofil | ❌ | ✅ |
| mindestens eine Einbettung | ❌ | ✅ |
| brauchbarer Einbettungsweg | ❌ | ✅ (früher **nie** erfüllbar, s.u.) |

Alle drei Stellen fragen jetzt denselben Prüfer. Fehlt eine Vorbedingung, **ruht**
die Hürde: der Passwortweg bleibt offen, ein WARNING geht ins Protokoll, und die
Einwilligung **bleibt gespeichert**. Sie ist der Nachweis einer Erklärung der
Person (Art. 9 DSGVO), kein Schalter, den das System still umlegen darf — kehrt
das Profil zurück, greift die Anforderung von selbst wieder. Das ist dieselbe
Regel, die für `VOICE_AUTH_ENABLED=false` schon galt, nur konsequent angewandt.

Ein Angreifer gewinnt dadurch nichts: um die Hürde zum Ruhen zu bringen, muss er
das Profil entfernen, und das verlangt `users.manage` bzw. `speakers.all` — dieselbe
Berechtigung, mit der er die Einwilligung ohnehin direkt abschalten könnte.

### Die Stimmprobe kommt vom voice-server

`/auth/voice` berechnet die Einbettung **nicht** im Backend-Prozess. Der Weg dorthin
(`SpeakerService.extract_embedding_from_bytes`) ist durch
`SPEAKER_INPROCESS_EMBEDDINGS_ENABLED=false` auf beiden Instanzen verriegelt — der
zentrale Riegel nennt „voice-login" ausdrücklich als abgedeckten Aufrufer — und selbst
offen wäre es der **falsche Vektorraum**: gespeichert sind voice-server-ONNX-Vektoren,
berechnet würde SpeechBrain. Also derselbe Weg wie beim Anlernen: `voice_server_client.stt()`
mit einem kurzlebigen Dienst-Token (hier ist noch niemand angemeldet). `verify_speaker`
bleibt unberührt — reine Kosinus-Ähnlichkeit, nicht am Riegel.

Nebengewinn: der voice-server misst die Aufnahme selbst. `audio_duration_s` wird gegen
`SPEAKER_RECOGNITION_MIN_DURATION_S` geprüft — eine **serverseitige** Mindestdauer. Die
1,5 s in der Erfassungsmaske sind Bedienführung; ein Angreifer schickt direkt an die API.

### Wo die Spalte geschaltet wird — und warum getrennt von `PATCH /users/{id}`

`POST /api/users/{id}/voice-second-factor` (`{"enabled": true|false}`), in der
Oberfläche die Schild-Schaltfläche je Zeile unter **Verwaltung → Benutzer** — und für
das eigene Konto unter **Mein Konto** (`/settings/account`, ohne Verwaltungsrecht
erreichbar). Die Route ist bewusst **nicht** Teil des allgemeinen
`PATCH /users/{id}`, weil dort beide Richtungen dieselbe Berechtigung hätten — die
Asymmetrie wäre nicht abbildbar:

| Richtung | Wer darf | Grund |
|---|---|---|
| **Einschalten** | nur das Konto selbst (sonst **403**) — **angemeldet genügt, kein `users.manage`** | Einwilligung in biometrische Verarbeitung; niemand kann sie für jemand anderen geben |
| **Ausschalten (eigenes Konto)** | angemeldet genügt | den eigenen Faktor zurückzunehmen ist niemandes Rechteausweitung — und wer angemeldet ist, hat ihn bei scharfem Faktor bereits bestanden |
| **Ausschalten (fremdes Konto)** | `admin` | 🛑 der Rückweg ist zugleich ein Angriffsweg: er senkt ein fremdes Konto still auf Passwort allein, und *danach* kann man sich an diesem Passwort versuchen — mehr als ein Passwort-Zurücksetzen, das an einem scharfen Faktor nicht vorbeikommt. `users.manage` ist delegierbar ohne `admin`. |

Das Einschalten wird zusätzlich mit **409** verweigert, solange der Einlöseweg nicht
trägt — kein Sprecherprofil, keine Einbettungen, oder abgeschaltete Sprecherkennung
(derselbe Prüfer wie oben). `VOICE_AUTH_ENABLED` wird dabei bewusst **nicht** geprüft,
sonst wäre die Reihenfolge des Cutovers unmöglich: erst die Einwilligungen einsammeln,
dann das Flag umlegen. Solange ruht die Hürde. Grund für die Sperre: `POST /auth/voice`
prüft 1:1 dagegen und verweigert fail-closed, das Konto käme also nie wieder herein.
Die Oberfläche zeigt die Schaltfläche in diesem Fall gesperrt statt sie ins 409
laufen zu lassen. Beide Richtungen werden protokolliert (wer, wann, für welches Konto).

#### Der Grund ist lesbar, bevor jemand einwilligt

`GET /api/users/{id}/voice-second-factor` → `{enabled, blocker}`, **nur für das eigene
Konto** (sonst 403; der Grund nennt eine Eigenschaft des Sprecherprofils einer Person
und gehört ihr). `blocker` trägt einen der vier maschinenlesbaren Codes —
`voice_path_off`, `recognition_off`, `no_profile`, `no_embeddings` — oder `null`.
Ein fünfter, `device_account`, kommt **nur** aus der 409-Antwort: ein Gerätekonto
meldet sich nicht an und liest seinen Zustand nie selbst.

🛑 **Der Code geht heraus, nicht der Satz** — aus **jeder** 409 dieser Route.
Übersetzt wird in der Oberfläche, auf „Mein Konto" wie in der Benutzerverwaltung.
Vorher schickte der Server hartkodierte **englische** Sätze, und das war zugleich die
einzige Stelle, an der die Person den Grund überhaupt erfuhr.

🛑 **Der erste Anlauf traf nur die benannten Fälle.** Ersetzt wurden die drei Sätze
aus dem Befund; die Gerätekonto-Absage vier Zeilen darüber, in derselben Funktion,
blieb englisch stehen und wurde erst in der Abnahme gefunden. Deshalb steht jetzt ein
**Strukturtest** daneben, der die Quelle liest und `detail="…"` in jedem 409-Zweig
verbietet: ein Verhaltenstest deckt nur die Fälle ab, an die jemand gedacht hat.

🛑 **Der Grund gilt unabhängig von der Einwilligung.** `/settings/account` zeigte ihn
zuvor nur im Zustand „scharf" — also nie für jemanden, der gerade überlegt
einzuwilligen — und kannte ausserdem nur die instanzweite Hälfte der Vorbedingungen
(aus den Feature-Flags). Gemessen im Haushalt am 2026-09-29: bei **6 von 7 Konten** war
`no_profile` der Blocker, genau die kontogebundene Hälfte. Sie sahen ein blankes „Aus"
und eine Schaltfläche, die fehlschlagen musste.

`voice_factor_blocker()` in `services/voice_factor_preconditions` liefert beide
Hälften, die **instanzweite zuerst**: ein abgeschalteter Sprachweg ist die dominante
Wahrheit. Für das EINSCHALTEN bleibt `voice_path_off` folgenlos (Cutover-Reihenfolge),
die drei anderen sperren die Schaltfläche. Das Abschalten hängt an **keiner**
Vorbedingung — es ist der Rückweg.

🛑 **Was die Stimme weiterhin nicht kann:** eine Aufnahme kann sie täuschen, es gibt
keine Lebendigkeitsprüfung. Als *erster* Faktor war das die ganze Tür; als
*zweiter* braucht ein Angreifer zusätzlich das Passwort. Genau darin liegt der
Gewinn — und darin die Grenze.

### Im Browser

`LoginPage` fängt `SecondFactorRequired` (aus `AuthContext.login`) ab und zeigt
statt des Formulars `components/auth/VoiceSecondFactorStep`. Die Maske nimmt auf
(`hooks/useVoiceFactorRecording`: Mindestdauer 1,5 s, Selbststopp bei 8 s, kein
VAD — eine Anmeldephrase darf nicht bei einer Pause abgeschnitten werden) und
schickt Ticket + Aufnahme an `POST /api/auth/voice`. Erst danach steht die
Sitzung, und `fetchUser` holt den Nutzer über das frisch gesetzte Cookie.

🛑 **Die Maske zeigt die Server-Meldung NICHT durch.** Sie ist absichtlich
uninformativ; sie durchzureichen würde das Orakel in die Oberfläche tragen,
sobald der Server je auskunftsfreudiger wird. Unterscheidbar sind nur Fehler des
NUTZERGERÄTS — verweigertes Mikrofon, kein Gerät, Browser ohne Aufnahme —, denn
wer nicht weiß, dass er das Mikrofon verweigert hat, kann es nicht erlauben.

🛑 **Es gibt keinen Knopf vorbei.** Auch nach mehreren Fehlversuchen nicht; ab dem
dritten nennt die Maske nur den vorgesehenen Weg (administratives Abschalten des
Faktors für dieses Konto). Das Mikrofon wird beim Verlassen der Maske freigegeben
— ein heimlich offenes Mikrofon auf einer Anmeldeseite wäre das Letzte, was man
will.

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

## MCP Tool Permissions

### Übersicht

MCP-Tools (Home Assistant, n8n, Wetter, Suche, etc.) werden durch dynamische Permissions geschützt. Das System ist ein **Hybrid aus Konvention und YAML-Konfiguration**.

### Permission-Typen

| Typ | Beispiel | Beschreibung |
|-----|---------|-------------|
| **Admin-Wildcard** | `mcp.*` | Zugriff auf alle MCP-Tools |
| **Server-Wildcard** | `mcp.calendar.*` | Zugriff auf alle Tools eines Servers |
| **Server-Konvention** | `mcp.weather` | Zugriff auf alle Tools des Servers `weather` |
| **Tool-spezifisch** | `mcp.calendar.read` | Zugriff nur auf ein bestimmtes Tool |

### Konventionsbasiert (Standard)

Ohne explizite YAML-Konfiguration wird automatisch `mcp.<server_name>` als Permission benötigt:

```
Server "weather" → User braucht "mcp.weather"
Server "homeassistant" → User braucht "mcp.homeassistant"
```

### YAML-Konfiguration (optional, granular)

In `config/mcp_servers.yaml` können Server-Level und Tool-Level Permissions definiert werden:

```yaml
servers:
  - name: calendar
    url: "${CALENDAR_MCP_URL:-http://localhost:9095/mcp}"
    transport: streamable_http
    enabled: "${CALENDAR_ENABLED:-false}"
    # Server-Level: User braucht mindestens eine dieser Permissions
    permissions:
      - "mcp.calendar.read"
      - "mcp.calendar.manage"
    # Tool-Level: Spezifische Permission pro Tool (überschreibt Server-Level)
    tool_permissions:
      list_events: "mcp.calendar.read"
      create_event: "mcp.calendar.manage"
      delete_event: "mcp.calendar.manage"
```

### Auflösungsreihenfolge

Die Permission-Prüfung folgt dieser Reihenfolge (first match wins):

1. `user_permissions = None` → **Erlaubt** (AUTH_ENABLED=false, abwärtskompatibel)
2. `mcp.*` in User-Permissions → **Erlaubt** (Admin-Wildcard)
3. `tool_permissions` hat Mapping → Prüfe spezifische Permission
4. `permissions` definiert (Server-Level) → Prüfe ob User mindestens eine hat
5. Keine YAML-Config → Konvention: `mcp.<server_name>` prüfen
6. Keine Übereinstimmung → **Abgelehnt**

### Wildcard-Matching

```
mcp.* → Erlaubt Zugriff auf mcp.weather, mcp.calendar.read, mcp.n8n.list, ...
mcp.calendar.* → Erlaubt mcp.calendar.read, mcp.calendar.manage, ...
mcp.calendar → Erlaubt mcp.calendar UND mcp.calendar.read (Server-Konvention)
```

### Rollen konfigurieren

MCP-Permissions können beim Erstellen/Bearbeiten von Rollen angegeben werden:

```bash
curl -X POST http://localhost:8000/api/roles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Smart-Home-Nutzer",
    "description": "Nur HA und Wetter",
    "permissions": ["ha.read", "mcp.homeassistant", "mcp.weather"]
  }'
```

### Dynamische Permission-Discovery

Der Endpoint `GET /api/roles/permissions/all` liefert auch dynamische MCP-Permissions basierend auf den aktuell verbundenen MCP-Servern. Diese werden im Rollen-Editor im Frontend angezeigt.

### Satellite Voice Auth

Bei Satellites wird die Permission des erkannten Sprechers verwendet:
1. OpenWakeWord erkennt Wake Word
2. Audio wird transkribiert (Whisper)
3. Sprecher wird identifiziert (SpeechBrain)
4. Verknüpfter User wird geladen → `user.get_permissions()` + `user.id`
5. Permission-Check bei MCP-Tool-Ausführung
6. `user_id` wird als `user_id` in MCP-Tool-Parameter injiziert (für per-User-Filterung)

Wenn kein Sprecher erkannt wird oder kein User verknüpft ist, wird `user_permissions=None` und `user_id=None` verwendet (alle Tools erlaubt, kein User-Filter).

---

## Kalender-Sichtbarkeit (Calendar Visibility)

### Übersicht

Das Calendar MCP Server (`renfield-mcp-calendar`) unterstützt per-User-Sichtbarkeit von Kalendern. Dies ermöglicht es, bestimmte Kalender (z.B. Firmenkalender) nur für bestimmte Benutzer sichtbar zu machen.

### Konfiguration

In `config/calendar_accounts.yaml`:

| Feld | Werte | Default | Beschreibung |
|------|-------|---------|--------------|
| `visibility` | `shared`, `owner` | `shared` | Wer sieht den Kalender? |
| `owner_id` | `int` | `null` | User-ID des Besitzers (pflicht bei `visibility: owner`) |

```yaml
calendars:
  - name: work
    label: "Firmenkalender"
    type: ews
    visibility: owner     # Nur der Owner sieht diesen Kalender
    owner_id: 1           # Admin (User-ID 1)
    # ...

  - name: family
    label: "Familienkalender"
    type: google
    visibility: shared    # Alle authentifizierten User sehen diesen Kalender
    # ...
```

### Sichtbarkeits-Regeln

| Bedingung | Ergebnis |
|-----------|----------|
| `user_id = None` (kein Auth / Poller) | **Alle** Kalender sichtbar |
| `visibility: shared` | Sichtbar für **jeden** authentifizierten User |
| `visibility: owner` + `owner_id` match | Sichtbar nur für den Owner |
| `visibility: owner` + kein match | **Nicht sichtbar** (Access denied) |

### MCP Tool-Permissions

Zusätzlich zur Sichtbarkeit schützen MCP-Permissions den Zugriff auf Kalender-Tools:

```yaml
# mcp_servers.yaml
- name: calendar
  permissions:
    - mcp.calendar.read
    - mcp.calendar.manage
  tool_permissions:
    list_calendars: mcp.calendar.read
    list_events: mcp.calendar.read
    get_event: mcp.calendar.read
    get_pending_notifications: mcp.calendar.read
    create_event: mcp.calendar.manage
    update_event: mcp.calendar.manage
    delete_event: mcp.calendar.manage
```

Admin und Familie haben `mcp.*` → voller Zugriff. Gast hat kein MCP → automatisch blockiert.

### Zusammenspiel

1. **MCP Permission** (Renfield Backend) → Darf der User *überhaupt* Kalender-Tools nutzen?
2. **Calendar Visibility** (Calendar MCP Server) → *Welche* Kalender sieht der User?

Beide Prüfungen sind unabhängig voneinander. Die Permission-Prüfung erfolgt im Renfield Backend, die Sichtbarkeitsprüfung im Calendar MCP Server basierend auf dem injizierten `user_id`.

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

**Lösung:**

Alle Fehlschläge des zweiten Faktors antworten absichtlich **identisch**
(`{"success": false, "message": "Voice authentication failed"}`) — kein Feld und
kein Text unterscheidet „Ticket abgelaufen" von „Stimme passt nicht" von „Konto
gesperrt". Das ist derselbe Grundsatz, nach dem der Passwortpfad „Nutzer
unbekannt" und „Passwort falsch" nicht trennt: sonst wäre die Route eine
Auskunftsstelle. **Die Diagnose steht im Server-Protokoll**, dort mit Grund und
Messwert.

Zur Fehlersuche also ins Backend-Log sehen, nicht in die Antwort. Dann:

1. Mehr Voice-Samples zum Sprecher hinzufügen (`/speakers`)
2. Ruhigere Umgebung für die Aufnahme
3. `VOICE_AUTH_MIN_CONFIDENCE` senken — **senkt die Sicherheit**; bei einem Faktor,
   den eine Aufnahme ohnehin täuschen kann, ist das der falsche Hebel
### 🛑 Der Notausgang, wenn es keinen zweiten Administrator gibt

`users.manage` sitzt auf einer Standardinstallation **nur** auf der Admin-Rolle.
Hat die einzige Administratorin den Faktor für sich eingeschaltet und kann dann
nicht sprechen, greift die Kette lückenlos gegen sie: die Anmeldung hält die
Token zurück, `/auth/voice` scheitert an der Stimme, und Abschalten verlangt
`users.manage` — das nur sie hat. **Kein API-Aufruf stellt die Instanz wieder
her.** Der Haushalt ist genau so eine Instanz.

Dafür gibt es `bin/voice_2fa_emergency.py`, das direkt gegen die Datenbank
schreibt. Es kann **nur abschalten** — Einschalten ist eine Einwilligung in
biometrische Verarbeitung, die eine Person über die Oberfläche für sich selbst
abgibt, nicht ein Betreiber über die Kommandozeile.

🛑 **Das Skript ist NICHT im Bild.** Der Build-Kontext des Backends ist
`src/backend/`, und `COPY . .` sieht das `bin/` des Repos nicht — wie bei jedem
`bin/backfill_*.py`. Es wird zur Laufzeit hineinkopiert (darum trägt es denselben
`_find_backend_dir`-Block: er findet `/app` im Bild von selbst):

```bash
POD=$(kubectl -n renfield get pod -l app.kubernetes.io/name=backend -o name | head -1)
kubectl -n renfield cp bin/voice_2fa_emergency.py "${POD#pod/}":/tmp/voice_2fa_emergency.py

kubectl -n renfield exec "$POD" -- python /tmp/voice_2fa_emergency.py --list
kubectl -n renfield exec "$POD" -- python /tmp/voice_2fa_emergency.py --username admin --off --dry-run
kubectl -n renfield exec "$POD" -- python /tmp/voice_2fa_emergency.py --username admin --off
```

Die Alternative wäre gewesen, das Einschalten für den letzten Administrator zu
verweigern — dann könnte ausgerechnet die Person mit dem größten Schutzbedarf
den Faktor als Einzige nie benutzen.

### Was die Oberfläche zeigt — und was sie dafür wissen muss

Das Abzeichen in der Benutzerliste unterscheidet **„Stimme als 2. Faktor"** (wirkt)
von **„2. Faktor ruht"** (Einwilligung steht, greift aber nicht). Dafür braucht die
Maske **beide** Hälften der Vorbedingungen, und `GET /api/config/features` führt
darum `voice_auth_enabled` und `speaker_recognition_enabled` mit. Kein Geheimnis:
sie sagen nur, ob ein Weg offen ist, nicht wer ihn geht.

🛑 **Einschalten und Ruhen hängen an VERSCHIEDENEN Bedingungen** — die Maske bildet
das nach, weil das Backend es so tut:

| | zählt `voice_auth_enabled`? | zählt `speaker_recognition_enabled`? | zählt das Profil? |
|---|---|---|---|
| **Einschalten** (409) | **nein** | ja | ja |
| **Wirkt der Faktor?** | ja | ja | ja |

`voice_auth_enabled` ist beim Einschalten bewusst ausgenommen: die Cutover-Reihenfolge
lautet erst die Einwilligungen einsammeln, **dann** das Flag umlegen. Sperrte die
Schaltfläche solange, wäre genau das unmöglich. Umgekehrt meldete die Maske ohne diese
Trennung im selben Fenster bei *jeder* eingewilligten Zeile „scharf", obwohl alles ruht
— gefunden bei der Browser-Abnahme am 2026-09-28.

Solange die Schalter noch laden, schließt die Maske **nicht** auf „ruht": eine
unbekannte Antwort ist kein Beweis.

### Sperren: zwei Zähler, ein Knopf

Fehlversuche des zweiten Faktors zählen unter `voice2fa:<user_id>`, nicht unter
dem Benutzernamen. Das ist Absicht: ein Fehlversuch der Stimme darf den
Passwortpfad nicht mitsperren, sonst wäre der zweite Faktor ein Weg, jemanden mit
fremden Mitteln aus seinem Konto zu drängen. Die Benutzerliste und
`POST /api/users/{id}/unlock` prüfen bzw. räumen **beide** Zähler — vorher nur den
des Benutzernamens, sodass die Liste „nicht gesperrt" meldete und der Knopf nichts
tat, während die Person nicht hereinkam. Die Kennung steht in
`services/voice_factor_preconditions.voice_factor_lock_id`, damit sie nicht wieder
auseinanderläuft.

---

4. Kommt jemand dauerhaft nicht durch (defektes Mikrofon, Erkältung):
   `voice_second_factor_enabled` für diese Person abschalten — **Verwaltung →
   Benutzer**, Schild-Schaltfläche in der Zeile, oder
   `POST /api/users/{id}/voice-second-factor` mit `{"enabled": false}`. Das ist der
   vorgesehene Wiederherstellungsweg, nicht ein Notbehelf; er braucht kein
   verknüpftes Sprecherprofil mehr.

---

## Sicherheits-Hinweise

1. **SECRET_KEY:** Immer einen starken, zufälligen Key verwenden
2. **Admin-Passwort:** Nach erstem Login sofort ändern
3. **ALLOW_REGISTRATION:** In Produktion auf `false` setzen
4. **HTTPS:** Für Produktion unbedingt HTTPS aktivieren (via Nginx)
5. **Token-Speicherung:** Access-Tokens nie in localStorage speichern (nur Memory)
6. **Voice Auth:** Confidence-Threshold nicht zu niedrig setzen
