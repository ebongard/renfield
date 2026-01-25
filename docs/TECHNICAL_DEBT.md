# Technical Debt - Renfield System

Dieses Dokument enthält eine umfassende Analyse der technischen Schulden im gesamten Renfield-System.

**Letzte Aktualisierung:** 2026-01-25

---

## Übersicht

| Bereich | Kritisch | Mittel | Niedrig | Gesamt | Behoben |
|---------|----------|--------|---------|--------|---------|
| Backend | 0 | 5 | 4 | 9 | 2 |
| Frontend | 1 | 4 | 3 | 8 | 0 |
| Satellite | 0 | 3 | 2 | 5 | 0 |
| Infrastruktur | 0 | 3 | 2 | 5 | 1 |
| **Gesamt** | **1** | **15** | **11** | **27** | **3** |

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

#### 3. Große API-Route-Dateien

| Datei | Zeilen | Empfehlung |
|-------|--------|------------|
| `routes/rooms.py` | 1024 | In `rooms/` Modul aufteilen |
| `routes/knowledge.py` | 1019 | CRUD von Logik trennen |
| `routes/speakers.py` | 650 | OK, beobachten |

---

#### 4. Hardcoded Fallback-Werte

**Problem:** Fallback auf `localhost` funktioniert nicht in Container-Umgebungen.

```python
# audio_output_service.py:245
return "http://localhost:8000"
```

**Empfehlung:** Immer über Konfiguration/Environment lösen.

---

#### 5. Print Statements statt Logger

**Datei:** `test_plugins.py` enthält 25+ `print()` Statements.

**Empfehlung:** Durch `logger.info()` ersetzen oder als separates CLI-Tool kennzeichnen.

---

#### 6. Fehlende Type Hints

**Problem:** Viele Funktionen haben keine Type Hints.

**Empfehlung:** Schrittweise Type Hints hinzufügen, mit `mypy` prüfen.

---

#### 7. Ollama Service Größe (966 Zeilen)

**Problem:** `ollama_service.py` ist sehr groß und hat mehrere Verantwortlichkeiten.

**Empfehlung:** Intent-Extraction, Streaming und RAG in separate Module.

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

### 🔴 Kritisch

#### 1. ChatPage.jsx (1295 Zeilen)

**Problem:** Monolithische Komponente mit zu vielen Verantwortlichkeiten:
- WebSocket-Verbindung
- Audio Recording
- Message Rendering
- Session Management

**Empfehlung:** Aufteilen in:
```
pages/ChatPage/
├── index.jsx
├── ChatMessages.jsx
├── ChatInput.jsx
├── AudioControls.jsx
└── hooks/
    ├── useChatWebSocket.js
    └── useAudioRecording.js
```

**Aufwand:** ~2-3 Tage

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
5. ⬜ ChatPage.jsx aufteilen
6. ⬜ Requirements pinnen
7. ⬜ Type Hints hinzufügen (Backend)

### Mittelfristig (1-3 Monate)

8. ⬜ TypeScript Migration (Frontend)
9. ⬜ Test-Coverage erhöhen auf 60%+
10. ⬜ Dependency Updates (Minor)

### Langfristig (3-6 Monate)

11. ⬜ Major Dependency Updates (React 19, etc.)
12. ⬜ Hardware-Abstraktionsschicht (Satellite)
13. ⬜ Multi-Stage Docker Builds

---

## Changelog

| Datum | Änderung |
|-------|----------|
| 2026-01-25 | Docker :latest Tags durch gepinnte Versionen ersetzt (#35) |
| 2026-01-25 | Lifecycle-Management extrahiert nach api/lifecycle.py (#27) |
| 2026-01-25 | main.py Refactoring abgeschlossen: 2130 → 337 Zeilen (84% Reduktion) (#27) |
| 2026-01-25 | WebSocket-Handler extrahiert: chat, satellite, device (#27) |
| 2026-01-25 | main.py Refactoring Phase 1: Shared Utilities extrahiert (#27) |
| 2026-01-25 | Bare Except Clauses im Backend behoben (#27) |
| 2026-01-25 | Initial Technical Debt Analyse |
