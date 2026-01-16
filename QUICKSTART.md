# Renfield - Schnellstart Checkliste ✅

## Vor dem Start

- [ ] Docker installiert und läuft
- [ ] Docker Compose verfügbar
- [ ] Mindestens 16 GB RAM frei
- [ ] 50 GB Speicherplatz frei

## Setup (10-15 Minuten)

### 1. Projekt vorbereiten
```bash
cd renfield
cp .env.example .env
```

### 2. .env konfigurieren
Öffne `.env` und passe an:

**Pflichtfelder:**
```env
POSTGRES_PASSWORD=dein_sicheres_passwort  # ⚠️ ÄNDERN!
```

**Optional (aber empfohlen):**
```env
# Home Assistant
HOME_ASSISTANT_URL=http://192.168.1.100:8123
HOME_ASSISTANT_TOKEN=eyJ0eXAi...  # Von HA holen

# n8n (falls vorhanden)
N8N_WEBHOOK_URL=http://192.168.1.100:5678/webhook

# Frigate (falls vorhanden)
FRIGATE_URL=http://192.168.1.100:5000
```

### 3. System starten
```bash
chmod +x start.sh
./start.sh
```

Das Script:
- ✅ Startet alle Docker Container
- ✅ Lädt Ollama Modell
- ✅ Prüft alle Services
- ⏱️ Dauert 5-10 Minuten beim ersten Start

### 4. Im Browser öffnen
```
http://localhost:3000
```

## Erste Tests

### Test 1: Chat (1 Minute)
1. Gehe zu **Chat**
2. Schreibe: "Hallo, wer bist du?"
3. ✅ Sollte antworten

### Test 2: Sprache (2 Minuten)
1. Im Chat auf 🎤 klicken
2. Sage: "Was kannst du alles?"
3. ✅ Sollte transkribieren und antworten
4. Klicke 🔊 bei Antwort
5. ✅ Sollte vorlesen

### Test 3: Home Assistant (2 Minuten)
1. Gehe zu **Smart Home**
2. ✅ Sollte deine Geräte zeigen
3. Klicke ein Licht an
4. ✅ Sollte ein/ausschalten

### Test 4: Kameras (1 Minute)
1. Gehe zu **Kameras**
2. ✅ Sollte Events zeigen (falls Frigate läuft)

## Troubleshooting

### Container startet nicht
```bash
docker-compose logs renfield-backend
# Logs prüfen, dann:
docker-compose restart
```

### Ollama Modell fehlt
```bash
docker exec -it renfield-ollama ollama pull llama3.2:3b
docker-compose restart backend
```

### Frontend nicht erreichbar
```bash
docker-compose logs renfield-frontend
# Prüfe ob Port 3000 frei ist
```

### Home Assistant verbindet nicht
1. Prüfe URL in .env
2. Erstelle neuen Token in HA:
   - Profil → Lange Zugangstoken → Token erstellen
3. Kopiere in .env
4. `docker-compose restart backend`

## Nächste Schritte

✅ **System läuft?** Großartig!

### Jetzt kannst du:

1. **Sprache nutzen**
   - Mikrofon-Button im Chat
   - Sage Befehle wie "Schalte Licht an"

2. **Smart Home steuern**
   - Gehe zu Smart Home
   - Klicke Geräte an/aus
   - Oder sage im Chat: "Schalte X ein"

3. **Kameras überwachen**
   - Wenn Frigate läuft: Gehe zu Kameras
   - Sieh Events von heute

4. **Workflows triggern**
   - Wenn n8n läuft: "Starte Backup"
   - Konfiguriere Workflows in n8n

### Erweiterte Features

- **iOS App**: Safari → Teilen → Zum Home-Bildschirm
- **HTTPS**: Siehe INSTALLATION.md
- **Backup**: `docker exec renfield-postgres pg_dump ...`

## Häufige Fragen

**Q: Wie lange dauert der erste Start?**
A: 5-10 Minuten (Ollama Modell Download)

**Q: Brauche ich Home Assistant?**
A: Nein, Chat funktioniert auch ohne

**Q: Funktioniert es ohne Internet?**
A: Ja, vollständig offline (außer initial Download)

**Q: Kann ich andere LLM Modelle nutzen?**
A: Ja, in .env OLLAMA_MODEL ändern

**Q: iOS App verfügbar?**
A: Ja, als PWA installierbar

## Support

**Logs anzeigen:**
```bash
docker-compose logs -f
```

**System neu starten:**
```bash
docker-compose restart
```

**System stoppen:**
```bash
docker-compose down
```

**Alles zurücksetzen:**
```bash
docker-compose down -v
# Achtung: Löscht alle Daten!
```

## Fertig! 🎉

Dein Renfield-Assistent ist bereit!

Öffne: **http://localhost:3000**

---

Weitere Infos:
- 📖 README.md - Übersicht
- 🚀 INSTALLATION.md - Detaillierte Anleitung
- ✨ FEATURES.md - Alle Features
- 📁 PROJECT_OVERVIEW.md - Projektstruktur
