# Renfield - Feature Dokumentation

## 🎯 Übersicht

Renfield ist ein vollständig offline-fähiger KI-Assistent, der speziell für Smart Home und Hausautomatisierung entwickelt wurde.

## 💬 Chat & Konversation

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

## 🎤 Sprach-Interface

### Speech-to-Text (STT)
- **Whisper Integration**: OpenAI's Whisper für hochwertige Transkription
- **Offline-Verarbeitung**: Keine Cloud-Dienste nötig
- **Deutsche Sprache**: Optimiert für deutsche Spracheingabe
- **Modell-Auswahl**: Wählbar zwischen tiny, base, small, medium, large
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

## 🏠 Home Assistant Integration

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
- **Gruppenstuerung**: Mehrere Geräte gleichzeitig

## 📹 Kamera-Überwachung

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

## 🔄 n8n Workflow Integration

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

## 📋 Task Management

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

## 🧠 KI-Features

### Ollama LLM
- **Lokale Verarbeitung**: Kein Internet nötig
- **Modell-Auswahl**: Verschiedene Größen verfügbar
- **GPU-Beschleunigung**: Optional für bessere Performance
- **Kontext-Fenster**: Großer Kontext für komplexe Anfragen

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

## 📱 Progressive Web App

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

## 🔒 Sicherheit & Datenschutz

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

## 🚀 Performance

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

## 🔧 Erweiterbarkeit

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
- **Languages**: Mehrsprachigkeit vorbereitet
- **Voices**: Verschiedene TTS-Stimmen
- **Models**: Austauschbare KI-Modelle

## 📊 Monitoring

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

## 🛠️ Wartung

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

## 🎨 UI/UX Features

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

---

Diese Features machen Renfield zu einem leistungsstarken, sicheren und benutzerfreundlichen KI-Assistenten für dein Smart Home!
