# Technical Debt - Renfield System

Dieses Dokument enthält eine umfassende Analyse der technischen Schulden im gesamten Renfield-System.

**Letzte Aktualisierung:** 2026-01-25

---

## Übersicht

| Bereich | Kritisch | Mittel | Niedrig | Gesamt | Behoben |
|---------|----------|--------|---------|--------|---------|
| Backend | 0 | 1 | 4 | 7 | 6 |
| Frontend | 0 | 4 | 3 | 7 | 1 |
| Satellite | 0 | 3 | 2 | 5 | 0 |
| Infrastruktur | 0 | 3 | 2 | 5 | 1 |
| **Gesamt** | **0** | **11** | **11** | **24** | **8** |

---

## Backend

### ~~🔴 Kritisch~~ → ✅ Behoben

#### ~~1. God Class: main.py (2130 → 337 Zeilen)~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Ursprüngliches Problem:** Die Datei `src/backend/main.py` enthielt zu viele Verantwortlichkeiten:
- FastAPI App-Konfiguration
- WebSocket-Handler (Chat, Device, Satellite)
- Lifecycle-Management
- Streaming-Logik

**Lösung:**
- ✅ Phase 1: Shared Utilities extrahiert
  - `api/websocket/shared.py` erstellt
  - `ConversationSessionState`, `RAGSessionState`, Helpers ausgelagert
- ✅ Phase 2: WebSocket-Handler extrahiert
  - `api/websocket/chat_handler.py` (~370 Zeilen)
  - `api/websocket/satellite_handler.py` (~550 Zeilen)
  - `api/websocket/device_handler.py` (~530 Zeilen)
- ✅ Phase 3: Alte Handler entfernt

**Ergebnis:**
- main.py: 2130 → 337 Zeilen (**84% Reduktion**)
- Alle 558 Tests bestanden

**Neue Struktur:**
```
api/
├── lifecycle.py         ✅ Startup/Shutdown management
├── websocket/
│   ├── __init__.py      ✅ Router exports
│   ├── shared.py        ✅ Shared utilities
│   ├── chat_handler.py  ✅ /ws endpoint
│   ├── device_handler.py ✅ /ws/device endpoint
│   └── satellite_handler.py ✅ /ws/satellite endpoint
└── routes/
    └── ... (unchanged)
```

---

#### 2. ~~Bare Except Clauses (6 Stellen)~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Änderungen:**
- `main.py:1945` → `except Exception:`
- `output_routing_service.py:305` → `except Exception:`
- `device_manager.py:211, 573` → `except Exception:`
- `satellite_manager.py:164, 481` → `except Exception:`

---

### 🟡 Mittel

#### 3. Große API-Route-Dateien (teilweise behoben)

| Datei | Vorher | Nachher | Status |
|-------|--------|---------|--------|
| `routes/rooms.py` | 1024 | 866 | ✅ Schemas extrahiert |
| `routes/knowledge.py` | 1019 | 924 | ✅ Schemas extrahiert |
| `routes/speakers.py` | 650 | 650 | OK, beobachten |

**Änderungen (2026-01-25):**
- `rooms_schemas.py` (182 Zeilen) - Pydantic Models extrahiert
- `knowledge_schemas.py` (117 Zeilen) - Pydantic Models extrahiert

---

#### ~~4. Hardcoded Fallback-Werte~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Ursprüngliches Problem:** Fallback auf `localhost` funktioniert nicht in Container-Umgebungen.

**Lösung:** Neues Config-Setting `BACKEND_INTERNAL_URL` (Default: `http://backend:8000`) als Fallback statt localhost.

---

#### ~~5. Print Statements in CLI-Tools~~ ✅ Dokumentiert

**Status:** Dokumentiert am 2026-01-25

**Lösung:** CLI-Test-Tools (`test_plugins.py`, `test_url_encoding.py`, `test_error_handling.py`) sind jetzt als interaktive CLI-Tools dokumentiert, wo `print()` für Ausgabe angemessen ist.

---

#### ~~6. Fehlende Type Hints~~ ✅ Verbessert

**Status:** Verbessert am 2026-01-25

**Änderungen:**
- `ollama_service.py`: `ensure_model_loaded() -> None`, `_build_plugin_context() -> str`
- `audio_output_service.py`: `_ensure_cache_dir() -> None`, `_cleanup_old_cache_files() -> None`
- TYPE_CHECKING Imports für PluginRegistry und Message hinzugefügt

**Empfehlung:** Weitere Type Hints schrittweise hinzufügen, mit `mypy` prüfen.

---

#### ~~7. Ollama Service Größe (966 → 773 Zeilen)~~ ✅ Teilweise behoben

**Status:** Teilweise behoben am 2026-01-25

**Änderungen:**
- `services/conversation_service.py` erstellt (~300 Zeilen)
- Conversation-Methoden aus OllamaService extrahiert
- OllamaService delegiert jetzt an ConversationService (Rückwärtskompatibilität)
- Reduktion: 966 → 773 Zeilen (**20% Reduktion**)

**Neue Struktur:**
```
services/
├── ollama_service.py       (773 Zeilen) - LLM, Intent, RAG
├── conversation_service.py (300 Zeilen) - Conversation Persistence (NEU)
└── rag_service.py          - Document Management (bestehend)
```

**Verbleibend:** Intent-Extraction könnte noch separiert werden.

---

### 🟢 Niedrig

#### 8. Alembic Migrations ohne Downgrade

Einige Migrations haben leere `downgrade()` Funktionen.

#### 9. Nicht genutzte Imports

Vereinzelte ungenutzte Imports in verschiedenen Dateien.

#### 10. Docstrings fehlen teilweise

Einige Service-Methoden haben keine Docstrings.

#### 11. Magic Numbers

Einige hartcodierte Zahlen (Timeouts, Limits) sollten in Config.

---

## Frontend

### ~~🔴 Kritisch~~ → ✅ Behoben

#### ~~1. ChatPage.jsx (1295 → 555 Zeilen)~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Ursprüngliches Problem:** Monolithische Komponente mit zu vielen Verantwortlichkeiten:
- WebSocket-Verbindung
- Audio Recording
- Message Rendering
- Session Management

**Lösung:** Aufgeteilt in modulare Struktur:
```
pages/ChatPage/
├── index.jsx              (555 Zeilen) - Haupt-Orchestrator
├── ChatMessages.jsx       (101 Zeilen) - Nachrichtenanzeige
├── ChatInput.jsx          (191 Zeilen) - Eingabebereich + RAG
├── ChatHeader.jsx         (174 Zeilen) - Wake Word Controls
├── AudioVisualizer.jsx    (74 Zeilen)  - Wellenform-Anzeige
└── hooks/
    ├── index.js           (2 Zeilen)   - Exports
    ├── useChatWebSocket.js (114 Zeilen) - WebSocket-Logik
    └── useAudioRecording.js (370 Zeilen) - Audio + VAD
```

**Ergebnis:**
- Haupt-Datei: 1295 → 555 Zeilen (**57% Reduktion**)
- 7 separate Module für bessere Wartbarkeit
- Alle 10 Tests bestanden
- Build erfolgreich

---

### 🟡 Mittel

#### 2. Console.log Statements (30+)

**Problem:** Viele `console.log` Statements, besonders in `useWakeWord.js`.

**Empfehlung:** Debug-Logs entfernen oder hinter Feature-Flag.

---

#### 3. Keine TypeScript Migration

**Problem:** Gesamtes Frontend in JavaScript (JSX), keine Type-Safety.

**Empfehlung:** Schrittweise TypeScript Migration für neue Dateien.

---

#### 4. Outdated Dependencies

| Package | Current | Latest | Breaking |
|---------|---------|--------|----------|
| react | 18.3.1 | 19.x | ⚠️ Major |
| react-router-dom | 6.30.3 | 7.x | ⚠️ Major |
| tailwindcss | 3.4.19 | 4.x | ⚠️ Major |
| vite | 5.4.21 | 7.x | ⚠️ Major |
| @headlessui/react | 1.7.19 | 2.x | ⚠️ Major |
| lucide-react | 0.307.0 | 0.563.0 | ✅ Minor |

**Empfehlung:** Minor-Updates zeitnah, Major-Updates planen.

---

#### 5. ESLint-Disable Kommentare

```javascript
// useDeviceConnection.js:542
// eslint-disable-next-line react-hooks/exhaustive-deps
```

**Empfehlung:** Dependencies prüfen und korrekt angeben.

---

### 🟢 Niedrig

#### 6. Große Komponenten

- `SpeakersPage.jsx` (1027 Zeilen)
- `RoomsPage.jsx` (762 Zeilen)
- `useDeviceConnection.js` (616 Zeilen)

#### 7. Fehlende Error Boundaries

Nur eine zentrale ErrorBoundary, keine Feature-spezifischen.

#### 8. Keine Unit Tests für Hooks

Custom Hooks wie `useWakeWord.js` haben keine Tests.

---

## Satellite

### 🟡 Mittel

#### 1. Bare Except Clauses (20+)

**Betroffene Dateien:**
- `hardware/button.py` (6 Stellen)
- `hardware/led.py` (1)
- `audio/playback.py` (4)
- `audio/capture.py` (3)
- `satellite.py` (1)

**Empfehlung:** Spezifische Exceptions, besonders für Hardware-Fehler.

---

#### 2. satellite.py Größe (875 Zeilen)

**Problem:** Große State Machine mit viel Logik.

**Empfehlung:** States und Transitions in separate Klassen.

---

#### 3. Hardware-Abhängigkeiten nicht gemockt

**Problem:** Tests benötigen echte Hardware (GPIO, SPI).

**Empfehlung:** Hardware-Abstraktionsschicht für Tests.

---

### 🟢 Niedrig

#### 4. Pi Zero 2 W Einschränkungen (dokumentiert)

- Kein PyTorch (ARM32)
- Kein Silero VAD
- 512MB RAM Limit

Siehe: `src/satellite/TECHNICAL_DEBT.md`

#### 5. Logging Inkonsistenz

Mix aus `print()` und `logger`.

---

## Infrastruktur

### ~~🔴 Kritisch~~ → ✅ Behoben

#### ~~1. :latest Tags in Docker~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Ursprüngliches Problem:** Docker Compose Dateien verwendeten `:latest` Tags.

**Lösung:** Alle Images auf spezifische Versionen gepinnt:
- `ollama/ollama:latest` → `ollama/ollama:0.15.1`
- `nginx:alpine` → `nginx:1.28-alpine`

Bereits gepinnte Images:
- `pgvector/pgvector:pg16` ✅
- `redis:7-alpine` ✅

---

### 🟡 Mittel

#### 2. Unpinned Python Dependencies

**Problem:** Nur 7 von 40 Requirements haben gepinnte Versionen.

```
# Vorher
fastapi
pydantic

# Nachher
fastapi==0.115.6
pydantic==2.10.5
```

**Empfehlung:** `pip-compile` oder `poetry` für Lockfile.

---

#### 3. Keine Health Checks in Docker Compose

**Problem:** Nur Backend hat Health Check, andere Services nicht.

**Empfehlung:** Health Checks für alle Services.

---

#### 4. Fehlende Rate Limiting

**Problem:** Kein globales Rate Limiting für API.

**Empfehlung:** slowapi oder nginx Rate Limiting.

---

### 🟢 Niedrig

#### 5. Keine Multi-Stage Builds

Frontend Dockerfile könnte Multi-Stage für kleinere Images nutzen.

#### 6. Secrets in .env

Besser: Docker Secrets oder Vault für Produktion.

---

## Test-Coverage

| Bereich | Test Files | Source Files | Ratio |
|---------|------------|--------------|-------|
| Backend | 29 | 72 | 40% |
| Frontend | 10 | ~40 | 25% |
| Satellite | 1 | 15 | 7% |

### Fehlende Tests

- [ ] `services/audio_output_service.py` - kein Test
- [ ] `services/output_routing_service.py` - kein Test
- [ ] `integrations/frigate.py` - nur Mock-Tests
- [ ] Frontend Hooks - keine Unit Tests
- [ ] Satellite Hardware - keine Tests möglich ohne Mocks

---

## Priorisierte Empfehlungen

### Sofort (< 1 Woche)

1. ✅ ~~Bare except → Exception ersetzen~~ (2026-01-25)
2. ✅ ~~Docker :latest → gepinnte Versionen~~ (2026-01-25)
3. ⬜ Console.log Statements entfernen

### Kurzfristig (1-4 Wochen)

4. ✅ ~~main.py Refactoring~~ (2026-01-25)
5. ✅ ~~ChatPage.jsx aufteilen~~ (2026-01-25)
6. ⬜ Requirements pinnen
7. ✅ ~~Type Hints hinzufügen (Backend)~~ (2026-01-25)
8. ✅ ~~ollama_service.py Refactoring~~ (2026-01-25)

### Mittelfristig (1-3 Monate)

9. ⬜ TypeScript Migration (Frontend)
10. ⬜ Test-Coverage erhöhen auf 60%+
11. ⬜ Dependency Updates (Minor)

### Langfristig (3-6 Monate)

12. ⬜ Major Dependency Updates (React 19, etc.)
13. ⬜ Hardware-Abstraktionsschicht (Satellite)
14. ⬜ Multi-Stage Docker Builds

---

## Changelog

| Datum | Änderung |
|-------|----------|
| 2026-01-25 | ConversationService extrahiert aus OllamaService: 966 → 773 Zeilen (20% Reduktion) (#28) |
| 2026-01-25 | Type Hints hinzugefügt: ollama_service.py, audio_output_service.py (#28) |
| 2026-01-25 | Schemas extrahiert: rooms_schemas.py, knowledge_schemas.py (#28) |
| 2026-01-25 | CLI-Test-Tools dokumentiert (print statements OK für CLI) (#28) |
| 2026-01-25 | Hardcoded localhost durch BACKEND_INTERNAL_URL ersetzt (#28) |
| 2026-01-25 | ChatPage.jsx Refactoring: 1295 → 555 Zeilen (57% Reduktion), 7 Module (#30) |
| 2026-01-25 | Docker :latest Tags durch gepinnte Versionen ersetzt (#35) |
| 2026-01-25 | Lifecycle-Management extrahiert nach api/lifecycle.py (#27) |
| 2026-01-25 | main.py Refactoring abgeschlossen: 2130 → 337 Zeilen (84% Reduktion) (#27) |
| 2026-01-25 | WebSocket-Handler extrahiert: chat, satellite, device (#27) |
| 2026-01-25 | main.py Refactoring Phase 1: Shared Utilities extrahiert (#27) |
| 2026-01-25 | Bare Except Clauses im Backend behoben (#27) |
| 2026-01-25 | Initial Technical Debt Analyse |
