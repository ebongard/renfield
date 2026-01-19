# Sprechererkennung (Speaker Recognition)

Renfield verfügt über ein integriertes Sprechererkennungssystem basierend auf **SpeechBrain ECAPA-TDNN**, das Sprecher automatisch identifiziert und Profile anlegt.

## Features

- **Automatische Sprechererkennung** bei jeder Spracheingabe (Web & Satellite)
- **Auto-Discovery** unbekannter Sprecher mit automatischem Profil-Anlegen
- **Continuous Learning** - Verbesserte Erkennung durch jede Interaktion
- **Multi-Speaker Support** - Unbegrenzte Anzahl von Sprecherprofilen
- **Frontend-Management** - Vollständige Verwaltung über Web-Interface

## Architektur

```
┌─────────────────────────────────────────────────────────────┐
│                   Audio Input                                │
│     (Web Frontend / Satellite / Voice Chat)                  │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              Whisper Service (STT)                           │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  1. Audio Preprocessing (Noise Reduction, Normalize)   │ │
│  │  2. Transcription (Whisper)                            │ │
│  │  3. Speaker Embedding Extraction (SpeechBrain)         │ │
│  │  4. Speaker Identification / Auto-Enrollment           │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                  PostgreSQL Database                         │
│  ┌─────────────────┐    ┌────────────────────────────────┐  │
│  │    speakers     │───▶│      speaker_embeddings        │  │
│  │  - id           │    │  - id                          │  │
│  │  - name         │    │  - speaker_id (FK)             │  │
│  │  - alias        │    │  - embedding (Base64 192-dim)  │  │
│  │  - is_admin     │    │  - created_at                  │  │
│  └─────────────────┘    └────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Funktionsweise

### 1. Sprecheridentifikation

Bei jeder Spracheingabe wird:
1. Ein 192-dimensionales **Voice Embedding** aus dem Audio extrahiert
2. Das Embedding mit allen bekannten Sprechern verglichen (Cosine Similarity)
3. Der beste Match über dem Threshold zurückgegeben

### 2. Auto-Discovery (Auto-Enrollment)

Wenn kein Sprecher erkannt wird und `SPEAKER_AUTO_ENROLL=true`:
1. Neues Profil "Unbekannter Sprecher #N" wird angelegt
2. Voice Embedding wird gespeichert
3. Bei nächster Interaktion wird der Sprecher wiedererkannt

### 3. Continuous Learning

Wenn `SPEAKER_CONTINUOUS_LEARNING=true`:
1. Bei jeder Interaktion eines erkannten Sprechers
2. Wird das aktuelle Embedding zum Profil hinzugefügt
3. Maximum 10 Embeddings pro Sprecher (verhindert unbegrenztes Wachstum)
4. Durchschnitt aller Embeddings verbessert die Erkennung

## Konfiguration

### Umgebungsvariablen (.env)

```bash
# Sprechererkennung aktivieren/deaktivieren
SPEAKER_RECOGNITION_ENABLED=true

# Minimum Similarity für positive Identifikation (0.0-1.0)
# Niedriger = mehr false positives, Höher = mehr false negatives
SPEAKER_RECOGNITION_THRESHOLD=0.25

# Inferenz-Gerät (cpu oder cuda)
SPEAKER_RECOGNITION_DEVICE=cpu

# Automatisches Anlegen unbekannter Sprecher
SPEAKER_AUTO_ENROLL=true

# Embeddings bei jeder Interaktion hinzufügen
SPEAKER_CONTINUOUS_LEARNING=true
```

### Threshold-Empfehlungen

| Szenario | Threshold | Beschreibung |
|----------|-----------|--------------|
| Hohe Sicherheit | 0.35-0.45 | Weniger false positives, mehr "unbekannt" |
| **Standard** | **0.25** | Gute Balance zwischen Sicherheit und Komfort |
| Komfort | 0.15-0.20 | Mehr Erkennung, aber mehr false positives |

## API-Endpunkte

### Speaker Management

| Methode | Endpoint | Beschreibung |
|---------|----------|--------------|
| `GET` | `/api/speakers/status` | Service-Status prüfen |
| `GET` | `/api/speakers` | Alle Sprecher auflisten |
| `POST` | `/api/speakers` | Neuen Sprecher anlegen |
| `GET` | `/api/speakers/{id}` | Sprecher-Details abrufen |
| `PATCH` | `/api/speakers/{id}` | Sprecher aktualisieren |
| `DELETE` | `/api/speakers/{id}` | Sprecher löschen |

### Voice Enrollment & Identification

| Methode | Endpoint | Beschreibung |
|---------|----------|--------------|
| `POST` | `/api/speakers/{id}/enroll` | Voice Sample hinzufügen |
| `POST` | `/api/speakers/identify` | Sprecher aus Audio identifizieren |
| `POST` | `/api/speakers/{id}/verify` | Sprecher verifizieren |

### Beispiel: Neuen Sprecher anlegen

```bash
curl -X POST http://localhost:8000/api/speakers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Max Mustermann",
    "alias": "max",
    "is_admin": false
  }'
```

### Beispiel: Voice Sample hochladen

```bash
curl -X POST http://localhost:8000/api/speakers/1/enroll \
  -F "audio=@voice_sample.wav"
```

### Beispiel: Sprecher identifizieren

```bash
curl -X POST http://localhost:8000/api/speakers/identify \
  -F "audio=@unknown_voice.wav"
```

**Response:**
```json
{
  "is_identified": true,
  "speaker_id": 1,
  "speaker_name": "Max Mustermann",
  "speaker_alias": "max",
  "confidence": 0.847
}
```

## Frontend-Verwaltung

Das Speaker Management ist über das Web-Interface unter `/speakers` erreichbar:

### Funktionen

1. **Service-Status** - Zeigt ob SpeechBrain verfügbar ist
2. **Sprecher-Liste** - Alle registrierten Sprecher mit Embedding-Anzahl
3. **Neuer Sprecher** - Manuell Sprecher anlegen
4. **Voice Sample aufnehmen** - Live-Aufnahme mit Wellenform-Visualisierung
5. **Sprecher identifizieren** - Wer spricht gerade?
6. **Sprecher löschen** - Profile entfernen

### Workflow: Unbekannte Sprecher umbenennen

1. Neuer Benutzer spricht → "Unbekannter Sprecher #1" wird automatisch angelegt
2. Im Frontend unter `/speakers` den Sprecher auswählen
3. Namen und Alias ändern (z.B. "Max Mustermann", "max")
4. Ab sofort wird der Benutzer korrekt identifiziert

## Logs

### Erfolgreiche Erkennung
```
🎤 Speaker identified: Max Mustermann (0.85)
📊 Added embedding to speaker 1 (now 5 total)
```

### Neuer unbekannter Sprecher
```
🆕 New unknown speaker created: Unbekannter Sprecher #1 (ID: 3)
```

### Satellite-Erkennung
```
🎤 Satellite Sprecher erkannt: Max Mustermann (@max) - Konfidenz: 0.78
```

## Performance

| Metrik | Wert |
|--------|------|
| Model | ECAPA-TDNN (SpeechBrain) |
| Embedding-Dimension | 192 |
| EER (Equal Error Rate) | 0.8-1.7% |
| Inferenz-Zeit (CPU) | ~200-500ms |
| Modell-Größe | ~90MB |
| Min. Audio-Länge | 0.5 Sekunden |
| Empfohlene Audio-Länge | 3-10 Sekunden |

## Technische Details

### SpeechBrain ECAPA-TDNN

- **Modell**: `speechbrain/spkrec-ecapa-voxceleb`
- **Training**: VoxCeleb1 + VoxCeleb2 Datasets
- **Architektur**: Emphasized Channel Attention, Propagation and Aggregation
- **Output**: 192-dimensionales Embedding pro Audio-Sample

### Embedding-Vergleich

```python
# Cosine Similarity zwischen zwei Embeddings
similarity = np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))

# Identifikation: Höchste Similarity über Threshold
if similarity > SPEAKER_RECOGNITION_THRESHOLD:
    return speaker_id, speaker_name, similarity
```

### Datenbank-Schema

```sql
-- Sprecher
CREATE TABLE speakers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    alias VARCHAR(50) UNIQUE,
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Voice Embeddings
CREATE TABLE speaker_embeddings (
    id SERIAL PRIMARY KEY,
    speaker_id INTEGER REFERENCES speakers(id) ON DELETE CASCADE,
    embedding TEXT NOT NULL,  -- Base64-encoded numpy array
    created_at TIMESTAMP DEFAULT NOW()
);
```

## Known Issues & Technical Debt

### SpeechBrain/torchaudio Inkompatibilität

In neueren torchaudio-Versionen (2.1+) wurden bestimmte Backend-APIs entfernt.
**Workaround**: Monkey-Patch in `speaker_service.py`

Siehe: [BUGFIXES.md](BUGFIXES.md#-problem-6-speechbraintorchaudio-inkompatibilität-technical-debt)

### huggingface_hub Inkompatibilität

SpeechBrain verwendet veraltete `use_auth_token` Parameter.
**Workaround**: Version-Pin `huggingface_hub<0.24.0`

Siehe: [BUGFIXES.md](BUGFIXES.md#-problem-7-speechbrainhuggingface_hub-inkompatibilität-technical-debt)

## Troubleshooting

### "Speaker recognition not available"

1. Prüfe ob SpeechBrain installiert ist:
   ```bash
   docker exec renfield-backend pip list | grep speechbrain
   ```

2. Prüfe die Logs:
   ```bash
   docker compose logs backend | grep -i speaker
   ```

3. Stelle sicher, dass genug Speicherplatz vorhanden ist (für Modell-Download):
   ```bash
   docker system df
   ```

### Sprecher werden nicht erkannt

1. Prüfe den Threshold:
   ```bash
   # In .env
   SPEAKER_RECOGNITION_THRESHOLD=0.20  # Niedriger = mehr Erkennung
   ```

2. Prüfe die Embedding-Anzahl:
   - Minimum 1 Embedding nötig
   - Empfohlen: 3-5 verschiedene Voice Samples

3. Prüfe die Audio-Qualität:
   - Mindestens 0.5 Sekunden
   - Deutliche Sprache
   - Wenig Hintergrundgeräusche

### Modell lädt nicht

```bash
# Cache leeren und neu starten
docker compose down backend
docker volume rm renfield_huggingface_cache
docker compose up -d backend
```

## Datenschutz

- Alle Voice Embeddings werden **lokal** in PostgreSQL gespeichert
- Keine Cloud-Verbindung für Sprechererkennung
- Embeddings können nicht zurück zu Audio konvertiert werden
- Sprecher können jederzeit gelöscht werden (inkl. aller Embeddings)
