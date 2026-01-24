# Renfield - Feature Dokumentation

## Übersicht

Renfield ist ein vollständig offline-fähiger KI-Assistent, der speziell für Smart Home und Hausautomatisierung entwickelt wurde.

## Chat & Konversation

### Natural Language Understanding
- **Intent Recognition**: Automatische Erkennung von Benutzerabsichten
- **Kontext-Bewusstsein**: Versteht Kontext aus vorherigen Nachrichten
- **Multi-Turn Dialoge**: Führt komplexe Gespräche über mehrere Nachrichten

### Streaming Responses
- **WebSocket-basiert**: Echtzeit-Antworten
- **Token-für-Token**: Sieht Antworten während sie generiert werden
- **Fallback auf HTTP**: Funktioniert auch ohne WebSocket

### Chat-Historie
- **Session Management**: Getrennte Gespräche für verschiedene Themen
- **Persistente Speicherung**: Alle Nachrichten werden in PostgreSQL gespeichert
- **Historie-Suche**: Durchsuche frühere Konversationen
- **WebSocket-Persistenz**: Echtzeit-Speicherung bei jeder Nachricht
- **Satellite-Sessions**: Tägliche Sessions für Voice-Commands
- **Follow-up Kontext**: LLM erhält Konversationshistorie für intelligente Antworten

## Sprach-Interface

### Speech-to-Text (STT)
- **Whisper Integration**: OpenAI's Whisper für hochwertige Transkription
- **Offline-Verarbeitung**: Keine Cloud-Dienste nötig
- **Deutsche Sprache**: Optimiert für deutsche Spracheingabe
- **Modell-Auswahl**: Wählbar zwischen tiny, base, small, medium, large
- **GPU-Beschleunigung**: Optional mit NVIDIA GPU für schnellere Transkription
- **Rauschunterdrückung**: Gute Qualität auch bei Background-Geräuschen

### Text-to-Speech (TTS)
- **Piper Integration**: Natürlich klingende deutsche Stimme
- **Offline-Synthese**: Lokal generiert
- **Mehrere Stimmen**: Verschiedene deutsche Stimmen verfügbar
- **Qualitätsstufen**: Von schnell bis hochwertig

### Voice Chat
- **End-to-End Voice**: Sprechen → Verstehen → Antworten → Vorlesen
- **Streaming Audio**: Antworten werden sofort vorgelesen
- **Hands-Free Mode**: Freihändige Bedienung möglich

## Multi-Room Device System

### Unterstützte Gerätetypen
- **Satellites**: Raspberry Pi Hardware-Geräte mit Wake-Word
- **Web Panels**: Stationäre Web-Panels (z.B. Wand-Tablets)
- **Web Tablets**: Mobile Tablets
- **Web Browser**: Desktop/Mobile Browser
- **Web Kiosk**: Kiosk-Terminals

### Automatische Raum-Erkennung
- **IP-basiert**: Stationäre Geräte werden anhand der IP-Adresse erkannt
- **Kontext-Weitergabe**: Raum-Kontext wird an LLM übergeben
- **Implizite Befehle**: "Schalte das Licht ein" funktioniert ohne Raum-Angabe
- **IP-Update**: IP wird bei jeder Verbindung aktualisiert

### Geräte-Registrierung
- **Frontend Setup**: Geräte-Konfiguration über Web-Interface
- **Persistente Speicherung**: Geräte überleben Neustarts
- **Capability-basiert**: UI passt sich an Gerätefähigkeiten an
- **Raum-Zuweisung**: Geräte werden Räumen zugeordnet

### Raspberry Pi Satellites
- **Pi Zero 2 W Support**: Kostengünstige (~63€) Satellite-Einheiten
- **ReSpeaker 2-Mics HAT**: Hochwertige Mikrofonerfassung mit 3m Reichweite
- **Lokale Wake-Word-Erkennung**: OpenWakeWord mit ONNX Runtime
- **LED-Feedback**: Visuelles Feedback für alle Zustände
- **Hardware-Button**: Manuelle Aktivierung möglich

### Wake-Word Detection
- **Lokale Verarbeitung**: Wake-Word wird auf dem Satellite erkannt
- **Konfigurierbare Keywords**: Alexa, Hey Mycroft, Hey Jarvis, etc.
- **Niedriger CPU-Verbrauch**: ~20% auf Pi Zero 2 W
- **Refractory Period**: Verhindert Doppel-Auslösungen
- **Stop-Word Support**: Laufende Interaktionen abbrechen

### Multi-Room Features
- **Auto-Discovery**: Satellites finden Backend automatisch via Zeroconf/mDNS
- **Parallele Verarbeitung**: Mehrere Räume gleichzeitig bedienen
- **Session-Routing**: Antworten werden zum richtigen Satellite geroutet
- **Room-Independence**: Räume blockieren sich nicht gegenseitig

### LED-Feedback
| Zustand | Muster | Farbe |
|---------|--------|-------|
| Idle | Dimmes Pulsieren | Blau |
| Listening | Durchgehend | Grün |
| Processing | Laufen | Gelb |
| Speaking | Atmen | Cyan |
| Error | Blinken | Rot |

### WebSocket Protokoll
- **Audio-Streaming**: 16-bit PCM, 16kHz, Mono
- **Base64-Encoding**: Für WebSocket-Übertragung
- **Heartbeat**: Verbindung wird überwacht
- **Auto-Reconnect**: Automatische Wiederverbindung bei Ausfall

### Verbindungs-Architektur (Frontend)

Das Frontend verwendet **zwei separate WebSocket-Verbindungen** für unterschiedliche Zwecke:

| Verbindung | Endpoint | Zweck | Status-Anzeige |
|------------|----------|-------|----------------|
| **Chat WebSocket** | `/ws` | Senden/Empfangen von Chat-Nachrichten | "Verbunden" im Chat-Fenster |
| **Device WebSocket** | `/ws/device` | Geräte-Registrierung, Raum-Zuweisung, Capabilities | Status im Header |

**Wichtig:** Diese Verbindungen sind unabhängig voneinander:
- Der Chat kann verbunden sein, während das Gerät nicht registriert ist
- Der Header zeigt "Offline" oder "Setup" wenn keine Geräte-Registrierung erfolgt ist
- Das Chat-Fenster zeigt "Verbunden" sobald der Chat-WebSocket aktiv ist

**Unterschied der Funktionen:**

| Feature | Nur Chat WS | Mit Device-Registrierung |
|---------|-------------|--------------------------|
| Nachrichten senden/empfangen | ✓ | ✓ |
| Raum-Kontext für Befehle | ✗ | ✓ |
| Geräte-Capabilities (Mikrofon, Lautsprecher) | ✗ | ✓ |
| Persistente Geräte-Identität | ✗ | ✓ |
| Auto-Raum-Erkennung für "Licht einschalten" | ✗ | ✓ |

**Geräte-Registrierung aktivieren:**
1. Klicke auf "Setup" oder "Offline" im Header
2. Wähle einen Raum und Gerätetyp
3. Optional: Aktiviere Mikrofon/Lautsprecher-Capabilities
4. Nach erfolgreicher Registrierung zeigt der Header den Raumnamen an

## Raum-Management

### Raum-Verwaltung
- **CRUD-Operationen**: Räume erstellen, bearbeiten, löschen
- **Alias-System**: Normalisierte Namen für Sprachbefehle
- **Source-Tracking**: Ursprung des Raums (Renfield, Home Assistant, Satellite)
- **Icon-Support**: Material Design Icons für Räume

### Home Assistant Area Sync
- **Bidirektionaler Sync**: Import und Export von Areas
- **Konfliktlösung**: Skip, Link oder Overwrite bei Namenskollisionen
- **Automatische Verknüpfung**: Räume mit gleichen Namen werden verknüpft
- **Area Registry API**: Nutzt HA WebSocket API

### Geräte pro Raum
- **Übersicht**: Alle Geräte eines Raums auf einen Blick
- **Online-Status**: Echtzeit-Anzeige welche Geräte verbunden sind
- **Geräte-Icons**: Visuelle Unterscheidung nach Gerätetyp
- **Geräte verschieben**: Geräte zwischen Räumen verschieben

## Home Assistant Integration

### Gerätesteuerung
- **Lichter**: Ein/Aus/Dimmen/Farbsteuerung
- **Schalter**: Beliebige Schalter steuern
- **Klimaanlagen**: Temperatur und Modi setzen
- **Rollläden**: Öffnen/Schließen/Position setzen
- **Sensoren**: Status abfragen

### Natural Language Control
```
"Schalte das Licht im Wohnzimmer ein"
"Mach die Heizung im Schlafzimmer auf 21 Grad"
"Sind alle Fenster geschlossen?"
"Schließe alle Rollläden"
```

### Entity Discovery
- **Automatische Erkennung**: Findet alle Home Assistant Entities
- **Fuzzy Search**: Versteht auch ungenaue Namen
- **Domain-Filterung**: Zeige nur bestimmte Gerätetypen
- **Echtzeitstatus**: Live-Updates der Gerätezustände

### Szenen und Automationen
- **Szenen aktivieren**: "Aktiviere Filmabend"
- **Automationen triggern**: "Starte Gute-Nacht-Routine"
- **Gruppensteuerung**: Mehrere Geräte gleichzeitig

## Kamera-Überwachung

### Frigate Integration
- **Event-Erkennung**: Person, Auto, Tier, etc.
- **Objekt-Tracking**: Verfolgt bewegte Objekte
- **Snapshot-Zugriff**: Bilder von Events abrufen
- **Zone-Überwachung**: Verschiedene Bereiche definieren

### Intelligente Benachrichtigungen
- **Relevanz-Filter**: Nur wichtige Events
- **Person-Erkennung**: Unterscheidet zwischen Personen und anderen Objekten
- **Tageszeit-Anpassung**: Unterschiedliche Regeln für Tag/Nacht
- **Bekannte Gesichter**: Optional mit Gesichtserkennung

### Event-Historie
- **Zeitliche Suche**: Events nach Zeitraum filtern
- **Objekt-Filterung**: Nur bestimmte Objekttypen
- **Konfidenz-Werte**: Wie sicher die Erkennung war
- **Multi-Kamera**: Alle Kameras im Überblick

### Real-Time Monitoring
- **MQTT Events**: Sofortige Benachrichtigung bei neuen Events
- **Live-Status**: Aktuelle Kamera-Stati
- **Streaming**: Optional Live-Streams anzeigen

## n8n Workflow Integration

### Workflow-Trigger
- **Webhook-basiert**: Triggert n8n Workflows per Webhook
- **Parameter-Übergabe**: Sendet Daten an Workflows
- **Status-Feedback**: Erhält Rückmeldung vom Workflow
- **Error-Handling**: Behandelt Fehler graceful

### Anwendungsfälle
```
"Erstelle ein Backup"
"Sende mir den Wochenbericht"
"Starte die Abendroutine"
"Prüfe die Sensoren"
```

### Workflow-Verwaltung
- **Name-Mapping**: Workflows über Namen ansprechen
- **Dokumentation**: Workflows in Datenbank dokumentieren
- **Scheduling**: Zeitgesteuerte Workflows

## Plugin System

### YAML-basierte Plugins
- **Keine Code-Änderungen**: Plugins werden über YAML definiert
- **Hot-Reload**: Plugins werden beim Start geladen
- **Umgebungsvariablen**: Konfiguration über .env

### Verfügbare Plugins
- **Weather** (OpenWeatherMap): Wetterdaten und Vorhersagen
- **News** (NewsAPI): Aktuelle Nachrichten
- **Search** (SearXNG): Web-Suche ohne API-Key
- **Music** (Spotify): Musik-Steuerung

### Plugin-Entwicklung
- **Einfache Syntax**: YAML-basierte Definition
- **API-Mapping**: HTTP-Anfragen konfigurieren
- **Response-Mapping**: Antworten transformieren
- **Intent-Integration**: Automatische Intent-Erkennung

## Task Management

### Task-Queue
- **Asynchrone Verarbeitung**: Tasks laufen im Hintergrund
- **Prioritäts-System**: Wichtige Tasks zuerst
- **Status-Tracking**: Pending, Running, Completed, Failed
- **Result-Storage**: Ergebnisse werden gespeichert

### Task-Typen
- **Home Assistant**: Gerätesteuerung
- **n8n**: Workflow-Trigger
- **Research**: Web-Recherchen
- **Camera**: Kamera-Analysen
- **Custom**: Eigene Task-Typen

### Task-History
- **Vollständiges Log**: Alle Tasks mit Zeitstempel
- **Filterung**: Nach Status, Typ, Datum
- **Error-Logs**: Detaillierte Fehlermeldungen
- **Performance-Metriken**: Laufzeit-Statistiken

## KI-Features

### Ollama LLM
- **Lokale Verarbeitung**: Kein Internet nötig
- **Modell-Auswahl**: Verschiedene Größen verfügbar
- **GPU-Beschleunigung**: Optional für bessere Performance
- **Kontext-Fenster**: Großer Kontext für komplexe Anfragen
- **Externe Instanz**: Kann auf separatem GPU-Server laufen

### Intent Recognition
- **Automatisch**: Erkennt Benutzerabsicht aus Text
- **Multi-Intent**: Kann mehrere Absichten kombinieren
- **Confidence-Scores**: Wie sicher die Erkennung ist
- **Fallback**: Bei Unsicherheit nachfragen

### Kontextverständnis
- **Session-Memory**: Merkt sich Gespräch
- **Entity-Resolution**: Versteht "es" und "dort"
- **Time-Awareness**: Versteht zeitliche Bezüge
- **Location-Awareness**: Versteht Räume und Orte
- **Follow-up Fragen**: Versteht "Mach es aus" oder "Und morgen?" ohne explizite Referenzen

### Konversations-Persistenz
- **WebSocket-Integration**: Kontext bleibt über die gesamte Session erhalten
- **Satellite-Support**: Tägliche Sessions für Voice-Commands
- **Datenbank-Speicherung**: Alle Nachrichten werden in PostgreSQL gespeichert
- **History-Loading**: Kontext wird aus DB geladen bei Reconnect

**Beispiel für Follow-up:**
```
Nutzer: "Schalte das Licht im Wohnzimmer an"
Renfield: "Ich habe das Licht eingeschaltet."

Nutzer: "Mach es wieder aus"
→ Versteht "es" = Wohnzimmer-Licht
Renfield: "Ich habe das Licht ausgeschaltet."
```

**Unterstützte Kanäle:**
| Kanal | Session-ID | Historie |
|-------|------------|----------|
| WebSocket (`/ws`) | Client-provided | 10 Nachrichten |
| Satellite | Tägliche Sessions | 5 Nachrichten |
| REST API | Client-provided | 20 Nachrichten |

## Progressive Web App

### Multi-Platform
- **Desktop**: Vollwertiger Browser
- **Tablet**: Optimierte Touch-Bedienung
- **Smartphone**: Mobile-First Design
- **Offline**: Funktioniert ohne Internet

### iOS Support
- **Home-Screen**: Installierbar wie native App
- **Full-Screen**: Ohne Browser-UI
- **Push-Benachrichtigungen**: Optional aktivierbar
- **Haptic-Feedback**: Native iOS-Feeling

### Responsive Design
- **Adaptive Layout**: Passt sich Bildschirmgröße an
- **Touch-Optimiert**: Große Buttons, Swipe-Gesten
- **Dark Mode**: Angenehm für die Augen
- **Accessibility**: Screen-Reader kompatibel

## Sicherheit & Datenschutz

### Offline-First
- **Keine Cloud**: Alle Daten bleiben lokal
- **Keine Telemetrie**: Kein Tracking
- **Keine externen APIs**: Außer für optional aktivierte Features

### Datenspeicherung
- **Verschlüsselte Verbindungen**: HTTPS optional
- **Token-Sicherheit**: Home Assistant Tokens sicher gespeichert
- **Session-Management**: Sichere Session-Verwaltung
- **Datenbank-Backups**: Regelmäßige Backups möglich

### Privacy
- **DSGVO-konform**: Keine Daten verlassen dein Netzwerk
- **Kamera-Daten**: Bleiben lokal
- **Chat-Historie**: Nur auf deinem Server
- **Keine Profilbildung**: Keine Datensammlung

## Performance

### GPU-Beschleunigung
- **NVIDIA CUDA**: Support für NVIDIA GPUs
- **Whisper-Beschleunigung**: Schnellere Transkription
- **Ollama-GPU**: Schnellere LLM-Inferenz
- **Docker GPU**: Native Container-Unterstützung

### Optimierungen
- **Redis-Caching**: Schnelle Datenzugriffe
- **Connection-Pooling**: Effiziente Datenbankverbindungen
- **Lazy-Loading**: Lädt nur benötigte Daten
- **Image-Optimization**: Komprimierte Bilder

### Skalierung
- **Horizontal**: Mehrere Backend-Instanzen möglich
- **Vertical**: Unterstützt große Server
- **Load-Balancing**: Optional mit Nginx
- **Microservices**: Modular erweiterbar

## Erweiterbarkeit

### Plugin-System
- **Custom Integrations**: Eigene Integrationen hinzufügen
- **Custom Task-Types**: Neue Task-Typen definieren
- **Custom Commands**: Eigene Befehle registrieren
- **Webhooks**: Events nach außen senden

### API
- **REST API**: Vollständige REST-Schnittstelle
- **WebSocket**: Für Echtzeit-Features
- **OpenAPI**: Automatische Dokumentation
- **Client-Libraries**: Einfache Integration

### Customization
- **Themes**: UI anpassbar
- **Languages**: Vollständige Mehrsprachigkeit (DE/EN)
- **Voices**: Verschiedene TTS-Stimmen
- **Models**: Austauschbare KI-Modelle

## Monitoring

### System-Health
- **Service-Status**: Alle Services überwachen
- **Resource-Usage**: CPU, RAM, Disk
- **Error-Rates**: Fehlerquoten tracken
- **Response-Times**: Performance messen

### Logging
- **Strukturierte Logs**: JSON-formatiert
- **Log-Levels**: Debug, Info, Warning, Error
- **Log-Rotation**: Automatische Bereinigung
- **Centralized**: Alle Logs an einem Ort

### Metrics
- **Prometheus**: Optional integrierbar
- **Grafana**: Dashboard-Visualisierung
- **Alerting**: Benachrichtigungen bei Problemen
- **Historical Data**: Langzeit-Statistiken

## Wartung

### Updates
- **Rolling Updates**: Keine Downtime
- **Automatic Migrations**: Datenbank-Updates automatisch
- **Backup vor Update**: Automatische Backups
- **Rollback**: Einfaches Zurücksetzen

### Backup & Restore
- **Datenbank**: PostgreSQL Dumps
- **Konfiguration**: .env Dateien
- **Models**: KI-Modelle sichern
- **Automatisch**: Geplante Backups

### Troubleshooting
- **Health-Checks**: System-Diagnose
- **Log-Analysis**: Fehlersuche
- **Debug-Mode**: Detaillierte Ausgaben
- **Support**: Community-Support

## UI/UX Features

### Benutzerfreundlichkeit
- **Intuitive Navigation**: Klare Menüstruktur
- **Keyboard-Shortcuts**: Schnelle Bedienung
- **Search**: Globale Suche
- **Notifications**: Toast-Benachrichtigungen

### Accessibility
- **Screen-Reader**: Volle Unterstützung
- **Keyboard-Navigation**: Ohne Maus bedienbar
- **High-Contrast**: Bessere Lesbarkeit
- **Font-Scaling**: Anpassbare Textgröße

### Responsive
- **Mobile-First**: Für Smartphones optimiert
- **Tablet-Optimized**: Nutzt größere Bildschirme
- **Desktop-Features**: Volle Features auf Desktop
- **Adaptive UI**: Passt sich an Gerät an

## Dark Mode

### Theme-Unterstützung
- **Drei Modi**: Hell, Dunkel und System (folgt OS-Präferenz)
- **Persistente Einstellung**: Theme wird in localStorage gespeichert
- **FOUC-Prevention**: Kein Flackern beim Laden durch Pre-Render-Script
- **Smooth Transitions**: Sanfte Übergänge zwischen den Modi

### Implementierung
- **Tailwind CSS**: Class-basiertes Dark Mode mit `dark:` Prefix
- **ThemeContext**: React Context für globale Theme-Verwaltung
- **ThemeToggle**: Dropdown-Menü im Header für Theme-Auswahl
- **System-Integration**: Reagiert auf OS Dark Mode Änderungen

### Gestaltung
- **Light Mode**: Heller Hintergrund (gray-50), dunkler Text (gray-900)
- **Dark Mode**: Dunkler Hintergrund (gray-900), heller Text (gray-100)
- **Konsistente Farben**: Alle Komponenten unterstützen beide Modi
- **Accessible**: Ausreichende Kontrastverhältnisse in beiden Modi

### Verwendung
1. Klicke auf das Sonne/Mond-Symbol im Header
2. Wähle zwischen "Hell", "Dunkel" oder "System"
3. Die Einstellung wird automatisch gespeichert

## Mehrsprachigkeit (i18n)

### Unterstützte Sprachen
- **Deutsch (de)**: Vollständig übersetzt (Standard)
- **Englisch (en)**: Vollständig übersetzt

### Frontend-Internationalisierung
- **react-i18next**: Bewährte i18n-Library für React
- **Automatische Erkennung**: Browsersprache wird erkannt
- **Persistente Speicherung**: Spracheinstellung in localStorage
- **Fallback**: Deutsch als Fallback-Sprache

### Sprachwechsel
- **Header-Dropdown**: Globus-Icon im Header
- **Sofortige Umschaltung**: Alle Texte wechseln sofort
- **Benutzer-Präferenz**: Einstellung wird pro Benutzer gespeichert
- **Datum/Zeit-Formatierung**: Lokalisierte Formatierung

### Übersetzte Bereiche
| Bereich | Übersetzungen |
|---------|---------------|
| Navigation | Menü, Sidebar, Breadcrumbs |
| Chat | Nachrichten, Placeholder, Aktionen |
| Dashboard | Widgets, Statistiken, Status |
| Einstellungen | Alle Formulare und Labels |
| Geräteverwaltung | Räume, Geräte, Capabilities |
| Benutzer & Rollen | CRUD-Dialoge, Berechtigungen |
| Fehler & Validierung | Alle Fehlermeldungen |

### Technische Details
```javascript
// Verwendung in Komponenten
import { useTranslation } from 'react-i18next';

function MyComponent() {
  const { t } = useTranslation();
  return <h1>{t('common.welcome')}</h1>;
}

// Mit Variablen
{t('users.deleteConfirm', { username: 'Max' })}
// → "Möchtest du 'Max' wirklich löschen?"
```

### Sprach-Dateien
```
src/frontend/src/i18n/
├── index.js           # i18next Konfiguration
└── locales/
    ├── de.json        # Deutsche Übersetzungen (~400 Keys)
    └── en.json        # Englische Übersetzungen (~400 Keys)
```

### Neue Übersetzungen hinzufügen
1. Key in beide JSON-Dateien einfügen
2. `t('namespace.key')` in der Komponente verwenden
3. Für Variablen: `t('key', { var: value })`

**Dokumentation:** Siehe `docs/MULTILANGUAGE.md` für vollständige Anleitung.

---

Diese Features machen Renfield zu einem leistungsstarken, sicheren und benutzerfreundlichen KI-Assistenten für dein Smart Home!
