# Multi-Language Support (i18n)

Renfield unterstützt vollständige Mehrsprachigkeit im Frontend mit Deutsch und Englisch als verfügbare Sprachen.

---

## Übersicht

| Komponente | Technologie | Sprachen |
|------------|-------------|----------|
| **Frontend** | react-i18next | Deutsch, Englisch |
| **STT (Whisper)** | Per-Request Parameter | Alle Whisper-Sprachen |
| **TTS (Piper)** | Multi-Voice Config | DE, EN (konfigurierbar) |
| **Satellite** | Config-basiert | DE, EN |

---

## Frontend Internationalisierung

### Technologie-Stack

- **i18next**: Industrie-Standard i18n Framework
- **react-i18next**: React Integration mit Hooks
- **i18next-browser-languagedetector**: Automatische Spracherkennung

### Konfiguration

Die i18n-Konfiguration befindet sich in `src/frontend/src/i18n/index.js`:

```javascript
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      de: { translation: de },
      en: { translation: en }
    },
    fallbackLng: 'de',
    supportedLngs: ['de', 'en'],
    detection: {
      order: ['localStorage', 'navigator'],
      caches: ['localStorage'],
      lookupLocalStorage: 'renfield_language'
    }
  });
```

### Spracherkennung

Die Sprache wird in folgender Reihenfolge ermittelt:

1. **localStorage** (`renfield_language`)
2. **Browser-Sprache** (`navigator.language`)
3. **Fallback**: Deutsch (`de`)

### Sprachwechsel

Der `LanguageSwitcher` im Header ermöglicht den sofortigen Sprachwechsel:

```jsx
import { useTranslation } from 'react-i18next';

function LanguageSwitcher() {
  const { i18n } = useTranslation();

  const changeLanguage = (code) => {
    i18n.changeLanguage(code);
  };

  return (
    <button onClick={() => changeLanguage('en')}>English</button>
  );
}
```

---

## Übersetzungsdateien

### Dateistruktur

```
src/frontend/src/i18n/
├── index.js              # Konfiguration
└── locales/
    ├── de.json           # Deutsche Übersetzungen (~400 Keys)
    └── en.json           # Englische Übersetzungen (~400 Keys)
```

### Namespace-Struktur

Die Übersetzungen sind nach Namespaces organisiert:

```json
{
  "common": {
    "save": "Speichern",
    "cancel": "Abbrechen",
    "delete": "Löschen",
    "edit": "Bearbeiten",
    "loading": "Laden...",
    "error": "Fehler",
    "success": "Erfolg"
  },
  "nav": {
    "chat": "Chat",
    "knowledge": "Wissen",
    "tasks": "Aufgaben",
    "rooms": "Räume",
    "settings": "Einstellungen"
  },
  "chat": {
    "placeholder": "Nachricht eingeben...",
    "send": "Senden",
    "newChat": "Neuer Chat"
  }
}
```

### Verfügbare Namespaces

| Namespace | Beschreibung |
|-----------|--------------|
| `common` | Gemeinsame Buttons, Labels, Zustände |
| `nav` | Navigation und Menü |
| `chat` | Chat-Interface |
| `knowledge` | Wissensbasis |
| `rooms` | Raumverwaltung |
| `devices` | Geräteverwaltung |
| `speakers` | Sprechererkennung |
| `users` | Benutzerverwaltung |
| `roles` | Rollenverwaltung |
| `plugins` | Plugin-System |
| `settings` | Einstellungen |
| `auth` | Authentifizierung |
| `camera` | Kameraintegration |
| `tasks` | Aufgabenverwaltung |
| `home` | Dashboard |

---

## Verwendung in Komponenten

### Basis-Verwendung

```jsx
import { useTranslation } from 'react-i18next';

function MyComponent() {
  const { t } = useTranslation();

  return (
    <div>
      <h1>{t('common.welcome')}</h1>
      <button>{t('common.save')}</button>
    </div>
  );
}
```

### Interpolation (Variablen)

```jsx
// JSON:
// "deleteConfirm": "Möchtest du \"{{name}}\" wirklich löschen?"

const { t } = useTranslation();
t('users.deleteConfirm', { name: 'Max' });
// → "Möchtest du 'Max' wirklich löschen?"
```

### Pluralisierung

```jsx
// JSON:
// "itemCount": "{{count}} Element",
// "itemCount_plural": "{{count}} Elemente"

t('common.itemCount', { count: 5 });
// → "5 Elemente"
```

### Lokalisierte Formatierung

```jsx
const { i18n } = useTranslation();

// Datum formatieren
new Date().toLocaleDateString(i18n.language);
// DE: "24.01.2026"
// EN: "1/24/2026"

// Datum mit Uhrzeit
new Date().toLocaleString(i18n.language);
// DE: "24.01.2026, 14:30:45"
// EN: "1/24/2026, 2:30:45 PM"
```

---

## Neue Übersetzungen hinzufügen

### Schritt 1: Keys in beiden Dateien einfügen

**de.json:**
```json
{
  "myFeature": {
    "title": "Meine neue Funktion",
    "description": "Beschreibung auf Deutsch"
  }
}
```

**en.json:**
```json
{
  "myFeature": {
    "title": "My New Feature",
    "description": "Description in English"
  }
}
```

### Schritt 2: In Komponente verwenden

```jsx
import { useTranslation } from 'react-i18next';

function MyFeature() {
  const { t } = useTranslation();

  return (
    <div>
      <h1>{t('myFeature.title')}</h1>
      <p>{t('myFeature.description')}</p>
    </div>
  );
}
```

---

## Backend-Sprachunterstützung

### Volltextsuche im Second Brain (Postgres FTS)

Renfield-Haushalte sprechen oft mehrere Sprachen — eine Notiz auf Französisch, eine Konversation auf Deutsch, ein Dokument auf Englisch. Damit die Lexikalsuche im `/brain`-Pfad (`services/lexical_retrieval.py`) sprachübergreifend funktioniert, werden Postgres-FTS-Stammformen-Stemmer aller unterstützten Sprachen parallel angewendet.

**Unterstützte Sprachen** (single source of truth in `services/fts_languages.FTS_LANGUAGES`):

| Code Config | Sprache |
|---|---|
| `german` | Deutsch |
| `english` | Englisch |
| `french` | Französisch |
| `italian` | Italienisch |
| `spanish` | Spanisch |
| `dutch` | Niederländisch |

Alle sechs Configs sind in jeder Standard-Postgres-Installation enthalten — keine Zusatzpakete nötig.

**Funktionsweise:**

Zwei `GENERATED STORED`-Spalten — `conversation_memories.search_vector` (Migration `pc20260528`) und `document_chunks.search_vector` (Migration `pc20260529`) — berechnen bei jedem Insert/Update ihren Wert serverseitig als Union aller sechs `to_tsvector`-Aufrufe:

```sql
GENERATED ALWAYS AS (
  to_tsvector('german',  coalesce(content, '')) ||
  to_tsvector('english', coalesce(content, '')) ||
  to_tsvector('french',  coalesce(content, '')) ||
  to_tsvector('italian', coalesce(content, '')) ||
  to_tsvector('spanish', coalesce(content, '')) ||
  to_tsvector('dutch',   coalesce(content, ''))
) STORED
```

Die Query-Seite unioniert analog `websearch_to_tsquery` über dieselbe Menge. Folge: ein französisches Memory („Pierre aime le café") matcht eine deutsche Anfrage und umgekehrt, weil mindestens ein Stemmer-Paar identische Stämme produziert.

**Eine 7. Sprache hinzufügen:**

1. `services/fts_languages.FTS_LANGUAGES`-Tuple um die neue Config erweitern.
2. ZWEI Folge-Migrationen schreiben, die je die GENERATED-Spalte droppen und mit dem neuen Ausdruck neu anlegen (Postgres erlaubt kein `ALTER` auf einer GENERATED-Spalten-Expression):
   - `conversation_memories.search_vector` — Vorlage: `pc20260528` (DROP+ADD-Pattern, ok für kleine Korpora)
   - `document_chunks.search_vector` — Vorlage: `pc20260529` (atomic-swap-Pattern, minimiert das Schreib-Lock auf grossen Korpora)
3. GIN-Indexe `idx_conversation_memories_search_vector_gin` und `idx_document_chunks_search_vector_gin` werden von den Vorlagen-Migrationen automatisch mit CONCURRENTLY neu aufgebaut.

Beide Spalten sind ab pc20260529 auto-multilingual — `RAG_HYBRID_FTS_CONFIG` wird nicht mehr im Query-Pfad konsultiert und ist nur noch deklarativ (Startup-Warnung bei Werten ausserhalb `FTS_LANGUAGES`).

### Speech-to-Text (Whisper)

Whisper unterstützt über 90 Sprachen. Die Sprache kann pro Request angegeben werden:

```python
# API-Aufruf mit Sprache
POST /api/voice/stt?language=en

# WebSocket (Satellite)
{
  "type": "audio",
  "language": "en",
  "chunk": "<base64 audio>"
}
```

### Text-to-Speech (Piper)

Piper-Stimmen sind sprachspezifisch. Multi-Voice-Konfiguration:

```bash
# .env
PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium
```

Verfügbare deutsche Stimmen:
- `de_DE-thorsten-high` - Männlich, hohe Qualität
- `de_DE-eva_k-medium` - Weiblich, mittlere Qualität

Verfügbare englische Stimmen:
- `en_US-amy-medium` - US Englisch, weiblich
- `en_GB-cori-medium` - UK Englisch, weiblich

### Satellite-Konfiguration

Satellites können eine Sprache in ihrer Konfiguration angeben:

```yaml
# config/satellite.yaml
satellite:
  id: "sat-livingroom"
  room: "Living Room"
  language: "de"  # oder "en"
```

---

## Benutzer-Präferenzen

### Frontend-Speicherung

Die gewählte Sprache wird in `localStorage` gespeichert:

```javascript
localStorage.getItem('renfield_language');
// → "de" oder "en"
```

### Backend-Präferenz (Optional)

Bei aktivierter Authentifizierung kann die Sprachpräferenz auch im Benutzerprofil gespeichert werden:

```sql
-- User-Tabelle hat preferred_language Spalte
ALTER TABLE users ADD COLUMN preferred_language VARCHAR(10) DEFAULT 'de';
```

```python
# API Endpoint
GET /api/preferences/language
PUT /api/preferences/language {"language": "en"}
```

---

## Best Practices

### 1. Konsistente Key-Benennung

```json
// Gut
{
  "users": {
    "createUser": "Benutzer erstellen",
    "deleteUser": "Benutzer löschen"
  }
}

// Vermeiden
{
  "create-user": "...",
  "DELETE_USER": "..."
}
```

### 2. Kontext in Keys

```json
// Gut - Kontext im Key
{
  "button": {
    "save": "Speichern",
    "cancel": "Abbrechen"
  },
  "dialog": {
    "save": "Änderungen speichern",
    "cancel": "Vorgang abbrechen"
  }
}
```

### 3. Keine HTML in Übersetzungen

```jsx
// Vermeiden
t('welcome', { interpolation: { escapeValue: false } })

// Besser
<Trans i18nKey="welcome">
  Willkommen <strong>{{name}}</strong>!
</Trans>
```

### 4. Vollständigkeit prüfen

Beide Sprachdateien sollten die gleichen Keys haben:

```bash
# Keys vergleichen (Node.js)
node -e "
  const de = require('./de.json');
  const en = require('./en.json');
  const deKeys = Object.keys(de).sort();
  const enKeys = Object.keys(en).sort();
  console.log('Missing in EN:', deKeys.filter(k => !enKeys.includes(k)));
  console.log('Missing in DE:', enKeys.filter(k => !deKeys.includes(k)));
"
```

---

## Troubleshooting

### Übersetzung fehlt

**Problem:** `t('some.key')` zeigt nur den Key an

**Lösung:**
1. Key in beiden JSON-Dateien prüfen
2. Syntax der JSON-Datei prüfen (gültiges JSON?)
3. Browser-Cache leeren (Hard Reload: Cmd+Shift+R)

### Sprache wechselt nicht

**Problem:** Sprachwechsel hat keinen Effekt

**Lösung:**
1. `localStorage` prüfen: `localStorage.getItem('renfield_language')`
2. Browser-Konsole auf Fehler prüfen
3. i18n Import in `main.jsx` prüfen

### Interpolation funktioniert nicht

**Problem:** `{{name}}` wird nicht ersetzt

**Lösung:**
```jsx
// Falsch
t('greeting', 'Max')

// Richtig
t('greeting', { name: 'Max' })
```

---

## Weitere Sprachen hinzufügen

### Schritt 1: Übersetzungsdatei erstellen

```bash
cp src/frontend/src/i18n/locales/en.json src/frontend/src/i18n/locales/fr.json
# Alle Werte in fr.json übersetzen
```

### Schritt 2: In i18n-Config registrieren

```javascript
// src/frontend/src/i18n/index.js
import fr from './locales/fr.json';

i18n.init({
  resources: {
    de: { translation: de },
    en: { translation: en },
    fr: { translation: fr }  // NEU
  },
  supportedLngs: ['de', 'en', 'fr']  // NEU
});
```

### Schritt 3: LanguageSwitcher erweitern

```jsx
// src/frontend/src/components/LanguageSwitcher.jsx
const languages = [
  { code: 'de', name: 'Deutsch', flag: '🇩🇪' },
  { code: 'en', name: 'English', flag: '🇬🇧' },
  { code: 'fr', name: 'Français', flag: '🇫🇷' }  // NEU
];
```

### Schritt 4: Piper-Stimme hinzufügen (optional)

```bash
# .env
PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium,fr:fr_FR-gilles-low
```

---

## Referenzen

- [i18next Dokumentation](https://www.i18next.com/)
- [react-i18next Dokumentation](https://react.i18next.com/)
- [Piper TTS Voices](https://github.com/rhasspy/piper/blob/master/VOICES.md)
- [Whisper Languages](https://github.com/openai/whisper#available-models-and-languages)
